# satin-agent

Claudeのエージェントのツール実行（bash・ファイル操作）を、**会話（セッション）ごとに起動するGKE Agent Sandbox**の中で行うフレームワーク。次の2通りを、同じSandboxのランタイムイメージ・Skills・`CLAUDE.md`・`startup.sh`で動かす。

| | (A) Claude Managed Agents | (B) Claude Code |
| --- | --- | --- |
| 使い方 | Slack Bot（スレッド = セッション） | claude.aiの画面でself-hosted Environmentを選ぶ |
| エージェントループ | Anthropic側 | Anthropic側 |
| Sandbox内で動くもの | `EnvironmentWorker.handle_item()`（ツール実行だけ） | `claude self-hosted-runner`（1セッション専用） |
| 処理の要求の受け口 | work queue（`satin cma-orchestrator`） | spawn hint（`claude self-hosted-runner orchestrator` + `spawn-runner`フック） |
| ガード・監査フック | あり（Bot内で完結） | なし（入出力の通り道がない） |

エージェントループがAnthropic側にあるので、sandboxdのような中継は使わず、**ワーカー自体をセッション専用のSandboxで動かす**。

## 構成

```
(A)                                         (B)
Slack ── Bot ── Managed Agents session      claude.ai ── Claude Code session
          │          │ work item                              │ spawn hint
          │   satin-cma-orchestrator             satin-cc-orchestrator (+ reconcile)
          │          │ Sandbox CR + Secret                    │ Sandbox CR + Secret
          │          ▼                                        ▼
          │   ┌───────────────────────────────────────────────────────┐
          │   │ Sandbox Pod (gVisor, 非root, セッション専用PVC /workspace) │
          │   │ satin-sandbox: GCSからアセット展開 → startup.sh → ワーカー │
          │   └───────────────────────────────────────────────────────┘
          └─ ConfigMap（セッション→ユーザ、Sandboxの状態）
```

- **1回の処理 = Podの1回の起動**。ワーカーが終了するとPodが終わり、オーケストレータがSandboxを`Suspended`にする。ワークスペース（PVC）は処理をまたいで引き継ぐ。保持期限（既定7日）後に削除し、次の処理では新しいワークスペースになる。
- 1セッション1Sandbox。作成の名前衝突と`resourceVersion`の楽観ロックで守る。

### 責務の境界

| 部品 | 責務 | 持つ秘密 |
| --- | --- | --- |
| `satin cma-orchestrator` | work queueのpoll、Sandboxの起動・停止、状態の公開 | environment key（Managed Agents） |
| `satin-cc-orchestrator` | spawn hintの受付、`spawn-runner`フック | environment secret（Claude Code） |
| `satin cc reconcile`（サイドカー） | Podの終了の検出と停止、期限切れの削除 | なし |
| Slack Bot | スレッド↔セッション、進捗、停止、追加指示、ガード、監査フック、復帰 | Anthropic APIキー、Slackトークン |
| Sandbox | ツール実行、アセットの展開、`startup.sh` | その処理専用のwork secret / work order |

Anthropicの組織スコープのAPIキーは、Botの外には置かない（オーケストレータ・Sandboxには置かない）。

### (A) Slack Botのガードと監査フック

- **応答範囲の制限**（許可workspace・チャンネル・ユーザ、DM/ゲスト/外部ユーザの除外、長さ上限、送信頻度）。除外した投稿には返信しない。
- **入力ガード**: `gemini-3.8-flash`で許可/ブロック/要確認を判定。停止語は対象外。Geminiが失敗したときは既定でブロック。
- **出力の検査**: Model Armorで検査してから表示。個人情報はマスク、その他は差し止め。セッションの内容は書き換えない。
- **bashの実行前確認**: Agentのbashだけ`always_ask`にして、Botが`user.tool_confirmation`で応答する。ディレクトリ全体の一括取り出し（`tar czf all.tgz .`など）は拒否。Botが止まっている間はbashが確認待ちで止まり、再起動後に応答する。
- **監査フック**: 観測専用・非同期。標準フックは1イベント1行のJSON（本文は既定で出さない）。

### Managed Agentsの制約（検証で確認、design.md D10）

