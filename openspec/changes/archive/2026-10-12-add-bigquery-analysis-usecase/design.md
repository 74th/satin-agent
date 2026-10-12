# Design

## Context

satin-agentの配備は、設定ファイル（`satin.yaml`）・アセットディレクトリ・Kustomizeのoverlayを1組ずつ持つ。silk-agentのような`usecases/`の読み込みの仕組みはない。関係する現状は次のとおり。

- **配布**: `satin publish --config <yaml> --assets <dir>`は、状態ファイル（`assets.state_file`）とGCSの配布先（`assets.gcs_url`）を設定から読む。そのため、設定を分ければ別のAgent・別のアセットとして配布できる。
- **Sandbox**:
  - KSA（`sandbox.service_account`）、イメージ（`sandbox.image`）、許可するFQDN（`sandbox.allowed_fqdns`）は、既に設定で変えられる。
  - `src/satin_agent/sandbox/policies.py`は、DNS、メタデータサーバ（`169.254.169.254:80`など）、`api.anthropic.com`・`storage.googleapis.com`への送信を常に許可している。
  - 既定のKSA `satin-sandbox`には、`terraform/`がWorkload Identity Federationの直接のprincipalとして、アセットのバケットの読み取りを与えている。
- **MCP**:
  - `assets/mcp.json`の`mcp_servers`は、`agent_config()`でAgentの`mcp_servers`にそのまま入る。ただし`mcp_toolset`はツールに加えていない。
  - (A)のMCPの呼び出し元はAnthropic側のため、クラスタ内のServiceには直接届かない。
  - Botの`SessionsPort.create()`は`agent`・`environment_id`・`metadata`だけを渡し、`vault_ids`は渡していない。
- **MCP tunnels**（research preview）:
  - `mcp-proxy`と`cloudflared`のスタックが外向きにトンネルを張り、`https://<サブドメイン>.<トンネルのドメイン>/<パス>`を`routes`のupstreamへ転送する。
  - トンネルはupstreamを認証しない。
  - Managed AgentsのVaultが`mcp_server_url`ごとに持てるMCPの資格情報は、`static_bearer`と`mcp_oauth`だけである。任意の名前のヘッダー（`X_API_KEY`など）は使えない。
  - Proxyは、既定ではRFC1918のアドレスにしか接続しない。

## Goals / Non-Goals

**Goals:**

- 本体（`src/satin_agent`）の変更は、MCPの登録とVaultの付与だけにする。ユースケースは`usecases/bigquery-analysis/`にまとめる。
- AnthropicからMCP tunnelsを経由して、クラスタ内の認証付きMCPへ届く経路を確かめる。
- BigQueryの権限を、ユースケースのSandboxのKSAだけに与える。

**Non-Goals:**

- 次のものは扱わない。
  - 任意のSQLを実行する汎用の分析基盤
  - 検索語の予測
  - BigQueryの結果の永続的な保存
- (B) Claude CodeからのEcho MCPの利用。BigQueryのSkillは(B)でも動くが、MCPの配線は(A)だけを対象にする。
- 既定の配備とユースケースの配備を、同じクラスタで同時に動かすこと。
- トンネルのトークンと証明書の自動更新（Workload Identity Federationを使うsetupコンポーネント）。

## Decisions

### 1. ユースケースを`usecases/bigquery-analysis/`に置き、配備を入れ替えて動かす

置くものは次のとおり。

```
usecases/bigquery-analysis/
  assets/                 CLAUDE.md, mcp.json, startup.sh, skills/bigquery-trends/{SKILL.md, query.sql, scripts/run_query.py}
  satin.yaml              publish用（state_file / gcs_url / agent.name）
  runtime.Dockerfile      FROM satin-runtime + google-cloud-bigquery
  k8s/overlay/            ../../../../k8s/base + 専用KSA + satin.yaml（sandbox.*, agent.vault_ids）
  k8s/echo-mcp/           Echo MCPのDeployment / Service / NetworkPolicy
  k8s/mcp-tunnel/         MCP tunnelsのvalues.yamlとNetworkPolicy
  echo_mcp/               Echo MCPサーバ（独立したpyproject・テスト・Dockerfile）
  terraform/              GSAとIAM（独立したroot）
  README.md               運用手順
```

