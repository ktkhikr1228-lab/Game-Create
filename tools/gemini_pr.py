"""Issueの依頼をもとに Gemini にファイルの変更を作ってもらう(.github/workflows/gemini-pr.yml から呼ばれる)。

- Issueのタイトル・本文・コメント、AGENTS.md・docs の文書、関係しそうなファイルの中身を、まとめてGeminiに渡す
- Geminiには道具(コマンド・GitHubへの書き込み)を持たせない。返ってきた「ファイルの新しい全文」を、
  このスクリプトが作業ツリーに書き込むだけ。コミット・push・PR作成はワークフローが行う
- 書き込めるのはリポジトリの中だけ。.git / .github の中は書かない(GITHUB_TOKEN ではワークフローを変更できないため)
- 変更があれば $GITHUB_OUTPUT に changed=true を書き、$RUNNER_TEMP/gemini_pr/ に
  commit_msg.txt(1行目がPRのタイトル)と pr_body.md を書く。変更がなければ changed=false にして、Issueにその旨をコメントする

使うもの(すべて環境変数):
  GITHUB_TOKEN, GITHUB_REPOSITORY   … GitHub Actions が自動で渡す
  ISSUE_NUMBER                      … 依頼のIssueの番号
  GEMINI_API_KEY                    … Google AI Studio のAPIキー(Secrets)
  GEMINI_MODEL                      … 使うモデル(Variables。既定 gemini-4-argon)。後ろに「@medium」などでエフォート指定可
  COMMENT_BODY                      … 呼び出したコメントの本文(「/pr」の後ろの追加の指示を読む)
  GITHUB_OUTPUT, RUNNER_TEMP        … GitHub Actions が自動で渡す
Python標準ライブラリだけで動く。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_meeting import COMMENT_LIMIT, PROVIDERS, GitHub, MeetingError, Models, split_effort  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-4-argon"
TRIGGER = "@gemini-cli /pr"
CONTEXT_BUDGET = 300000      # 渡すファイルの中身の合計の上限(文字)
FILE_LIMIT = 80000           # 1ファイルの上限(文字)。超えるファイルは中身を渡さない
THREAD_BUDGET = 60000        # Issueのやり取りの上限(文字)。超えたら古いコメントから省く
MAX_FILES = 30               # 1回で変更できるファイルの数
BLOCKED_DIRS = {".git", ".github"}
TEXT_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".json", ".html", ".css", ".yml", ".yaml", ".toml", ".csv", ".gd", ".cs", ".lua"}
FOOTER = "\n\n---\n<sub>Geminiによる自動作成です。取り込むかどうかはオーナーが決めます。</sub>"

PR_RULES = f"""あなたはこのリポジトリの参加者(Gemini)です。オーナーがIssueで「PRにして」と明示したので、今回はファイルを変更してよい。
上の AGENTS.md のルール(ゲーム内の文章のルール、【現】【ゲ】の区別、データ・素材の取り扱い)に従うこと。

やること:
- Issueの依頼とやり取り(特にオーナーの発言と、最後の呼び出しコメントの指示)を読み、必要なファイルの変更を作る
- 決定済みの事項(docs/design.md・docs/decisions.md)と矛盾させない。依頼が決定と矛盾するなら、変更せずに理由を書く
- 依頼されていない変更はしない。既存の文章の書き方・見出しの形をまねる
- .github/ の中のファイルは変更できない。変更できるのは最大 {MAX_FILES} ファイル

返答の形式(この形以外のことは書かない。各マーカーは行の先頭に単独で書く):
<<<TITLE>>>
(PRのタイトル。日本語で1行)
<<<SUMMARY>>>
(PRの説明。何を・なぜ変えたか。オーナーに決めてほしいことがあれば最後に書く)
<<<FILE: 相対パス>>>
(そのファイルの新しい中身の全文。省略や「…」は禁止。変更しない部分もすべて書く)
<<<END FILE>>>
(変更するファイルの数だけ FILE〜END FILE を繰り返す。新しいファイルも同じ形)
<<<DELETE: 相対パス>>>
(削除するファイルがあるときだけ。1行で)

