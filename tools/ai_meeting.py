"""AI同士の会議を、GitHubのIssue上で開く(.github/workflows/ai-meeting.yml から呼ばれる)。

- 議題: 指定があればそれ。なければ「議題」ラベルの開いたIssue → docs/design.md の未決事項のうち、まだ会議していないもの
- 参加者: AI_MODELS の各モデルが、AGENTS.md の会議ルールに沿って順にコメントする。2周目以降は反論・補足
- 資料: design.md は、議題に関係する節は本文まるごと、それ以外は見出しと「確定」の行だけを渡す(元の文の切り出しで、要約はしない)
- 最後に1つのモデルが論点を整理し、「オーナーに決めてほしいこと」を書く
- AIはファイルを変更しない(Issueへのコメントだけ)。決定とファイルへの反映はオーナーが行う

使うもの(すべて環境変数):
  GITHUB_TOKEN, GITHUB_REPOSITORY            … GitHub Actions が自動で渡す
  AI_API_BASE                                 … ゲートウェイ(OpenAI互換API)の入口(…/v1 まで)。Secrets に登録
  AI_API_KEY                                  … ゲートウェイのAPIキー。Secrets に登録
  AI_KEY_HEADER                               … キーを渡すヘッダー名(既定 Authorization。専用ヘッダーのゲートウェイならその名前)
  AI_MODELS                                   … 「表示名=モデルID」をカンマ区切り(既定は下の DEFAULT_MODELS)
                                                モデルIDの前に「gemini:」「openai:」「anthropic:」「mistral:」を付けると、
                                                ゲートウェイを通さず、その会社の公式APIを直接使う(例: Gemini=gemini:gemini-4-neon)
                                                モデルIDの後ろに「@low」「@medium」「@high」などを付けると、考える量(エフォート)を指定する
                                                (例: Gemini=gemini:gemini-4-argon@medium)。付けなければモデルの既定のまま
  AI_SUMMARY_EFFORT                           … まとめ役のときだけ使うエフォート(任意。例: high)
  GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / MISTRAL_API_KEY
                                              … 公式APIを直接使うときのキー。使う会社のぶんだけ Secrets に登録
                                                (ゲートウェイを使うモデルがなければ、AI_API_BASE / AI_API_KEY は不要)
  AI_CALL_INTERVAL                            … 呼び出しの間に空ける秒数(既定15。混雑しやすいゲートウェイ向け)
  MEETING_TOPIC / MEETING_ISSUE / MEETING_ROUNDS … 手動実行のときの入力
Python標準ライブラリだけで動く。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS = "Claude=claude-opus-5.5,GPT=gpt-6.1-sol,Gemini=gemini-4-argon"
MEETING_LABEL = "AI会議"
AGENDA_LABEL = "議題"
MAX_ROUNDS = 3
COMMENT_LIMIT = 60000          # GitHubのコメントの上限(65536文字)より少し小さく
RETRY_WAITS = (15, 45)   # 混雑・時間切れのときのやり直しの間隔(秒)。長く待つより、その席を飛ばして会議を進める
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
FAILED_MARKERS = ("(応答できませんでした", "(まとめを作れませんでした")   # 以前の版が書いた失敗のコメント
FOOTER = "\n\n---\n<sub>AI会議の自動投稿です。決定はオーナーが行います。</sub>"
# 公式APIの入口(どれもOpenAI互換の /chat/completions を持つ)。check=False は /models の確認を飛ばす
PROVIDERS = {
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": "GEMINI_API_KEY", "check": True},
    "openai": {"base": "https://api.openai.com/v1", "key": "OPENAI_API_KEY", "check": True},
    "anthropic": {"base": "https://api.anthropic.com/v1", "key": "ANTHROPIC_API_KEY", "check": False},
    "mistral": {"base": "https://api.mistral.ai/v1", "key": "MISTRAL_API_KEY", "check": True},
}


class MeetingError(RuntimeError):
    """利用者に見せてよいエラー(秘密情報を含まない)。"""


# ---------- 設定 ----------
def parse_models(text: str) -> list[tuple[str, str]]:
    models = []
    for part in (text or DEFAULT_MODELS).split(","):
        if not part.strip():
            continue
        name, _, model = part.partition("=")
        if not name.strip() or not model.strip():
            raise MeetingError(f"AI_MODELS の書き方が違います: {part!r}(「表示名=モデルID」をカンマ区切りで)")
        split_effort(model)   # エフォートの書き方を、会議を始める前に確かめる
        models.append((name.strip(), model.strip()))
    if not 2 <= len(models) <= 5:
        raise MeetingError("参加モデルは2〜5個にしてください")
    return models


EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


def split_effort(spec: str) -> tuple[str, str | None]:
    """「gemini:gemini-4-argon@medium」→ ("gemini:gemini-4-argon", "medium")。「@」がなければエフォートは None。"""
    head, sep, effort = spec.strip().rpartition("@")
    if not sep:
        return spec.strip(), None
    effort = effort.strip().lower()
    if effort not in EFFORTS:
        raise MeetingError(f"エフォートの書き方が違います: {spec!r}(使えるのは {' / '.join(EFFORTS)})")
    return head.strip(), effort


def split_model(spec: str) -> tuple[str, str]:
    """「gemini:gemini-4-neon@low」→ ("gemini", "gemini-4-neon")。会社名がなければゲートウェイ("", モデルID)。"""
    spec, _ = split_effort(spec)
    provider, sep, model = spec.partition(":")
    if sep and provider.strip().lower() in PROVIDERS and model.strip():
        return provider.strip().lower(), model.strip()
    return "", spec.strip()


def parse_rounds(text: str) -> int:
    try:
        rounds = int(text or 2)
    except ValueError as error:
        raise MeetingError("周回数は数字で指定してください") from error
    return max(1, min(MAX_ROUNDS, rounds))


# ---------- 議題 ----------
def open_items(design: str) -> list[str]:
    """docs/design.md の未決事項。「## 12. 未決事項」のチェックの付いていない項目と、`未決` の印が付いた行。"""
    items: list[str] = []
    in_open = False
    for line in design.splitlines():
        if line.startswith("## "):
            in_open = "未決事項" in line
            continue
        if in_open:
            match = re.match(r"\s*-\s*\[ \]\s*(.+)", line)
            if match:
                items.append(match.group(1).strip())
        elif "`未決`" in line and not line.lstrip().startswith(">"):   # 引用(凡例など)は除く
            text = re.sub(r"^\s*[-*]\s*", "", line).replace("`未決`", "").strip(" 　")
            if text:
                items.append(text)
    return items


