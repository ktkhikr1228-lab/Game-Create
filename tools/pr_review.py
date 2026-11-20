"""PRを Gemini にレビューしてもらい、結果をPRにコメントする(.github/workflows/gemini-review.yml から呼ばれる)。

- PRのタイトル・説明・変更差分は、このスクリプトがGitHubのAPIで取ってきて、まとめてGeminiに渡す
- Geminiには道具(ファイル操作・コマンド・GitHubへの書き込み)を一切持たせない。返ってきた文章を投稿するだけ
- AGENTS.md と docs/design.md・docs/decisions.md を前提として渡す(企画の決定と矛盾していないかも見てもらう)

使うもの(すべて環境変数):
  GITHUB_TOKEN, GITHUB_REPOSITORY   … GitHub Actions が自動で渡す
  PR_NUMBER                         … レビューするPRの番号
  GEMINI_API_KEY                    … Google AI Studio のAPIキー(Secrets)
  GEMINI_MODEL                      … 使うモデル(Variables。既定 gemini-4-argon)。後ろに「@medium」などでエフォート指定可
  REVIEW_REQUEST                    … 呼び出したコメントの本文(「/review」の後ろの追加の指示を読む)
Python標準ライブラリだけで動く。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_meeting import COMMENT_LIMIT, PROVIDERS, GitHub, MeetingError, Models, split_effort  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-4-argon"
PATCH_LIMIT = 60000      # 1ファイルの差分の上限(文字)。超えたぶんは省く
DIFF_BUDGET = 400000     # 差分全体の上限(文字)
TRIGGER = "@gemini-cli /review"
FOOTER = "\n\n---\n<sub>Geminiによる自動レビューです。取り込むかどうかはオーナーが決めます。</sub>"

REVIEW_RULES = """あなたはこのリポジトリのPRをレビューする参加者(Gemini)です。上のルールに従ってください。

レビューの書き方:
- 日本語。くだけた口調でよいが、キャラ口調は不要。技術用語は言い換えない
- 最初に「### 結論」として、このPRの評価を2〜3文で書く(承認・却下の判定はしない。決めるのはオーナー)
- 次に「### 指摘」として、問題を重要な順に箇条書きにする。それぞれ先頭に [重大] [中] [軽] のどれかを付け、
  場所(ファイル名と、差分の + 側の行の内容や関数名)と、なぜ問題か、どう直すとよいかを書く
- コードの指摘は、バグ・安全性・壊れやすさを優先する。好みの問題は [軽] にとどめる
- 企画や設定の文章の変更なら、docs/design.md と docs/decisions.md の決定と矛盾していないかを見る
- 現実の鉱物・冶金の話とゲーム独自の効果の話が出るときは、【現】【ゲ】を分けて書く。不確かなことは「不確か」と書く
- 差分に含まれていない部分を推測で断定しない。差分が途中で省かれているときは、その旨を書く
- 問題がなければ、無理に指摘を作らず「大きな問題なし」と書く
- 長さは画面2つ分程度まで"""


def model_spec(env: dict) -> tuple[str, str | None]:
    return split_effort((env.get("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL)


def extra_request(comment: str) -> str:
    """呼び出しコメントの「@gemini-cli /review」より後ろを、追加の指示として取り出す。"""
    text = (comment or "").strip()
    if text.startswith(TRIGGER):
        text = text[len(TRIGGER):]
    return text.split("\n---\n")[0].strip()   # 自動で付く署名などは除く


def format_diff(files: list[dict]) -> str:
    parts, used = [], 0
    for f in files:
        name = f.get("filename", "?")
        head = f"=== {name} ({f.get('status', '?')}, +{f.get('additions', 0)} -{f.get('deletions', 0)}) ==="
        patch = f.get("patch")
        if patch is None:
            body = "(差分なし: バイナリか、GitHubが表示しない大きさのファイル)"
        elif used >= DIFF_BUDGET:
            body = "(差分の上限に達したので省略)"
        else:
            room = min(PATCH_LIMIT, DIFF_BUDGET - used)
            body = patch if len(patch) <= room else patch[:room] + f"\n…(ここから先 {len(patch) - room} 文字を省略)"
            used += len(body)
        parts.append(f"{head}\n{body}")
    return "\n\n".join(parts)


def read_doc(root: Path, rel: str) -> str:
    path = root / rel
    return path.read_text(encoding="utf-8") if path.exists() else f"({rel} はありません)"


def build_prompts(pr: dict, diff: str, extra: str, root: Path = ROOT) -> tuple[str, str]:
    system = (f"# AGENTS.md(このリポジトリの共通ルール)\n\n{read_doc(root, 'AGENTS.md')}\n\n"
              f"# レビューのルール\n\n{REVIEW_RULES}")
    user = (f"# 前提: docs/design.md(企画の正本)\n\n{read_doc(root, 'docs/design.md')}\n\n"
            f"# 前提: docs/decisions.md(決定の経緯)\n\n{read_doc(root, 'docs/decisions.md')}\n\n"
            f"# レビューするPR: #{pr.get('number')} {pr.get('title', '')}\n\n"
            f"## PRの説明\n\n{(pr.get('body') or '(説明なし)').strip()}\n\n"
            + (f"## オーナーからの追加の指示\n\n{extra}\n\n" if extra else "")
            + f"## 変更差分\n\n{diff}")
    return system, user


def pr_files(github: GitHub, number: int) -> list[dict]:
    files: list[dict] = []
    for page in range(1, 11):   # 最大1000ファイル
        batch = github.call("GET", f"/pulls/{number}/files?per_page=100&page={page}") or []
        files.extend(batch)
        if len(batch) < 100:
            break
    return files


def run(env: dict, github: GitHub, models: Models, root: Path = ROOT, log=print) -> str:
    try:
        number = int(env.get("PR_NUMBER", ""))
    except ValueError as error:
        raise MeetingError("PR_NUMBER がありません") from error
    model, effort = model_spec(env)
    pr = github.call("GET", f"/pulls/{number}")
    diff = format_diff(pr_files(github, number))
    system, user = build_prompts(pr, diff, extra_request(env.get("REVIEW_REQUEST", "")), root)
    log(f"PR #{number} をレビュー: モデル {model}" + (f" @{effort}" if effort else "") + f"、入力 約{len(system) + len(user)} 文字")
    try:
        review = models.chat(model, system, user, **({"effort": effort} if effort else {}))
    except MeetingError as error:
        github.comment(number, f"Geminiのレビューに失敗しました({error})。Actions のログを見てください。{FOOTER}")
        raise
    header = f"## Geminiレビュー(`{model}`)\n\n"
    body = header + review
    if len(body) + len(FOOTER) > COMMENT_LIMIT:
        body = body[:COMMENT_LIMIT - len(FOOTER) - 20] + "\n\n…(長すぎるので省略)"
    github.comment(number, body + FOOTER)
    log("レビューを投稿しました")
    return body


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
