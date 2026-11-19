"""AI同士の会議を、GitHubのIssue上で開く(.github/workflows/ai-meeting.yml から呼ばれる)。

- 議題: 指定があればそれ。なければ「議題」ラベルの開いたIssue → docs/design.md の未決事項のうち、まだ会議していないもの
- 参加者: AI_MODELS の各モデルが、AGENTS.md の会議ルールに沿って順にコメントする。2周目以降は反論・補足
- 最後に1つのモデルが論点を整理し、「オーナーに決めてほしいこと」を書く
- AIはファイルを変更しない(Issueへのコメントだけ)。決定とファイルへの反映はオーナーが行う

使うもの(すべて環境変数):
  GITHUB_TOKEN, GITHUB_REPOSITORY            … GitHub Actions が自動で渡す
  AI_API_BASE                                 … OpenAI互換APIの入口(…/v1 まで)。Secrets に登録
  AI_API_KEY                                  … APIキー。Secrets に登録
  AI_KEY_HEADER                               … キーを渡すヘッダー名(既定 Authorization。専用ヘッダーのゲートウェイならその名前)
  AI_MODELS                                   … 「表示名=モデルID」をカンマ区切り(既定は下の DEFAULT_MODELS)
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
FOOTER = "\n\n---\n<sub>AI会議の自動投稿です。決定はオーナーが行います。</sub>"


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
        models.append((name.strip(), model.strip()))
    if not 2 <= len(models) <= 5:
        raise MeetingError("参加モデルは2〜5個にしてください")
    return models


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
    def __init__(self, base: str, key: str, header: str = "Authorization", opener=urllib.request.urlopen, sleep=time.sleep):
        if not base or not key:
            raise MeetingError("AI_API_BASE と AI_API_KEY を、リポジトリの Secrets に登録してください")
        if any(c.isspace() or ord(c) < 32 for c in key):
            raise MeetingError("AI_API_KEY に改行や空白が混ざっています(キー1行だけにしてください)")
        self.base, self.opener, self.sleep = base.rstrip("/"), opener, sleep
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
                raise MeetingError(f"APIキーが受け付けられませんでした(HTTP {error.code})。AI_API_KEY と AI_KEY_HEADER を確認してください") from error
            # /models に対応していない入口もあるので、それ以外は会議の中で確かめる
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise MeetingError(f"AI_API_BASE に接続できません({type(error).__name__})") from error

    def chat(self, model: str, system: str, user: str) -> str:
        body = json.dumps({"model": model, "messages": [{"role": "system", "content": system},
                                                        {"role": "user", "content": user}]}).encode("utf-8")
        last = "不明なエラー"
        for attempt in range(3):
            request = urllib.request.Request(f"{self.base}/chat/completions", data=body, method="POST",
                                             headers={**self.headers, "Content-Type": "application/json",
                                                      "User-Agent": "ai-meeting"})
            try:
                with self.opener(request, timeout=240) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                content = payload["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                if str(content).strip():
                    return str(content).strip()
                last = "空の返答"
            except urllib.error.HTTPError as error:   # 本文にはキーの一部が入ることがあるので、番号だけ残す
                last = f"HTTP {error.code}"
                if error.code not in (408, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = f"接続できません({type(error).__name__})"
            except (KeyError, IndexError, TypeError, ValueError):
                last = "返答の形式が想定と違います"
                break
            self.sleep(5 * (attempt + 1))
        raise MeetingError(last)


# ---------- 会議 ----------
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

    # 議題を決める
    topic = (env.get("MEETING_TOPIC") or "").strip()
    number = (env.get("MEETING_ISSUE") or "").strip().lstrip("#")
    if number:
        issue = github.issue(int(number))
        github.add_labels(issue["number"], [MEETING_LABEL])
    elif topic:
        issue = github.create_issue(f"[AI会議] {topic}", issue_body(topic), [MEETING_LABEL])
    else:
        discussed = github.issues(MEETING_LABEL)
        waiting = [i for i in github.issues(AGENDA_LABEL, state="open")
                   if MEETING_LABEL not in [label["name"] for label in i.get("labels", [])]]
        if waiting:
            issue = waiting[0]
            github.add_labels(issue["number"], [MEETING_LABEL])
        else:
            titles = [i["title"] for i in discussed]
            candidates = [item for item in open_items(design) if not already_discussed(item, titles)]
            if not candidates:
                log("議題がありません(未決事項はすべて会議済みです)。議題を指定して実行してください。")
                return {"issue": None, "comments": 0}
            issue = github.create_issue(f"[AI会議] {candidates[0]}", issue_body(candidates[0]), [MEETING_LABEL])
    log(f"議題: #{issue['number']} {issue['title']}")

    docs = f"## docs/design.md\n{design}\n\n## docs/decisions.md\n{decisions}"
    posted = 0
    names = [n for n, _ in participants]
    for round_no in range(1, rounds + 1):
        answered = 0
        for name, model in participants:
            comments = github.comments(issue["number"])
            ask = ("この議題について、最初の意見を述べてください。" if round_no == 1 else
                   f"{round_no}周目です。ほかの参加者の意見を読み、同意・反論・補足を明示してください。"
                   "すでに出た内容の繰り返しは避け、論点を深めてください。")
            try:
                text = models.chat(model, system_prompt(agents, name, [n for n in names if n != name]),
                                   f"{docs}\n\n{transcript(issue, comments)}\n\n---\n{ask}")
                body = f"### 🤖 {name}(`{model}`)・{round_no}周目\n\n{text}"
                answered += 1
            except MeetingError as error:
                body = f"### 🤖 {name}(`{model}`)・{round_no}周目\n\n(応答できませんでした: {error})"
            github.comment(issue["number"], body[:COMMENT_LIMIT] + FOOTER)
            posted += 1
            log(f"{round_no}周目: {name} が投稿しました")
        if round_no == 1 and not answered:
            raise MeetingError("どのモデルも応答できませんでした。AI_API_BASE・AI_API_KEY・AI_MODELS を確認してください")

    # まとめ
    name, model = participants[0]
    comments = github.comments(issue["number"])
    ask = ("ここまでの議論を、オーナーが判断しやすいように整理してください。次の見出しで書きます。\n"
           "### 合意できた点\n### 意見が分かれた点(誰がどの立場か)\n### オーナーに決めてほしいこと(選択肢と、それぞれの影響)\n"
           "### decisions.md に書くならこの形(案)\n新しい意見は足さないでください。")
    try:
        text = models.chat(model, system_prompt(agents, f"{name}(まとめ役)", [n for n in names if n != name]),
                           f"{docs}\n\n{transcript(issue, comments)}\n\n---\n{ask}")
        github.comment(issue["number"], (f"## 📝 まとめ({name})\n\n{text}")[:COMMENT_LIMIT] + FOOTER)
        posted += 1
    except MeetingError as error:
        github.comment(issue["number"], f"## 📝 まとめ\n\n(まとめを作れませんでした: {error})" + FOOTER)
        posted += 1
    log(f"会議が終わりました: #{issue['number']}({posted}件のコメント)")
    return {"issue": issue["number"], "comments": posted}


def main() -> int:
    env = dict(os.environ)
    try:
        github = GitHub(env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_API_URL", "https://api.github.com"))
        models = Models(env.get("AI_API_BASE", ""), env.get("AI_API_KEY", ""), env.get("AI_KEY_HEADER", ""))
        models.check()
        run(env, github, models)
        return 0
    except MeetingError as error:
        print(f"::error::{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