def _plain(text: str) -> str:
    return re.sub(r"\[AI会議\]|[\s　（）()「」]", "", text)


def already_discussed(item: str, titles: list[str]) -> bool:
    """その未決事項の会議がもうあるか。人が短く書いたタイトルにも当たるよう、先頭12文字で照合する。"""
    key = _plain(item)[:12]
    return any(key and (key in _plain(title) or (_plain(title) and _plain(title) in _plain(item))) for title in titles)


def issue_body(topic: str) -> str:
    return f"""## 議題
{topic}

## 前提・決まっていること
`docs/design.md` と `docs/decisions.md` の決定済み事項を前提にする。

## 聞きたいこと
1. どう決めるのがよいか(おすすめの案と理由)
2. ほかの案と比べたときの長所・短所
3. 試作で確かめるなら、何をどう見ればよいか

## 出してほしい形
- AGENTS.md の「コメントの型」で
- 【現】と【ゲ】を分ける

---
この議題は、AI会議のワークフローで自動で立てました。AIはコメントで意見を出すだけで、ファイルは変更しません。"""


# ---------- 外部とのやりとり ----------
class GitHub:
    def __init__(self, token: str, repo: str, api: str = "https://api.github.com", opener=urllib.request.urlopen):
        if not token or not repo:
            raise MeetingError("GITHUB_TOKEN と GITHUB_REPOSITORY が必要です(GitHub Actions から実行してください)")
        self.token, self.repo, self.api, self.opener = token, repo, api.rstrip("/"), opener

    def call(self, method: str, path: str, body: dict | None = None, ok_statuses=(200, 201)):
        request = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}", method=method,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "User-Agent": "ai-meeting"})
        try:
            with self.opener(request, timeout=30) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            if error.code in ok_statuses:
                return None
            raise MeetingError(f"GitHubへの操作に失敗しました({method} {path}: HTTP {error.code})") from error

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self.call("POST", "/labels", {"name": name, "color": color, "description": description}, ok_statuses=(422,))

    def issues(self, label: str, state: str = "all") -> list[dict]:
        query = urllib.parse.urlencode({"labels": label, "state": state, "per_page": 100, "sort": "created", "direction": "asc"})
        return [i for i in self.call("GET", f"/issues?{query}") or [] if "pull_request" not in i]

    def issue(self, number: int) -> dict:
        return self.call("GET", f"/issues/{number}")

    def comments(self, number: int) -> list[dict]:
        return self.call("GET", f"/issues/{number}/comments?per_page=100") or []

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        return self.call("POST", "/issues", {"title": title, "body": body, "labels": labels})

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.call("POST", f"/issues/{number}/labels", {"labels": labels})

    def comment(self, number: int, body: str) -> None:
        self.call("POST", f"/issues/{number}/comments", {"body": body})