ユースケースは専用のものと、既定の配備と共有するものに分ける。

- 専用: Agent（状態ファイル`.satin-state-bigquery.json`）、GCSの配布先（`gs://<bucket>/satin/bigquery-analysis/`）、Sandboxのイメージ、KSA
- 既定の配備と共有: namespace（`satin-system`・`satin-sandboxes`）、Self-hosted Environment、Slackアプリ、Secret

1つのEnvironmentにつなぐオーケストレータは1つにする制約があるため、2つの配備を同時には動かさない。`kubectl apply -k`の対象を入れ替えて切り替え、既定に戻すときは`k8s/overlays/dev`を適用し直す。

`scripts/deploy.sh`は、環境変数`OVERLAY`と`SATIN_STATE`で対象を選べるようにする。状態ファイルに`vault_id`があれば、設定の`__VAULT_ID__`を置き換える。

他の案は次の理由で採らない。

- **namespaceごと別にして並走させる**: Environment・Slackアプリ・Secretも別に必要になり、手順と費用が倍になる。
- **既定の`assets/`に追加する**: 全セッションにBigQueryの権限が及ぶ。

### 2. BigQueryのSkillは固定のSQLとPythonスクリプトにする

SQLはSkillの付属ファイル`query.sql`に置く。

- 依頼のSELECT / GROUP BY / ORDER BY / LIMITの条件を保つ
- 日本で絞る
- `DATE_SUB(CURRENT_DATE("Asia/Tokyo"), INTERVAL 1 DAY)`以降を対象にする

`scripts/run_query.py`は次のように動く。

- `google-cloud-bigquery`とADCで実行する
- ジョブのプロジェクトは環境変数`BIGQUERY_JOB_PROJECT`から受け取る（Sandboxの`sandbox.env`で渡す）
- `maximum_bytes_billed`を設定する
- 成功時は列名付きのJSONを標準出力に出し、0で終了する
- 失敗時は分類（`auth` / `permission` / `network` / `query`）を付けたJSONを標準エラーに出し、非0で終了する
- 引数でSQLを受け取らない

`bq` CLIは採らない。イメージへの依存が大きく、エラーの分類と出力の形を安定させにくいため。

ランタイムイメージは`FROM <satin-runtime>`で作り、`google-cloud-bigquery`を固定の版で足す。SkillとCLAUDE.mdの配置は次のとおりで、既存の経路をそのまま使う。

- (A): Skills APIと`/workspace/skills/`
- (B): ホスト設定ディレクトリ

`CLAUDE.md`には次を書く。

- 分析ではこのSkillを使う
- 結果にない背景を断定しない
- 失敗したら結果を作らない

### 3. BigQueryの権限は、Terraformで作るGSAを経由する

`usecases/bigquery-analysis/terraform/`は独立したrootにし、`terraform/`のstateに混ぜない。

- 入力: プロジェクト、KSAのnamespaceと名前（既定`satin-sandboxes/satin-bq-sandbox`）、GSAのID、アセットのバケット名、ジョブのプロジェクト
- 管理するもの:
  - BigQuery APIの有効化
  - GSA
  - ジョブのプロジェクトの`roles/bigquery.jobUser`
  - アセットのバケットの`roles/storage.objectViewer`
  - GSAに対する`serviceAccount:<project>.svc.id.goog[<ns>/<ksa>]`の`roles/iam.workloadIdentityUser`
- 出力: GSAのメールアドレス

公開データの読み取りには、データセットの権限は要らない。

KSAは、`iam.gke.io/gcp-service-account`の注釈を付けてoverlayに置き、`automountServiceAccountToken: false`にする。注釈の値は、Terraformの出力からdeploy時に置き換える。overlayの`satin.yaml`では次を設定する。

- `sandbox.service_account: satin-bq-sandbox`
- `sandbox.allowed_fqdns: [bigquery.googleapis.com]`
- `sandbox.image`（ユースケースのイメージ）

