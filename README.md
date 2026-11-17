# mining-craft-game（仮）

実在の鉱物を掘って、精錬して、配合比を変えて自作合金を作る2D採掘・クラフトゲームの企画repo。

## 構成
| パス | 中身 |
|---|---|
| `docs/design.md` | 企画の正本（コンセプト・方針・鉱物候補・未決事項） |
| `docs/decisions.md` | 決定ログ（いつ・何を・なぜ決めたか） |
| `AGENTS.md` | AI参加者の共通ルール（Codex / ChatGPT が読む） |
| `CLAUDE.md` | AGENTS.md を読み込むだけ（Claude が読む） |
| `.github/ISSUE_TEMPLATE/meeting.md` | 会議用Issueテンプレート |
| `.github/workflows/claude.yml` | Issueで @claude を呼べるようにするAction |

## 会議の回し方
1. 「会議」テンプレートでIssueを立てる
2. 本文末尾、またはコメントで `@claude` と `@codex` を呼ぶ
3. 2周目は「相手の意見に反論・補足して」とコメントで再度呼ぶ
4. 決めたことを自分で `docs/decisions.md` → `docs/design.md` に反映してIssueを閉じる

## セットアップ
### Claude（@claude）
1. https://github.com/apps/claude をrepoにインストール
2. repoの Settings → Secrets and variables → Actions に認証情報を登録
   - APIキーを使う場合：`ANTHROPIC_API_KEY`
   - Claudeのサブスクを使う場合：ローカルで `claude setup-token` を実行して得たトークンを `CLAUDE_CODE_OAUTH_TOKEN` として登録
3. `.github/workflows/claude.yml` で使う方の行を有効にする

### Codex（@codex）
1. ChatGPT の Codex でGitHubを接続（ChatGPT Codex Connector をrepoに許可）
2. Codex cloud の環境をこのrepoで作成
3. Issue / PR のコメントで `@codex` を呼ぶ

### ChatGPT（チャットで読むだけ）
- ChatGPT の設定でGitHubを接続し、このrepoへのアクセスを許可