class Models:
    def __init__(self, base: str, key: str, header: str = "Authorization", opener=urllib.request.urlopen, sleep=time.sleep,
                 interval: float = 15, clock=time.monotonic, key_name: str = "AI_API_KEY"):
        if not base or not key:
            raise MeetingError("AI_API_BASE と AI_API_KEY を、リポジトリの Secrets に登録してください")
        if any(c.isspace() or ord(c) < 32 for c in key):
            raise MeetingError(f"{key_name} に改行や空白が混ざっています(キー1行だけにしてください)")
        self.key_name = key_name
        self.base, self.opener, self.sleep, self.clock = base.rstrip("/"), opener, sleep, clock
        self.interval = max(0.0, interval)
        self._last_call: float | None = None
        header = header or "Authorization"
        self.headers = {"Authorization": f"Bearer {key}"} if header.lower() == "authorization" else {header: key}

    def check(self) -> None:
        """会議を始める前に、キーが受け付けられるかを確かめる(だめならIssueを作らずに止める)。"""
        request = urllib.request.Request(f"{self.base}/models", method="GET",
                                         headers={**self.headers, "User-Agent": "ai-meeting"})
        try:
            with self.opener(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise MeetingError(f"APIキーが受け付けられませんでした(HTTP {error.code})。{self.key_name} を確認してください") from error
            # /models に対応していない入口もあるので、それ以外は会議の中で確かめる
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise MeetingError(f"AI_API_BASE に接続できません({type(error).__name__})") from error

    def chat(self, model: str, system: str, user: str, effort: str | None = None) -> str:
        payload_in = {"model": model, "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}]}
        if effort:   # 指定したときだけ送る(対応していない入口もあるため)
            payload_in["reasoning_effort"] = effort
        body = json.dumps(payload_in).encode("utf-8")
        last = "不明なエラー"
        for attempt in range(len(RETRY_WAITS) + 1):
            if self._last_call is not None:   # 短い間隔で続けて送ると、回数・量の制限に当たりやすい
                wait = self.interval - (self.clock() - self._last_call)
                if wait > 0:
                    self.sleep(wait)
            self._last_call = self.clock()
            request = urllib.request.Request(f"{self.base}/chat/completions", data=body, method="POST",
                                             headers={**self.headers, "Content-Type": "application/json",
                                                      "User-Agent": "ai-meeting"})
            retry_after = None
            try:
                with self.opener(request, timeout=180) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                content = payload["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                if str(content).strip():
                    return str(content).strip()
                last = "空の返答"
            except urllib.error.HTTPError as error:   # 本文にはキーの一部が入ることがあるので、番号だけ残す
                last = f"HTTP {error.code}"
                if error.code not in RETRYABLE:
                    break
                retry_after = (error.headers or {}).get("Retry-After") if hasattr(error, "headers") else None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = f"接続できません({type(error).__name__})"
            except (KeyError, IndexError, TypeError, ValueError):
                last = "返答の形式が想定と違います"
                break
            if attempt < len(RETRY_WAITS):
                wait = RETRY_WAITS[attempt]
                if retry_after and str(retry_after).isdigit():   # 「何秒後に」と言われたらそれに従う(最大2分)
                    wait = max(wait, min(int(retry_after), 120))
                print(f"  {model}: {last}、{wait}秒後にやり直し", flush=True)
                self.sleep(wait)
        raise MeetingError(last)


class Router:
    """モデルごとに、ゲートウェイか公式APIのどちらに送るかを振り分ける(chat と check は Models と同じ形)。"""

    def __init__(self, env: dict, participants: list[tuple[str, str]], interval: float = 15, make=Models):
        self.routes: dict[str, Models] = {}
        missing = []
        for _, spec in participants:
            provider, _ = split_model(spec)
            if provider in self.routes:
                continue
            if provider:
                info = PROVIDERS[provider]
                key = env.get(info["key"], "")
                if not key:
                    missing.append(info["key"])
                    continue
                self.routes[provider] = make(info["base"], key, "Authorization", interval=interval, key_name=info["key"])
            else:
                self.routes[""] = make(env.get("AI_API_BASE", ""), env.get("AI_API_KEY", ""), env.get("AI_KEY_HEADER", ""),
                                       interval=interval)
        if missing:
            raise MeetingError(f"公式APIのキーが Secrets にありません: {'、'.join(sorted(set(missing)))}")

    def check(self) -> None:
        for provider, route in self.routes.items():
            if not provider or PROVIDERS[provider]["check"]:
                route.check()

    def chat(self, spec: str, system: str, user: str, effort: str | None = None) -> str:
        provider, model = split_model(spec)
        effort = effort or split_effort(spec)[1]
        if effort:
            return self.routes[provider].chat(model, system, user, effort=effort)
        return self.routes[provider].chat(model, system, user)


# ---------- 会議 ----------
STOPWORDS = {"AI", "AI会議", "会議", "議題", "確定", "未決", "整理", "方針", "仕様", "どうする", "決める", "について",
             "それとも", "するか", "どちら", "ゲーム", "案", "一旦"}
BRIEF_BUDGET = 20000   # design.md から本文まるごと渡す節の合計の上限(文字)


def topic_terms(text: str) -> list[str]:
    """議題の文から、design.md の見出しや本文と照らし合わせる言葉(漢字・カタカナ・英数字の2文字以上のまとまり)を取り出す。"""
    terms = []
    # 漢字・カタカナ・英数字は別々のまとまりにする(「バイオーム仕様」→「バイオーム」「仕様」)
    for word in re.findall(r"[0-9]+[一-龥々]|[一-龥々]{2,}|[ァ-ヴー]{2,}|[A-Za-z0-9]{2,}", text or ""):
        if word not in STOPWORDS and word not in terms:
            terms.append(word)
    return terms


def split_sections(text: str) -> list[tuple[str, str]]:
    """「## 」「### 」の見出しで区切る。[(見出し行, 本文)] の並び(最初の見出しより前は見出し行が空)。"""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            sections.append((line, []))
        else:
            sections[-1][1].append(line)
    return [(head, "\n".join(body).strip("\n")) for head, body in sections]


def brief_design(design: str, topic: str, budget: int = BRIEF_BUDGET) -> str:
    """design.md を、議題に関係する節は本文まるごと、それ以外は見出しと「確定」の行だけにして渡す。
    元の文をそのまま切り出すだけなので、要約で意味が変わることはない。"""
    terms = topic_terms(topic)
    sections = split_sections(design)

    def score(head: str, body: str) -> int:
        in_head = sum(3 for t in terms if t in head)
        in_body = sum(1 for t in terms if t in body)
        return in_head + (in_body if in_body >= 2 else 0)

    always = ("## 1. コンセプト", "## 12. 未決事項")
    # 「## 」の節と、その下の「### 」の節をひとまとまりとして扱う(親が当たれば子も、子が当たれば親の前置きも載せる)
    parent: dict[int, int] = {}
    current = 0
    for i, (head, _) in enumerate(sections):
        if head.startswith("## "):
            current = i
        elif head.startswith("### "):
            parent[i] = current
    ranked = sorted(((score(h, b), i) for i, (h, b) in enumerate(sections) if h and not h.startswith(always)), reverse=True)
    full, used = set(), 0
    for value, i in ranked:
        if value < 3:
            break
        group = {i}
        if i in parent:
            group.add(parent[i])
        else:
            group.update(j for j, p in parent.items() if p == i)
        group -= full
        size = sum(len(sections[j][1]) for j in group)
        if used + size > budget:
            continue
        full |= group
        used += size
    parts = ["> これは docs/design.md の抜粋です。議題に関係する節は本文まるごと、それ以外の節は見出しと `確定` の行だけを載せています。"
             "載っていない節の中身を推測で語らないでください。"]
    for i, (head, body) in enumerate(sections):
        if not head:
            parts.append(body)
        elif i in full or head.startswith(always):
            parts.append(f"{head}\n{body}")
        else:
            kept = [line for line in body.splitlines() if "`確定`" in line]
            parts.append("\n".join([head + ("" if kept else "　(省略)")] + kept))
    return "\n\n".join(p for p in parts if p.strip())


def brief_decisions(decisions: str, topic: str) -> str:
    """decisions.md を、各決定の見出しと「決定：」の行だけにする。議題に関係する決定は理由・却下した案まで残す。"""
    terms = topic_terms(topic)
    parts = []
    for head, body in split_sections(decisions):
        if not head.startswith("## 20"):   # 書き方の見本やはじめの説明は省く
            continue
        hits = sum(1 for t in terms if t in head or t in body)
        if hits >= 2:
            parts.append(f"{head}\n{body}")
        else:
            parts.append("\n".join([head] + [line for line in body.splitlines() if line.startswith("- 決定")]))
    return "> これは docs/decisions.md の抜粋です(関係の薄い決定は「決定」の行だけ)。\n\n" + "\n\n".join(parts)


def system_prompt(agents_md: str, name: str, others: list[str]) -> str:
    return f"""あなたは、ゲームの企画会議に参加している「{name}」です。ほかの参加者は {'、'.join(others)} です。
次のリポジトリのルール(AGENTS.md)に必ず従ってください。

{agents_md}

追加の決まり:
- 返答は、Issueに投稿するコメント本文だけにする(前置きや「承知しました」は書かない)
- ファイルの変更やPRを提案・指示しない。決定はオーナーが行う
- 名前で呼びかけるときに「@」を付けない"""


def transcript(issue: dict, comments: list[dict]) -> str:
    parts = [f"## 議題(Issue #{issue['number']}): {issue['title']}\n{issue.get('body') or ''}"]
    for c in comments:
        if any(marker in (c.get("body") or "") for marker in FAILED_MARKERS):
            continue
        who = "オーナー" if c.get("user", {}).get("type") != "Bot" and "AI会議の自動投稿" not in (c.get("body") or "") else "AI"
        parts.append(f"### コメント({who})\n{c.get('body') or ''}")
    return "\n\n".join(parts)


def run(env: dict, github: GitHub, models: Models, docs_root: Path = ROOT, log=print) -> dict:
    participants = parse_models(env.get("AI_MODELS", ""))
    rounds = parse_rounds(env.get("MEETING_ROUNDS", ""))
    design = (docs_root / "docs" / "design.md").read_text(encoding="utf-8")
    decisions = (docs_root / "docs" / "decisions.md").read_text(encoding="utf-8")
    agents = (docs_root / "AGENTS.md").read_text(encoding="utf-8")

    github.ensure_label(MEETING_LABEL, "8957e5", "AI同士の会議(自動投稿)")
    github.ensure_label(AGENDA_LABEL, "fbca04", "AI会議で話したい議題")

    # 議題を決める。新しいIssueは、1周目で十分な意見が集まってから作る(失敗だけのIssueを残さない)
    topic = (env.get("MEETING_TOPIC") or "").strip()
    number = (env.get("MEETING_ISSUE") or "").strip().lstrip("#")
    issue: dict | None = None
    if number:
        issue = github.issue(int(number))
    elif not topic:
        discussed = github.issues(MEETING_LABEL)
        waiting = [i for i in github.issues(AGENDA_LABEL, state="open")
                   if MEETING_LABEL not in [label["name"] for label in i.get("labels", [])]]
        if waiting:
            issue = waiting[0]
        else:
            titles = [i["title"] for i in discussed]
            candidates = [item for item in open_items(design) if not already_discussed(item, titles)]
            if not candidates:
                log("議題がありません(未決事項はすべて会議済みです)。議題を指定して実行してください。")
                return {"issue": None, "comments": 0, "skipped": []}
            topic = candidates[0]
    draft = issue or {"number": "新規", "title": f"[AI会議] {topic}", "body": issue_body(topic)}
    log(f"議題: {draft['title']}")

    topic_text = f"{draft['title']}\n{topic}\n{(issue or {}).get('body') or ''}"
    docs = f"## docs/design.md(抜粋)\n{brief_design(design, topic_text)}\n\n## docs/decisions.md(抜粋)\n{brief_decisions(decisions, topic_text)}"
    log(f"資料: {len(docs)} 文字(design.md {len(design)} 文字・decisions.md {len(decisions)} 文字から抜粋)")
    names = [n for n, _ in participants]
    skipped: list[str] = []

    def ask(name: str, model: str, comments: list[dict], instruction: str, role: str = "", effort: str | None = None) -> str | None:
        extra = {"effort": effort} if effort else {}
        started = time.monotonic()
        try:
            text = models.chat(model, system_prompt(agents, name + role, [n for n in names if n != name]),
                               f"{docs}\n\n{transcript(draft, comments)}\n\n---\n{instruction}", **extra)
            log(f"{name}{role}: {time.monotonic() - started:.0f}秒")
            return text
        except MeetingError as error:
            log(f"{name}{role} は応答できませんでした({time.monotonic() - started:.0f}秒): {error}")
            return None

    # 1周目は、まず全員の意見を集めてから投稿する
    earlier = github.comments(issue["number"]) if issue else []
    first: list[str] = []
    for name, model in participants:
        so_far = earlier + [{"body": b, "user": {"type": "Bot"}} for b in first]
        text = ask(name, model, so_far, "この議題について、最初の意見を述べてください。")
        if text is None:
            skipped.append(f"{name}(1周目)")
            continue
        first.append(f"### 🤖 {name}(`{model}`)・1周目\n\n{text}")
        log(f"1周目: {name} が答えました")
    # 1人でも答えられれば会議を開く(答えを捨てない)。誰も答えられなければIssueを作らずに止める
    if not first:
        raise MeetingError("1周目に誰も答えられなかったため、会議を開きませんでした。"
                           "混雑している可能性があります。少し時間をおいて開き直してください")

    if issue is None:
        issue = github.create_issue(draft["title"], draft["body"], [MEETING_LABEL])
    else:
        github.add_labels(issue["number"], [MEETING_LABEL])
    draft = issue
    for body in first:
        github.comment(issue["number"], body[:COMMENT_LIMIT] + FOOTER)
    posted = len(first)
    log(f"議題: #{issue['number']} に1周目を投稿しました")

    for round_no in range(2, rounds + 1):
        for name, model in participants:
            text = ask(name, model, github.comments(issue["number"]),
                       f"{round_no}周目です。ほかの参加者の意見を読み、同意・反論・補足を明示してください。"
                       "すでに出た内容の繰り返しは避け、論点を深めてください。")
            if text is None:
                skipped.append(f"{name}({round_no}周目)")
                continue
            github.comment(issue["number"], f"### 🤖 {name}(`{model}`)・{round_no}周目\n\n{text}"[:COMMENT_LIMIT] + FOOTER)
            posted += 1
            log(f"{round_no}周目: {name} が投稿しました")

    # まとめ(まとめ役が答えられなければ、次のモデルに頼む)
    instruction = ("ここまでの議論を、オーナーが判断しやすいように整理してください。次の見出しで書きます。\n"
                   "### 合意できた点\n### 意見が分かれた点(誰がどの立場か)\n### オーナーに決めてほしいこと(選択肢と、それぞれの影響)\n"
                   "### decisions.md に書くならこの形(案)\n新しい意見は足さないでください。")
    note = f"\n\n<sub>混雑などで参加できなかった回: {'、'.join(skipped)}</sub>" if skipped else ""
    summary_effort = split_effort("x@" + env["AI_SUMMARY_EFFORT"].strip())[1] if (env.get("AI_SUMMARY_EFFORT") or "").strip() else None
    comments = github.comments(issue["number"])
    for name, model in participants:
        text = ask(name, model, comments, instruction, role="(まとめ役)", effort=summary_effort)
        if text is not None:
            github.comment(issue["number"], (f"## 📝 まとめ({name})\n\n{text}")[:COMMENT_LIMIT] + note + FOOTER)
            posted += 1
            break
    else:
        github.comment(issue["number"], "## 📝 まとめ\n\nまとめを作れませんでした(混雑のため)。"
                       f"「続きの会議をするIssue番号」に {issue['number']} を入れ、周回数1で開き直すと、続きから会議できます。" + note + FOOTER)
        posted += 1
    log(f"会議が終わりました: #{issue['number']}({posted}件のコメント)")
    return {"issue": issue["number"], "comments": posted, "skipped": skipped}


def main() -> int:
    env = dict(os.environ)
    try:
        github = GitHub(env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_API_URL", "https://api.github.com"))
        try:
            interval = float(env.get("AI_CALL_INTERVAL") or 15)
        except ValueError:
            interval = 15
        models = Router(env, parse_models(env.get("AI_MODELS", "")), interval=interval)
        models.check()
        run(env, github, models, log=lambda *a: print(*a, flush=True))   # 実行中もログに出るように
        return 0
    except MeetingError as error:
        print(f"::error::{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