- ツール実行・確認待ちの間は`user.message`が拒否される → Botが追加指示を保持し、ツール完了の直後に送る。
- 停止は`user.interrupt`。
- 検証の結果と、見つけて直した不具合は`docs/verification.md`。
- `agent_toolset`の権限は、既定を`always_allow`にして、bashだけ`always_ask`にする。

## 人間が用意するもの

| もの | 場所 | 用途 |
| --- | --- | --- |
| `CLAUDE_API_KEY` | `.env` | Skills/Agentの配布、Botのセッション操作（Botだけがクラスタに持つ） |
| `CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID` / `_KEY` | `.env` | (A) Environment（Consoleで`self_hosted`を作成し、environment keyを発行） |
| `CLAUDE_CODE_SELF_HOSTED_ENVIRONMENT_KEY` | `.env` | (B) claude.aiでself-hosted Environmentを作成して発行するsecret |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | `.env` | Slackアプリ（Socket Mode、`app_mentions:read`・`chat:write`・`files:write`・`users:read`・`users:read.email`、イベントは`app_mention`と`message.channels` / `message.im`） |
| `JEV_API_TOKEN` | `.env` | **テストだけ**（応答の意味の判定）。クラスタには入れない |
| GCPプロジェクト・GKEクラスタ | — | Agent Sandbox controller（v1.0.6）とFQDNNetworkPolicyのあるクラスタ |

`.env`は`.gitignore`済み。値はログ・状態ファイル・スクリプトの出力に出さない。

## テスト

```sh
uv sync
uv run pytest                      # ユニットテスト（外部は全て偽物）
uv run pytest -m live              # 実サービス（Anthropic / Jev / Gemini / Model Armor）
uv run ruff check . && uv run ruff format --check . && uv run pyrefly check
```

応答の判定は、固有の値は文字列一致、意味はJev（`tests/live/jev.py`、`noul > 0.3`）で行う。

## セットアップとデプロイ

1. `terraform/`（`terraform plan`を確認してから`apply`）: アセットのバケット、Artifact Registry、Bot / Sandboxの権限（Workload Identity）、DLP・Model Armorのテンプレート。
2. イメージ: `scripts/build-images.sh [tag]`（ランタイム `docker/runtime.Dockerfile`、アプリ `docker/app.Dockerfile`）。
3. アセットの配布: `satin publish --config satin.yaml --assets assets --target cma,gcs`。
   - GCSの配置を`manifest.json`と個別のファイルから`assets.zip`へ変えた版への切り替え: 新しいイメージをpushし、`satin publish --target gcs`（古いオブジェクトは自動で削除される）、`scripts/deploy.sh`の順に、続けて行う。古いイメージで作られた中断中のSandboxは、再開すると新しい配置を読めず失敗するので、`satin sandbox gc --all`で消す。
4. Secret: `KUBE_CONTEXT=<context> scripts/deploy-secrets.sh`（`.env`から作る。HMAC鍵は既存を維持）。
5. クラスタに適用: `KUBE_CONTEXT=<context> scripts/deploy.sh [tag]`（`k8s/overlays/dev`）。

`KUBE_CONTEXT`は必須。似た名前のクラスタに誤って適用しないため。

同じEnvironmentにつなぐオーケストレータは1つにする（別の場所のものがあるとspawn hintを取り合う）。

## bashツールのタイムアウト

Managed Agentsの`bash`は、SDKの既定では、`timeout_ms`未指定で120秒、**1回のツール呼び出し全体が150秒**で打ち切られる。長いクエリ向けに、Sandboxの起動時に上書きしている（設定`sandbox.bash_default_timeout_seconds`（既定3600）・`sandbox.bash_max_timeout_seconds`（既定7200））。`timeout_ms`を指定すれば、その値が使われる（上限まで）。(B)には`BASH_DEFAULT_TIMEOUT_MS` / `BASH_MAX_TIMEOUT_MS`で渡す（200秒・12分のコマンドで確認済み）。

## アセットの配布とカスタマイズ

```
assets/
  skills/<name>/SKILL.md (+付属ファイル)   # 複数可
  CLAUDE.md
  startup.sh
  mcp.json                                  # (A)のAgentのHTTP MCP（stdioは不可）
```

