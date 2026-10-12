# Proposal

## Why

Claudeのエージェントのツール実行を、会話（セッション）ごとに起動するGKE Agent Sandboxへ載せる方法が2通りある。(A) Claude Managed Agentsのself-hosted Environment、(B) Claude Codeのself-hosted Environment（claude.aiの画面から選ぶ）である。どちらも個別には動作を確認できている（`test-llm-agent/20260922-claude-managed-agents`、`silk-agent`）。ただしsilk-agentは、エージェントループをBotのプロセスで動かし、ツールをsandboxd経由でSandboxへ送る構成だった。(A)と(B)はどちらもエージェントループをAnthropic側に置き、ツール実行（bash / ファイル操作）だけを自前のワーカーで行う。そのため、ワーカー自体をセッション専用のSandboxで動かせば、sandboxdなしでツール実行だけをSandboxに閉じ込められる。この2パターンを、コンテナイメージ・Skills・CLAUDE.md・`startup.sh`を共通にした1つのフレームワークにする。あわせて(A)をSlack Botとして動かし、silk-agentと同じ水準の中断・再起動のケースに耐えられるかを検証する。

## What Changes

- 共通のSandboxランタイムイメージを追加する。(A)の`EnvironmentWorker`と(B)の`claude self-hosted-runner`の両方を含め、起動のたびにカスタマイズ用の`startup.sh`を実行してから、モードに応じたワーカーを起動する。
- セッションごとのSandboxのライフサイクル管理を追加する。セッションIDをキーに、GKE Agent SandboxのSandboxを作成・再開・停止・削除する。ワークスペースはセッション専用のPVCに置き、idleの後に別のPodで再開しても引き継ぐ。
- (A)用のオーケストレータを追加する。Managed Agentsのself-hosted Environmentのwork queueをポーリングし、work itemごとに該当セッションのSandboxを起動して、その中で`EnvironmentWorker.handle_item()`を実行させる。
- (B)用のオーケストレータを追加する。`claude self-hosted-runner orchestrator`の`spawn-runner`フックから、セッションごとにSandboxを起動し、その中で1セッション専用のrunnerを動かす。
- 共有アセット（Skills、CLAUDE.md、`startup.sh`）の配布スクリプトを追加する。指定ディレクトリの複数のSkillsを(A)のSkills APIへアップロードしてAgentへ付け、CLAUDE.mdを(A)のAgentのシステムプロンプトに反映する。同じものをGCSにもアップロードする。(B)では、Sandboxの起動時にGCSから取り出して配置する。
- (A)のManaged Agentsを使うSlack Botを追加する。スレッドを1つのManaged Agentsセッションとして扱い、進捗表示、停止語、実行中の追加指示、Bot・オーケストレータ・Sandboxの再起動からの復帰を扱う。
- (A)のSlack Botに、silk-agentのガード類を移植する。ルールのフィルタ、Gemini 3.8 Flashの入力ガード、Model Armorの出力検査、bashの実行前確認（ディレクトリ内の全ファイルの一括取り出しを拒否）、監査用のフック。(B)には移植しない。
- 中断・再起動のケースの検証シナリオと、手順書を追加する（silk-agentで確かめたケースと同じ水準）。エージェントの応答の判定には、文字列一致に加えてJev（TypeSafe）を使う。

## Capabilities

### New Capabilities

- `shared-agent-assets`: Skills・CLAUDE.md・`startup.sh`を1か所で管理し、(A)のManaged Agents（Skills API、Agentのシステムプロンプト）とGCSへ配布するスクリプト、およびGCS上の配置の約束
- `sandbox-runtime-image`: (A)と(B)で共通のSandboxランタイムイメージ。起動時のGCSからのアセット展開、`startup.sh`の実行、モードごとのワーカーの起動と終了
- `session-sandbox-lifecycle`: セッションIDごとのGKE Agent Sandboxの作成・再開・停止・保持期限後の削除と、ワークスペースPVCの引き継ぎ（(A)と(B)で共通）
- `cma-sandbox-orchestrator`: (A) Managed Agentsのwork queueからwork itemを受け取り、セッションのSandboxでツール実行させるオーケストレータ
- `claude-code-sandbox-orchestrator`: (B) Claude Codeのself-hosted runnerの`spawn-runner`フックから、セッションのSandboxでrunnerを起動するオーケストレータ
- `slack-bot`: (A)のManaged Agentsを使うSlack Bot（スレッドとセッションの対応、進捗表示、停止、追加指示、再起動からの復帰、およびガード類: ルールのフィルタ、入力ガード、出力検査、bashの実行前確認、監査フック）
- `resilience-verification`: 中断・再起動・idle後の再開・保持期限切れ・ガードなどのケースを、(A)と(B)で実際に確かめる検証シナリオと記録。応答の判定にJevを使う

### Modified Capabilities

（なし。新規プロジェクトのため既存のspecはない）

## Impact

- 新規のPythonパッケージ（`satin_agent`）。依存は`anthropic`（Managed Agentsのbeta APIと`EnvironmentWorker`）、`kubernetes`（または`kubernetes_asyncio`）、`google-cloud-storage`、`slack-bolt`、Gemini（Agent Platform）とModel ArmorのGoogle Cloudクライアント。テストのみ`typesafe-sdk`（Jev）
- silk-agentから、使う部分だけを移植する。`gke_session_sandbox`からはSandboxのテンプレート描画・停止と再開・NetworkPolicy・PVC管理を移植し、sandboxdとCommandTrackerは移植しない。`silk_agent`からはガード（`guards/`）、フック（`hooks.py`）、会話の中核（停止語・追加指示・heartbeat）を移植する
- GKE: silk-agentで構築中のGKEクラスタ（Agent Sandbox controller、gVisor）を使う。クラスタ名・リージョン・namespaceなどの情報は、構築後にユーザから受け取る。そこへ、オーケストレータ2種とSlack BotのDeployment、Sandbox操作用のRBACを追加する
- GCP: アセット配布用のGCSバケット、Sandbox Podが読み取るためのWorkload Identity（Sandbox用のKSAとGSA）、Slack BotがGeminiとModel Armorを呼ぶためのWorkload Identity、Model Armorのテンプレート（個人情報のマスクにはDLPテンプレートつきのもの）
- Anthropic: Managed Agentsのself-hosted Environment、Agent、Skills、Vault。Claude Codeのself-hosted Environment（claude.aiで作成し、environment secretを発行する）
- 秘密情報: Anthropic APIキー（`.env`の`CLAUDE_API_KEY`。配布スクリプトとSlack Botのみ）、Managed Agentsのenvironment key（(A)のオーケストレータのみ）、Claude Codeのenvironment secret（(B)のオーケストレータのみ）、Slackのトークン、Jevのトークン（`JEV_API_TOKEN`。テストのみ）。Sandbox Podには組織スコープの資格情報を置かない
- 対象外: (B)のガード類とSlack連携（(B)はclaude.aiの画面から使う）
