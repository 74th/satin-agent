# Proposal

## Why

satin-agentには、実データの分析と外部MCPツールの呼び出しを通しで試せるユースケースがない。silk-agentの`add-bigquery-analysis-usecase`と同じ題材で確認できるようにする。BigQueryの公開データを分析するエージェントと、認証付きのEcho MCPを使う。確認する経路は次のとおり。

- Skillと、Sandbox内のスクリプトによるクラウドAPIの利用
- Managed AgentsからクラスタのMCPを呼ぶ経路
- GKEへの配備

satin-agentでは(A)のMCP呼び出しがAnthropic側から来るため、silk-agentのようにClusterIPのMCPへ直接はつなげない。代わりにMCP tunnels（Proxy）を使う。

## What Changes

- `usecases/bigquery-analysis/`を新たに作る。既定の配備と同じnamespace・Self-hosted Environment・Slackアプリを使い、配備を入れ替えて動かす。Agent、アセットの配布先、Sandboxのイメージ、KSAはユースケース専用にする。中身は次のとおり。
  - アセット（`CLAUDE.md`、Skill、`mcp.json`）
  - satin設定ファイル
  - BigQueryクライアントを足したランタイムイメージ
  - Kustomizeのoverlay
  - Terraform root
  - 運用手順
- Skillに、固定のSQLと実行スクリプトを付ける。SQLはGoogle Trendsの日本の急上昇検索語で、東京日付の直近2日、最大10行を取る。会話ごとのSandboxでこのSQLを実行し、日付・順位・検索語を根拠に日本語で説明させる。
- BigQuery用のGoogle Cloud Service Account（GSA）を、ユースケース専用のTerraform rootで作る。中身はBigQueryのジョブ実行権限、アセットのバケットの読み取り、専用Kubernetes ServiceAccount（KSA）からのWorkload Identityでのなりすまし許可。このユースケースのSandboxだけがこのKSAを使う。外向き通信には`bigquery.googleapis.com`を加える。
- FastMCPのEcho MCPサーバをKubernetesに配備する。`X_API_KEY`ヘッダーの認証を必須にする。
- MCP tunnelsのスタック（`mcp-proxy`と`cloudflared`）を、Echo MCPとは別のマニフェストとして配備する手順を作る。AnthropicからのMCPの要求は、このトンネルを経由してEcho MCPに届く。
- 配布スクリプトを変更する。`mcp.json`のMCPサーバを、ツールとして使える形（`mcp_toolset`）でAgentに付ける。
- Slack Botを変更する。設定したVaultをセッションの作成時に付け、MCPサーバへの資格情報を渡す。
- 単体の検証と、GKE・Slackを通した検証を行う。GKE・Slackでは、SQLの実行、Echo、認証の拒否を確認する。

## Capabilities

### New Capabilities

- `bigquery-analysis-usecase`: Skillに従ってBigQueryの公開データを分析し、根拠付きで結果を示す。失敗を結果と区別する。
- `echo-mcp-service`: `X_API_KEY`で認証するEcho MCPをクラスタ内に置き、MCP tunnels経由でManaged Agentsから使う。疎通を検証する手順も含む。
- `sandbox-cloud-access`: ユースケースのSandboxに限り、Terraformで管理したGSAとWorkload Identityを使い、Google Cloud APIを呼べるようにする。宛先は明示して許可する。

### Modified Capabilities

- `shared-agent-assets`: `mcp.json`のMCPサーバを、ツールとして使える形でAgentに登録する要件を追加する。
- `slack-bot`: 設定したVaultをセッションの作成時に付ける要件を追加する。

## Impact

- 新規: `usecases/bigquery-analysis/`。内訳はアセット、Dockerfile、satin設定、Kustomize overlay、Echo MCPのコード・イメージ・マニフェスト、MCP tunnelsのマニフェスト、Terraform、手順書、テスト。
- 変更:
  - `src/satin_agent/assets/cma.py`: `mcp_toolset`を付ける。
  - `src/satin_agent/slack/sessions.py`と`commands.py`: `vault_ids`を渡す。
  - `src/satin_agent/config.py`まわり: Vaultの設定を読む。
  - `scripts/deploy.sh`: overlayと状態ファイルを引数で選べるようにする。
- 既定の動作は変えない（Vaultの設定がなければセッションにVaultを付けず、`mcp.json`が空ならAgentのツールも変わらない）。`assets/`、`k8s/overlays/dev`、`terraform/`、メタデータサーバとFQDNのPolicyの生成はそのまま使う。
- 外部の前提:
  - MCP tunnelsはresearch previewで、利用申請が必要。
  - トンネルトークンとサーバ証明書を用意する。
  - Managed AgentsのVaultに`static_bearer`の資格情報を作る。
- プロジェクトID、イメージのdigest、APIキー、トンネルトークンは、リポジトリに固定しない。
