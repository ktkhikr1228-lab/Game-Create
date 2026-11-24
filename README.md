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
| Variable | `AI_SUMMARY_EFFORT` | 任意。まとめ役のときだけ使うエフォート(例：`high`) |
| Secret | `GEMINI_API_KEY` など | 任意。ゲートウェイを通さず公式APIを使うときの、その会社のキー(`GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `MISTRAL_API_KEY`) |
| Variable | `AI_CALL_INTERVAL` | 任意。呼び出しの間に空ける秒数(既定15)。混雑で参加できないモデルが多いときに増やす |
| Variable | `AI_STREAM` | 任意。`0` にすると、返答を少しずつ受け取る方式(ストリーミング)を使わない。既定は使う |

- 公式APIを直接使う：`AI_MODELS` のモデルIDの前に会社名を付ける。例：`Claude=claude-opus-5.5,GPT=gpt-6.1-sol,Gemini=gemini:gemini-4-neon`(GeminiだけGoogleの公式API、ほかはゲートウェイ)。付けられるのは `gemini:` `openai:` `anthropic:` `mistral:`。全員を公式APIにすれば、`AI_API_BASE` / `AI_API_KEY` は要らない
- Geminiの公式APIのキーは Google AI Studio で発行する(AI Pro のログインは使わない)
- 考える量(エフォート)：`AI_MODELS` のモデルIDの後ろに `@low` `@medium` `@high` などを付ける。例：`Claude=claude-opus-5.5@medium,GPT=gpt-6.1-sol@medium,Gemini=gemini:gemini-4-argon@medium`。下げると、費用(Geminiの公式API)と時間切れ(ゲートウェイの504など)が減る。まとめ役だけ上げたいときは `AI_SUMMARY_EFFORT=high`。付けなければモデルの既定のまま。エフォートに対応していない入口だとエラーになるので、そのときはそのモデルだけ外す
- 仕組み：`.github/workflows/ai-meeting.yml` と `tools/ai_meeting.py`(Python標準ライブラリだけ)
- AIはIssueにコメントするだけで、ファイルは変更しない(ワークフローの権限も読み取りのみ)
- キーが受け付けられないときは、Issueを作らずに止まる
- 予備のモデル：`AI_MODELS` で `|` の後ろに書くと、前のモデルが答えられないときに使う。例：`Claude=claude-opus-5.5|anthropic:claude-opus-5-5`(ゲートウェイがだめなら公式API。予備の会社のキーも Secrets に要る)
- 返答はストリーミングで受け取る(返答が全部できるまで黙って待つと、ゲートウェイが待ちきれずに504を返すため)。ストリーミングを受け付けない入口なら、自動で使わない方式に切り替える
- 混雑(429)や時間切れ(504など)のときは、15秒・45秒あけて最大2回までやり直す。失敗の理由(制限の種類・残り回数・エラー本文の頭。キーは伏せ字)をログに出す
- それでも答えられなかった席は、その周のほかの席のあとに1回だけ頼み直す。それでもだめなら、その会議ではもう呼ばない(待ち時間を無駄にしない)
- 参加できなかった回は、まとめに1行だけ残す(失敗のコメントは書かない)
- 1周目に誰も答えられなければ、Issueを作らずに止まる。少し時間をおいて開き直す

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
PRに `@gemini-cli /review` とコメントすると、GeminiがそのPRをレビューして、結果を1つのコメントで返す。

- 呼べるのはオーナーだけ。ほかの人が書いても動かない
- Geminiには道具を持たせない。差分と企画の資料を渡して、返ってきた文章をワークフローが投稿するだけ（コードの変更やpushはしない）
- `/review` の後ろに、見てほしい点を書ける。例：`@gemini-cli /review 崩落の計算が重くないか見て`
- 準備：Settings → Secrets に `GEMINI_API_KEY`（Google AI Studio で発行したAPIキー）を登録する。AI会議でGeminiを公式APIにするときと同じキーでよい
- 任意：Variables の `GEMINI_MODEL` で使うモデルを指定できる（空なら `gemini-4-argon`。`gemini-4-argon@medium` のようにエフォートも付けられる）
- 費用：APIの従量課金。AI Studio で前払い（最低 $5）が必要で、Google Cloud のクレジットはそのあとに使える
- 仕組み：`.github/workflows/gemini-review.yml` と `tools/pr_review.py`

### Gemini PR作成（GitHub Actions）
Issueに `@gemini-cli /pr` とコメントすると、GeminiがそのIssueの依頼どおりにファイルを変更して、PRを作る。

- 呼べるのはオーナーだけ。PRは自動ではマージしない（取り込むかはオーナーが決める）
- Geminiには道具を持たせない。Issueのやり取りと企画の資料・関係するファイルを渡し、返ってきた「ファイルの新しい全文」をスクリプトが書き込む。コミット・push・PR作成はワークフローが行う
- `/pr` の後ろに、追加の指示を書ける。例：`@gemini-cli /pr 図鑑の文だけ直して`
- 変更できないもの：`.github/` の中（GITHUB_TOKEN にワークフローを変える権限がないため）。1回に変更できるのは30ファイルまで
- Geminiが変更を作らなかったとき（依頼があいまい・決定と矛盾するなど）は、理由や質問をIssueにコメントする
- 準備：PRレビューと同じ `GEMINI_API_KEY` に加えて、Settings → Actions → General → Workflow permissions の「Allow GitHub Actions to create and approve pull requests」をオンにする
- 注意：このワークフローが作ったPRでは、ほかのワークフローは自動で動かない。レビューはPRに `@gemini-cli /review` とコメントして頼む
- 仕組み：`.github/workflows/gemini-pr.yml` と `tools/gemini_pr.py`

### ChatGPT（チャットで読むだけ）
- ChatGPT の設定でGitHubを接続し、このrepoへのアクセスを許可
