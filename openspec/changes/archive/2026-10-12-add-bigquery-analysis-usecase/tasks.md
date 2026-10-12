# Tasks

## 1. 本体の変更（MCPの登録とVault）

- [x] 1.1 `agent_config()`を変更する。`mcp_servers`があるとき、サーバごとに`mcp_toolset`（`always_allow`）をツールに加える。空のときはツールを変えない。`tests/test_assets.py`で両方を確かめる。
- [x] 1.2 設定に`agent.vault_ids`を追加する。Botの`SessionsPort.create()`と、`commands.py`の検証用のセッション作成で`vault_ids`を渡す。未設定のときは渡さない。テストで、設定ありとなしの呼び出し引数を確かめる。
- [x] 1.3 `scripts/deploy.sh`を変更する。`OVERLAY`と`SATIN_STATE`を環境変数で選べるようにする（既定は今の値）。状態ファイルに`vault_id`があれば`__VAULT_ID__`を置き換える。既定のままで描画結果が変わらないことと、ユースケースのoverlayで置き換わることを、`kubectl kustomize`の出力で確かめる。

## 2. Sandboxのクラウドアクセス

- [x] 2.1 `usecases/bigquery-analysis/terraform/`に、独立したrootを作る。中身は次のとおり。
  - BigQuery APIの有効化
  - GSA
  - `roles/bigquery.jobUser`
  - アセットのバケットの`roles/storage.objectViewer`
  - 対象のKSAだけの`roles/iam.workloadIdentityUser`
  - GSAのメールアドレスのoutput

  `terraform fmt -check`、`validate`、`plan`で確かめる。
- [x] 2.2 overlayに`satin-bq-sandbox`のKSAを置く。`iam.gke.io/gcp-service-account`の注釈を付け、トークンの自動マウントはしない。注釈は、deploy時にTerraformの出力で置き換える。描画結果で、注釈とTerraformのnamespace・KSAの名前が一致することを確かめる。
- [x] 2.3 overlayの`satin.yaml`に次を設定する。
  - `sandbox.service_account: satin-bq-sandbox`
  - `sandbox.allowed_fqdns: [bigquery.googleapis.com]`
  - `sandbox.image`
  - `sandbox.env.BIGQUERY_JOB_PROJECT`

  `SandboxConfig.from_config`と`render_sandbox` / `render_policies`の出力を見て、PodのKSAとFQDNの許可を確かめるテストを足す。

## 3. BigQuery分析ユースケース

- [x] 3.1 `usecases/bigquery-analysis/`に、アセット（`CLAUDE.md`、`mcp.json`、`startup.sh`、`skills/bigquery-trends/`）と、publish用の`satin.yaml`を作る。`satin.yaml`には専用の`state_file`・`gcs_url`・`agent.name`を設定する。`load_assets()`で読めることと、参照先が揃っていることを構成テストで確かめる。
- [x] 3.2 `query.sql`を書く。次の条件が依頼と一致することをテストで確かめる。
  - 東京の日付で直近2日
  - Japan
  - 3列
  - 重複を除く
  - 日付の降順・順位の昇順
  - 最大10行
- [x] 3.3 `scripts/run_query.py`を作る。固定のSQL、ADC、`BIGQUERY_JOB_PROJECT`、`maximum_bytes_billed`を使う。偽のクライアントを使ったテストで、次の場合の出力と終了コードを確かめる。
  - 成功
  - 0行
  - 権限不足
  - 通信の失敗
  - 認証の失敗
- [x] 3.4 ユースケースのランタイムイメージ（`FROM satin-runtime`に`google-cloud-bigquery`を固定の版で足す）と、ビルドの手順を作る。`docker build`が通ることと、イメージ内でスクリプトの`--help`相当が動くことを確かめる。
- [x] 3.5 Skill（`SKILL.md`）と`CLAUDE.md`を書く。次の指示を入れる。
  - 結果の根拠（日付・順位・検索語・条件）を示す
  - 背景を断定しない
  - 失敗したら結果を作らない

  テストで、該当の指示がアセットに含まれることを確かめる。