メタデータサーバへの送信は既に常に許可しているので、`policies.py`は変えない。silk-agentでは、メタデータサーバへの送信許可を明示する設定を足していた。

他の案は次の理由で採らない。

- **KSAのprincipalに直接BigQueryの権限を付ける**（既定の`terraform/`の方式）: 依頼の「Terraform管理のGSA」に合わない。
- **既定のKSA `satin-sandbox`に付ける**: 全セッションに権限が及ぶ。

GSAを経由すると、アセットの取得（GCS）もGSAの権限になる。そのため、バケットの読み取りもこのrootで与える。

### 4. Echo MCPはクラスタ内のServiceにし、MCP tunnelsで公開する

**Echo MCPサーバ**

- FastMCP（固定の版）のStreamable HTTPで動かす。ポートは8080、パスは`/mcp`。
- namespace `satin-mcp`のDeploymentとClusterIPのServiceにする。
- ASGIのミドルウェアで、全ての要求の鍵を照合する。
  - `X_API_KEY`ヘッダーを受け付ける。
  - `Authorization: Bearer`も受け付ける。Vaultの`static_bearer`が送るヘッダーの名前は文書にないため。実際にどちらが届くかはGKEで確かめる。
  - 比較は定数時間で行う。
- 鍵の環境変数が空なら起動しない。
- 鍵は、Secret `echo-mcp-api-key`から供給する。

**MCP tunnelsのスタック**

- Echo MCPとは別に、公式のHelm chart（`oci://us-docker.pkg.dev/anthropic-public-registry/charts/mcp-tunnel`、版を固定）で入れる。
  - values: `k8s/mcp-tunnel/values.yaml`
  - 入れる手順: `helm upgrade --install`
- `setup.enabled: false`にする。トンネルトークンとサーバ証明書は、Consoleで用意し、手元のファイルから作るSecretを`external.*SecretName`で参照する。
- `gateway.config.routes`は`echo: http://echo-mcp.satin-mcp.svc.cluster.local:8080`にする。
- `upstream.allowed_ips`には、RFC1918の範囲とクラスタのService CIDRを入れる。GKEのService CIDRはRFC1918の外のことがあるため、手順で`kubectl get svc kubernetes`から確かめる。

**NetworkPolicy**: 同じnamespaceにNetworkPolicyとFQDNNetworkPolicyを置く。

- Echo MCPへの受信は、`mcp-proxy`のPodからの8080だけ
- `cloudflared`からの送信は、トンネルのエッジ（`198.41.192.0/19`の7844 TCP/UDP）とDNSだけ
- `mcp-proxy`からの送信は、Echo MCPとDNSだけ

**Agentとセッションの配線**

- ユースケースの`mcp.json`は`{"type": "url", "name": "echo", "url": "https://echo.<トンネルのドメイン>/mcp"}`にする。ドメインはリポジトリに固定せず、publishの前に置き換える。
- 資格情報は、`usecases/bigquery-analysis/scripts/create-vault.py`で作る。
  - Managed AgentsのVaultと、`static_bearer`（`mcp_server_url`はトンネルのURL、tokenは`.env`の`ECHO_MCP_API_KEY`）を作る。
  - VaultのIDだけを状態ファイルに書く。
- Botは`agent.vault_ids`をセッションの作成時に`vault_ids`として渡す。検証用の`commands.py`も同じにする。

**Agentのツール**: `agent_config()`は`mcp_servers`があるとき、サーバごとに`{"type": "mcp_toolset", "mcp_server_name": <name>}`をツールに加える。権限は`always_allow`にする。bashのような確認はBotの対象外のため。

他の案は次の理由で採らない。

- **Echo MCPをIngressで直接インターネットに公開する**: ユーザがMCP tunnelsを指定しており、受信のポートを開けずに済む方を採る。
- **self-hostedのワーカーをMCPのクライアントにし、カスタムツールとして公開する**: SDKのワーカーに手を入れることになり、Managed AgentsのMCP接続を検証するという目的から外れる。
- **Helm chartを使わず、マニフェストを手で書く**: Proxyの設定・証明書・ヘルスチェックの追従を自前で持つことになる。