依頼があいまいで変更を作れないとき、または変更すべきでないときは、FILE を1つも書かず、
SUMMARY に理由やオーナーへの質問を書く。"""

MARKER = re.compile(r"^<<<(TITLE|SUMMARY|FILE: (.+?)|END FILE|DELETE: (.+?))>>>[ \t]*$", re.M)


class Plan:
    def __init__(self, title: str = "", summary: str = "", files: dict[str, str] | None = None, deletes: list[str] | None = None):
        self.title, self.summary = title, summary
        self.files = files or {}
        self.deletes = deletes or []


def model_spec(env: dict) -> tuple[str, str | None]:
    return split_effort((env.get("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL)


def extra_request(comment: str) -> str:
    """呼び出しコメントの「@gemini-cli /pr」より後ろを、追加の指示として取り出す。"""
    text = (comment or "").strip()
    if text.startswith(TRIGGER):
        text = text[len(TRIGGER):]
    return text.split("\n---\n")[0].strip()


def parse_reply(text: str) -> Plan:
    """Geminiの返答をマーカーで区切って読む。形が崩れていたら MeetingError。"""
    text = re.sub(r"^```[^\n]*\n|\n```\s*$", "", text.strip())   # 全体をコードブロックで囲んでくることがある
    marks = list(MARKER.finditer(text))
    if not marks:
        raise MeetingError("Geminiの返答が決めた形式になっていません")
    plan = Plan()
    open_file: str | None = None
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        kind = m.group(1)
        if kind == "TITLE":
            plan.title = body.strip().splitlines()[0].strip() if body.strip() else ""
        elif kind == "SUMMARY":
            plan.summary = body.strip()
        elif kind.startswith("FILE: "):
            open_file = m.group(2).strip()
            content = body[1:] if body.startswith("\n") else body
            if i + 1 >= len(marks) or marks[i + 1].group(1) != "END FILE":
                raise MeetingError(f"{open_file} の終わり(<<<END FILE>>>)がありません。返答が途中で切れた可能性があります")
            if content and not content.endswith("\n"):
                content += "\n"
            plan.files[open_file] = content
        elif kind == "END FILE":
            if open_file is None:
                raise MeetingError("<<<END FILE>>> の前に <<<FILE: …>>> がありません")
            open_file = None
        else:
            plan.deletes.append(m.group(3).strip())
    if not plan.title:
        plan.title = "Geminiによる変更"
    return plan


def safe_path(root: Path, rel: str) -> Path:
    """書き込み先がリポジトリの中で、.git / .github の外であることを確かめる。"""
    rel = rel.strip().strip("`").replace("\\", "/")
    pure = PurePosixPath(rel)
    if not rel or pure.is_absolute() or any(part in ("..", "") for part in pure.parts) or rel.startswith("./"):
        raise MeetingError(f"使えないパスです: {rel!r}")
    if pure.parts[0] in BLOCKED_DIRS or ".git" in pure.parts:
        raise MeetingError(f"{rel} は変更できません(.git / .github の中)")
    path = (root / pure).resolve()
    if root.resolve() not in path.parents:
        raise MeetingError(f"リポジトリの外は変更できません: {rel!r}")
    return path


def apply_plan(plan: Plan, root: Path = ROOT) -> list[str]:
    """変更を作業ツリーに書き込む。先に全部のパスを確かめてから書く(途中で止まって半端に残らないように)。"""
    if len(plan.files) + len(plan.deletes) > MAX_FILES:
        raise MeetingError(f"変更するファイルが多すぎます({len(plan.files) + len(plan.deletes)} 個。上限 {MAX_FILES})")
    writes = {rel: safe_path(root, rel) for rel in plan.files}
    deletes = {rel: safe_path(root, rel) for rel in plan.deletes}
    for rel, path in deletes.items():
        if not path.is_file():
            raise MeetingError(f"削除するファイルがありません: {rel}")
    touched = []
    for rel, path in writes.items():
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        if old == plan.files[rel]:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.files[rel], encoding="utf-8")
        touched.append(rel)
    for rel, path in deletes.items():
        path.unlink()
        touched.append(rel)
    return touched


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout
        return [line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*")
                      if p.is_file() and ".git" not in p.relative_to(root).parts)


def pick_context(files: list[str], hint: str) -> list[str]:
    """中身を渡すファイルを選ぶ: ルールと企画の文書 → Issueに名前が出てくるファイル → docs の残り。"""
    first = ["AGENTS.md", "docs/design.md", "docs/decisions.md", "README.md"]
    named = [f for f in files if f in hint or (PurePosixPath(f).name in hint and len(PurePosixPath(f).name) > 4)]
    docs = [f for f in files if f.startswith("docs/")]
    picked: list[str] = []
    for f in first + named + docs:
        if f in files and f not in picked:
            picked.append(f)
    return picked


def file_context(root: Path, files: list[str], picked: list[str]) -> str:
    parts, used = [], 0
    for rel in picked:
        if PurePosixPath(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > FILE_LIMIT or used + len(text) > CONTEXT_BUDGET:
            parts.append(f"<<<CONTENT: {rel}>>>\n(大きいので省略。このファイルは変更しないこと)\n<<<END CONTENT>>>")
            continue
        used += len(text)
        parts.append(f"<<<CONTENT: {rel}>>>\n{text}\n<<<END CONTENT>>>")
    tree = "\n".join(files)
    return f"## リポジトリのファイル一覧\n\n{tree}\n\n## ファイルの中身(現在の main)\n\n" + "\n\n".join(parts)


def thread_text(issue: dict, comments: list[dict]) -> str:
    head = f"# Issue #{issue.get('number')} {issue.get('title', '')}\n\n{(issue.get('body') or '(本文なし)').strip()}"
    lines = []
    for c in comments:
        who = (c.get("user") or {}).get("login", "?")
        role = c.get("author_association", "")
        lines.append(f"--- {who}" + ("(オーナー)" if role == "OWNER" else "") + f"\n{(c.get('body') or '').strip()}")
    while lines and len(head) + sum(len(x) for x in lines) > THREAD_BUDGET:
        lines.pop(0)
    return head + ("\n\n## コメント\n\n" + "\n\n".join(lines) if lines else "")


def build_prompts(issue: dict, comments: list[dict], extra: str, root: Path = ROOT) -> tuple[str, str]:
    agents = (root / "AGENTS.md").read_text(encoding="utf-8") if (root / "AGENTS.md").exists() else "(AGENTS.md はありません)"
    system = f"# AGENTS.md(このリポジトリの共通ルール)\n\n{agents}\n\n# 今回のルール\n\n{PR_RULES}"
    thread = thread_text(issue, comments)
    files = tracked_files(root)
    context = file_context(root, files, pick_context(files, thread + "\n" + extra))
    user = (f"{context}\n\n# 依頼のIssue\n\n{thread}\n\n"
            + f"# 呼び出しコメントの追加の指示\n\n{extra or '(なし。Issueの依頼どおりに)'}\n")
    return system, user


def write_outputs(env: dict, changed: bool, plan: Plan | None = None, number: int = 0, model: str = "") -> None:
    out = env.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
    if not changed or plan is None:
        return
    folder = Path(env.get("RUNNER_TEMP") or "/tmp") / "gemini_pr"
    folder.mkdir(parents=True, exist_ok=True)
    title = plan.title.replace("\n", " ").strip()[:200]
    (folder / "commit_msg.txt").write_text(f"{title}\n\nRefs #{number}\n", encoding="utf-8")
    body = f"Issue #{number} の依頼をもとに、Gemini(`{model}`)が作った変更です。\n\n{plan.summary}"
    (folder / "pr_body.md").write_text(body[:COMMENT_LIMIT - len(FOOTER)] + FOOTER, encoding="utf-8")


def run(env: dict, github: GitHub, models: Models, root: Path = ROOT, log=print) -> list[str]:
    try:
        number = int(env.get("ISSUE_NUMBER", ""))
    except ValueError as error:
        raise MeetingError("ISSUE_NUMBER がありません") from error
    model, effort = model_spec(env)
    issue = github.issue(number)
    comments = github.comments(number)
    system, user = build_prompts(issue, comments, extra_request(env.get("COMMENT_BODY", "")), root)
    log(f"Issue #{number} の変更を作る: モデル {model}" + (f" @{effort}" if effort else "") + f"、入力 約{len(system) + len(user)} 文字")
    reply = models.chat(model, system, user, **({"effort": effort} if effort else {}))
    plan = parse_reply(reply)
    touched = apply_plan(plan, root)
    if not touched:
        write_outputs(env, False)
        note = plan.summary or "(説明なし)"
        github.comment(number, f"Geminiは変更を作りませんでした。Geminiの説明:\n\n{note}"[:COMMENT_LIMIT - len(FOOTER)] + FOOTER)
        log("変更なし")
        return []
    write_outputs(env, True, plan, number, model)
    log("変更したファイル: " + ", ".join(touched))
    return touched


def main() -> int:
    env = dict(os.environ)
    try:
        github = GitHub(env.get("GITHUB_TOKEN", ""), env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_API_URL", "https://api.github.com"))
        gemini = PROVIDERS["gemini"]
        key = env.get(gemini["key"], "")
        if not key:
            raise MeetingError("GEMINI_API_KEY を、リポジトリの Secrets に登録してください")
        models = Models(gemini["base"], key, interval=0, key_name=gemini["key"])
        run(env, github, models)
        return 0
    except MeetingError as error:
        print(f"::error::{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