## 4. 認証付きEcho MCP

- [x] 4.1 `usecases/bigquery-analysis/echo_mcp/`に、FastMCP（固定の版）のEchoサーバと認証のミドルウェアを作る。`X_API_KEY`とBearerを受け付け、比較は定数時間で行う。統合テストで次を確かめる。
  - 正しい鍵（どちらのヘッダーでも）なら、同じ文字列が返る
  - 鍵なし・誤った鍵では、ツールが実行されない
- [x] 4.2 空の鍵では起動に失敗すること、鍵が応答と通常のログに出ないことを、テストで確かめる。
- [x] 4.3 Echo MCPのDockerfileと、`k8s/echo-mcp/`を作る。中身は次のとおり。
  - namespace `satin-mcp`
  - Deployment
  - ClusterIPのService
  - Secretの参照
  - `mcp-proxy`のPodからの受信だけを許すNetworkPolicy

  Kustomizeの描画で、Secretの配線と受信の制限を確かめる。

## 5. MCP tunnels

- [x] 5.1 `k8s/mcp-tunnel/values.yaml`を作る。chartの版を固定し、次を設定する。
  - `setup.enabled: false`
  - `external.*SecretName`
  - `gateway.config.routes`の`echo`
  - `upstream.allowed_ips`

  `helm template`の描画で、routeとSecretの参照を確かめる。
- [x] 5.2 `cloudflared`・`mcp-proxy`のNetworkPolicyとFQDNNetworkPolicyを作る。送信は、トンネルのエッジ、DNS、Echo MCPだけに限る。描画結果で、`mcp-proxy`のPodのラベルとEcho MCPのNetworkPolicyのselectorが一致することを確かめる。
- [x] 5.3 トンネルトークンとサーバ証明書から、Secretを作るスクリプトを作る。`.env`と手元のファイルから作り、値を出力しない。dry runの出力で、Secretの名前とキーを確かめる。
- [x] 5.4 `scripts/create-vault.py`を作る。Managed AgentsのVaultと、`static_bearer`の資格情報（トンネルのURLと`ECHO_MCP_API_KEY`）を作り、VaultのIDだけを状態ファイルに書く。偽のAPIを使ったテストで、次を確かめる。
  - 2回目の実行で、Vaultが重複しない
  - 状態ファイルに秘密が入らない

## 6. 配備と実動作の確認

- [x] 6.1 `usecases/bigquery-analysis/README.md`に運用手順を書く。含める内容は次のとおり。
  - 前提: MCP tunnelsの利用申請、Consoleでのトンネルの作成とトークン・CAの登録、課金のプロジェクトとdry runの走査量の確認
  - Terraform
  - イメージ
  - Echo MCP
  - トンネル
  - Vault
  - publish
  - deploy
  - 既定への戻し方
  - 片付け

  手順のコマンドと参照のパスが存在することを確かめる。
- [x] 6.2 GKEで、KSAの注釈とGSAのIAMの関連付けを確かめる。gVisorのSandbox内で、ADCと指定のSQLを実行し、次のどちらかを記録する。
  - 返った列・10行以下・並び
  - 0行

  失敗したときは、認証と送信の許可の原因を特定して直す。
- [x] 6.3 GKEで、クラスタ内から直接接続し、次を確かめる。
  - 正しい鍵でのEcho
  - 鍵なし・誤った鍵の拒否
  - Proxy以外のPodからの接続が拒否されること
- [ ] 6.4 トンネルを経由して、Managed AgentsのセッションからEchoのツールを列挙して呼び出す。Vaultの資格情報で届いたヘッダーの種類を記録する（値は記録しない）。
- [ ] 6.5 Slackから分析の依頼とEchoの依頼を送る。エージェントがSkillのスクリプトとMCPのEchoツールを呼んだことを、ログと応答で確かめる。結果を`docs/verification.md`に記録する。その後、既定の配備へ戻す。