### 5. 実動作は2段階で確かめる

**ローカル**

- Terraform: `fmt` / `validate` / `plan`
- KSAの注釈とTerraformの入力が一致すること
- SQLの固定の条件
- スクリプトの出力: 成功・0行・権限不足・通信失敗（BigQueryのクライアントは偽物にする）
- Skill・CLAUDE.md・設定の参照の整合性
- Echo MCPへの要求: 鍵なし・誤り・`X_API_KEY`・Bearer
- `agent_config()`の`mcp_toolset`
- Botの`vault_ids`
- Kustomizeの描画
- `helm template`の描画

**GKE**

- KSAとGSAの関連付け
- gVisorのSandbox内でのADCとSQLの実行
- クラスタ内からEcho MCPへの直接の接続（成功と拒否）
- トンネル経由のMCP
- SlackからBigQueryとEchoを頼んだときの、ツール呼び出しのログと応答

公開データは日々変わるので、特定の検索語を成功の条件にしない。

## Risks / Trade-offs

- [MCP tunnelsはresearch previewで、利用申請が要る。chartの値の名前が変わる可能性がある] → chartの版を固定する。申請の状況と、Consoleで発行するものを手順の前提に書く。
- [Vaultの`static_bearer`が送るヘッダーの名前が文書にない] → Echo MCPで`X_API_KEY`とBearerの両方を受け付ける。GKEの検証で、実際に届くヘッダーを記録する（値はログに出さない）。
- [Managed Agentsの`mcp_servers`にトンネルのURLを使う方法が、APIでは明記されていない（文書はConsoleの手順だけ）] → 最初にAPIで作ったAgentでツールを列挙して確かめる。だめなら、Consoleで登録したときのAgentの設定を読んで、`mcp.json`の形を合わせる。
- [MCPのツールの既定の権限が`always_ask`の場合、Botが確認に応答できない] → `mcp_toolset`に`always_allow`を明示する。
- [GKEのService CIDRがRFC1918の外にある] → `upstream.allowed_ips`に実際のCIDRを加える。手順で確かめる。
- [gVisor上でのWorkload Identityのトークンの取得] → 既定のSandboxは既にGCSを読めているので、同じ経路を使う。GSAのなりすましが失敗したら、注釈とIAMを確かめる。
- [SQLのジョブの課金。`LIMIT 10`は走査量を減らさない] → `maximum_bytes_billed`を設定する。検証の前に、課金のプロジェクトとdry runでの走査量を確かめる。
- [配備を入れ替えている間、既定のAgentは使えない] → 検証の間だけ入れ替え、手順に戻し方を書く。

## Migration Plan

1. ユースケースのTerraformを`plan`で確かめてから`apply`し、GSAのメールアドレスを得る。
2. ユースケースのランタイムイメージとEcho MCPのイメージをビルドしてpushする。
3. Echo MCPを配備する。クラスタ内から、直接の接続で認証の成功と拒否を確かめる。
4. MCP tunnelsのSecretを作り、Helmで配備する。
5. トンネルのURLを`mcp.json`に入れる。`create-vault.py`でVaultを作る。`satin publish --config usecases/bigquery-analysis/satin.yaml --assets usecases/bigquery-analysis/assets`で配布する。
6. `OVERLAY=usecases/bigquery-analysis/k8s/overlay SATIN_STATE=.satin-state-bigquery.json scripts/deploy.sh`で、KSAとBot・オーケストレータを適用する。
7. 検証の後、既定に戻すときは`scripts/deploy.sh`（既定のoverlay）を実行し、残ったSandboxを`satin sandbox gc`で消す。

片付けは逆の順に行う。

1. Helmのuninstall
2. Echo MCPのマニフェストとSecretの削除
3. KSAの削除
4. VaultとAgentの片付け（Agentのarchiveは取り消せないので確かめてから）
5. ユースケースのTerraformの`plan -destroy`と`destroy`

既定の配備の設定とPolicyの生成は変わらない。
