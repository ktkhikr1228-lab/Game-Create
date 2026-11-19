# mining-craft-game（仮）

実在の鉱物を掘って、精錬して、配合比を変えて自作合金を作る2D採掘・クラフトゲームの企画repo。

## 構成
| パス | 中身 |
|---|---|
| `docs/design.md` | 企画の正本（コンセプト・方針・鉱物候補・未決事項） |
| `docs/decisions.md` | 決定ログ（いつ・何を・なぜ決めたか） |
| `AGENTS.md` | AI参加者の共通ルール（Codex / ChatGPT が読む） |
| `CLAUDE.md` | AGENTS.md を読み込むだけ（Claude が読む） |
| `GEMINI.md` | AGENTS.md を読み込むだけ（Gemini CLI が読む） |
| `.github/ISSUE_TEMPLATE/meeting.md` | 会議用Issueテンプレート |
| `.github/workflows/claude.yml` | Issueで @claude を呼べるようにするAction |

## 会議の回し方
1. 「会議」テンプレートでIssueを立てる
2. 本文末尾、またはコメントで `@claude` と `@codex` を呼ぶ
3. 2周目は「相手の意見に反論・補足して」とコメントで再度呼ぶ
4. 決めたことを自分で `docs/decisions.md` → `docs/design.md` に反映してIssueを閉じる

## AI会議(AI同士の自動会議)
Claude・GPT・Geminiの3モデルが、Issue上で順に意見を書き、2周目で反論・補足し、最後にまとめ役が「オーナーに決めてほしいこと」を整理する。オーナーが開いたときだけ動く(自動では動かない)。

### 開き方
1. Actions タブ →「AI会議」→「Run workflow」
2. 必要なら入力する(すべて空でも開ける)
   - 議題：空なら「議題」ラベルの付いた開いたIssue → `docs/design.md` の未決事項のうち、まだ会議していないもの、の順で自動で選ぶ
   - Issue番号：続きの会議をするとき
   - 周回数：1〜3(既定2)
3. 数分後、`AI会議` ラベルのIssueにコメントが並ぶ。決めたことは、今までどおり自分で `docs/decisions.md` → `docs/design.md` に反映して閉じる

### 準備(最初の1回)
Settings → Secrets and variables → Actions で登録する。
| 種類 | 名前 | 中身 |
|---|---|---|
| Secret | `AI_API_BASE` | OpenAI互換APIの入口(`…/v1` まで) |
| Secret | `AI_API_KEY` | APIキー |
| Variable | `AI_KEY_HEADER` | キーを渡すヘッダー名(`Authorization` 以外のときだけ) |
| Variable | `AI_MODELS` | 任意。`表示名=モデルID` をカンマ区切り(省略時は Claude / GPT / Gemini) |
| Secret | `GEMINI_API_KEY` など | 任意。ゲートウェイを通さず公式APIを使うときの、その会社のキー(`GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `MISTRAL_API_KEY`) |
| Variable | `AI_CALL_INTERVAL` | 任意。呼び出しの間に空ける秒数(既定15)。混雑で参加できないモデルが多いときに増やす |

- 公式APIを直接使う：`AI_MODELS` のモデルIDの前に会社名を付ける。例：`Claude=claude-opus-5.5,GPT=gpt-6.1-sol,Gemini=gemini:gemini-4-neon`(GeminiだけGoogleの公式API、ほかはゲートウェイ)。付けられるのは `gemini:` `openai:` `anthropic:` `mistral:`。全員を公式APIにすれば、`AI_API_BASE` / `AI_API_KEY` は要らない
- Geminiの公式APIのキーは Google AI Studio で発行する(AI Pro のログインは使わない)
- 仕組み：`.github/workflows/ai-meeting.yml` と `tools/ai_meeting.py`(Python標準ライブラリだけ)
- AIはIssueにコメントするだけで、ファイルは変更しない(ワークフローの権限も読み取りのみ)
- キーが受け付けられないときは、Issueを作らずに止まる
- 混雑(429)や時間切れ(504など)のときは、20〜120秒あけて最大4回までやり直す
- それでも答えられなかったモデルは飛ばし、まとめに「参加できなかった回」として1行だけ残す(失敗のコメントは書かない)
- 1周目に答えられたモデルが2つ未満なら、Issueを作らずに止まる。少し時間をおいて開き直す

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

### Gemini
Geminiの席。サブスク（Google AI Pro）とAPIキーは、使える場所が違うので注意。

- **ルールの読み込み**：Gemini CLI は `GEMINI.md` を読む。`GEMINI.md` は `AGENTS.md` を読み込むだけのファイルなので、ルールは `AGENTS.md` だけを直せばよい
- **会議での使い方（手動）**：Geminiアプリに、Issueの本文と `AGENTS.md` を貼って答えをもらい、「Gemini の意見（オーナーが転記）」としてIssueにコメントする
- **公式の道具（サブスクのログインでOK）**：Geminiアプリ、Gemini CLI、Antigravity、Jules
- **それ以外の道具やAI会議から呼ぶとき**：Google AI Studio で発行した **APIキー** を使う（AI Pro の毎月のGoogle Cloudクレジットが使える。最初に前払いが必要）
- **やってはいけないこと**：AI Pro のログイン（OAuth）や、Gemini CLI の認証ファイルを、ほかのツールや GitHub の Secrets に渡すこと。Googleの規約違反になり、アカウントが止められることがある

### Gemini PRレビュー（GitHub Actions）
PRに `@gemini-cli /review` とコメントすると、GeminiがそのPRをレビューしてコメントを付ける。

- 呼べるのはオーナーだけ。ほかの人が書いても動かない
- Geminiができるのは、PRを読むこととレビューコメントを書くことだけ（コードの変更やpushはしない）
- `/review` の後ろに、見てほしい点を書ける。例：`@gemini-cli /review 崩落の計算が重くないか見て`
- 準備：Settings → Secrets に `GEMINI_API_KEY`（Google AI Studio で発行したAPIキー）を登録する。AI会議でGeminiを公式APIにするときと同じキーでよい
- 任意：Variables の `GEMINI_MODEL` で使うモデルを指定できる
- 費用：APIの従量課金（AI Pro の毎月のGoogle Cloudクレジットが使える）
- 仕組み：`.github/workflows/gemini-review.yml`（Googleの公式の run-gemini-cli の例を元に、レビュー専用・オーナー限定にしたもの）

### ChatGPT（チャットで読むだけ）
- ChatGPT の設定でGitHubを接続し、このrepoへのアクセスを許可