- (A): Skills APIへ登録（再配布は新バージョン、Agentにはlatestで付ける）、`CLAUDE.md`はAgentのシステムプロンプトへ。Skillsの場所（`/workspace/skills/`）を伝える定型文を加える。
- (A)(B)共通: アセット全体を1つのzip（`assets.zip`、同じ内容なら同じバイト列）にして、GCSの配布先へ1オブジェクトとして置く（配布先のそれ以外のオブジェクトは削除する）。Sandboxは起動時にzipを1回で取り出し、読み込み・CRC・パス（絶対パスや`..`は拒否）を検査してから展開し、失敗したら起動しない。(B)ではホスト設定ディレクトリ（`SELF_HOSTED_RUNNER_HOST_CONFIG_DIR`）に置くので、リポジトリの有無にかかわらず全セッションが読む。
- `startup.sh`: ワーカーの前に毎回、`/workspace`で実行する。環境変数は`SATIN_MODE`・`SATIN_SESSION_ID`・`SATIN_FIRST_RUN`（1/0）・`AGENT_USER_EMAIL`。失敗するとワーカーを起動しない（`SATIN_STARTUP_CONTINUE_ON_FAILURE=1`で続行）。
- ツールを足すときは、`docker/runtime.Dockerfile`のイメージを`FROM`にしたイメージを作り、`sandbox.image`に設定する。
- `AGENT_USER_EMAIL`: Botがセッションの作成時に、会話を始めたSlackユーザのメールを対応表（ConfigMap、HMAC付き）に書き、オーケストレータがSandboxの環境変数にする。(B)は`CLAUDE_RUNNER_ACCOUNT_EMAIL`。

## 既知の制限

- **セッション専用の秘密はツールから見える**: (A)のwork secretはマウントしたファイル、(B)のセッショントークンは環境変数として、同じユーザのツールから読める。どちらもそのセッション専用の短命な値で、Sandboxの外への通信はFQDNNetworkPolicyで制限する。環境変数には置かず、(A)はファイルから読んで引数で渡す。
- ツール実行中は追加指示を送れない。ツールを`user.interrupt`で中断してから送る（止めずに届ける方法がない）。
- ツール実行中にSandboxのPodが消えたとき、猶予内に終わらないツールは、自動で回復するか、失敗が公開される（Botは通知して次の依頼を受けられる）。落ちたワーカーのリースがサーバ側で約5分残る。詳細は`docs/verification.md`とdesign.md。
- (B)にはガード類がない。
- 単一レプリカ（Botは1インスタンス前提でSQLite）。
- Skillsの利用はモデルによる（Haikuは自発的に読まないことがあるため、システムプロンプトで場所を明示している）。

## 片付け

取り消せない操作があるので、順番と対象を確認してから行う。

1. **Kubernetes**（クラスタ上のDeployment・Sandbox・PVC・Secret）
   - `kubectl delete -k k8s/overlays/dev`（Deployment・RBAC・ConfigMap・PVC。namespaceも消える点に注意。`satin-sandboxes`を共有している場合は個別に削除する）
   - 残ったSandboxとワークスペース: `satin sandbox gc --all --config satin.yaml`
   - Secret: `kubectl -n satin-system delete secret satin-secrets satin-cc-environment-secret`
2. **Anthropic**（Skill / Agent / Environment / Vault）
   - **Agentのarchiveは取り消せない**（アーカイブしたAgentは使えず、元に戻せない）。不要と確認してから行う。
   - Skill・Environment・Vaultは、Consoleまたはスクリプトで削除する。environment keyはConsoleで無効にする。
   - `.satin-state.json`のIDで対象を確認し、他のプロジェクトのものを消さない。
3. **GCS**: `gcloud storage rm -r gs://<bucket>/satin/default/`（アセット）。バケット自体はterraformが管理する。
4. **terraform**: `terraform plan -destroy`を確認してから`terraform destroy`。バケットが空でないと失敗する。DLP・Model Armorのテンプレート、IAM、Artifact Registryのイメージも消える。
5. **claude.ai**: self-hosted Environmentの削除とsecretの失効。
