# Tasks

## 1. スパイク（設計の未確定点を先に決める）

- [x] 1.1 Agent Sandbox controller v1.0.6で、`podTemplate`の`restartPolicy: Never`が受け付けられ、コンテナの終了後にPodが`Succeeded`/`Failed`で止まるかを、最小のSandbox CR（`sh -c 'exit 0'`）で確認する。結果（D2の本命か代替か）をdesign.mdのD2に追記して完了とする
- [x] 1.2 `claude self-hosted-runner orchestrator`をローカルで動かし、`spawn-runner`フックに渡る環境変数とwork orderの中身（`--debug-dir`で取得）を記録する。そのwork orderでrunnerを起動する方法（`--environment-secret-file`にwork orderを渡すか）を確かめ、セッションが1つ処理されることを確認して、D6に追記する
- [x] 1.3 Managed Agentsのセッションで、`user.interrupt`による実行中のツールの中断と、実行中に送った`user.message`の扱い（キューされるか）をスクリプトで確認し、D10の表に結果を追記する
- [x] 1.4 `EnvironmentWorker`のbashツールの子プロセスに渡る環境変数と、(B)のrunnerが子のClaude Codeに渡す環境変数を確認する。秘密を除く方法をD7に追記する

## 2. プロジェクトの土台

- [x] 2.1 `pyproject.toml`（Python 3.12、`anthropic`、`kubernetes_asyncio`、`google-cloud-storage`、`slack-bolt`、`aiohttp`、dev: pytest / ruff / pyrefly）と`src/satin_agent/`のパッケージ構成（D11）を作る。`uv sync && uv run pytest`が0件で成功することを確認する
- [x] 2.2 `satin`コマンドのCLIの骨組み（`publish` / `sandbox` / `cma-orchestrator` / `cc spawn` / `cc reconcile` / `slack`）と設定の読み込み（YAMLと環境変数、相対パスは設定ファイル基準）を作る。`satin --help`とサブコマンドのhelpのテストを通す
- [x] 2.3 外部依存のport（Kubernetes、Anthropic、GCS、Slack、時計）と、テスト用の偽物を作る。偽物どうしを組み合わせたテストが通ることを確認する
- [x] 2.4 `.gitignore`（`.env`、`secrets/`、`.satin-state.json`、tfstate、`results/`）、ruff / pyreflyの設定を追加する。`uv run ruff check . && uv run pyrefly check`が通ることを確認する
- [x] 2.5 秘密の読み込みを実装する（`CLAUDE_API_KEY`を優先、なければ`ANTHROPIC_API_KEY`で、Anthropicのクライアントへ明示的に渡す）。どちらもないときのエラーと、値がログに出ないことをテストで確認する
- [x] 2.6 `tests/live/`と`live`マーカー（既定では実行しない）、Jevの判定ヘルパー（`tests/live/jev.py`: `JEV_API_TOKEN`、`noul`、閾値0.3、`usage`の記録）を作る。`uv run pytest`でliveが実行されないこと、`uv run pytest -m live tests/live/test_jev_smoke.py`で、値に言及する文の判定値が言及しない文より高くなることを確認する

## 3. 共有アセットの配布（shared-agent-assets）

- [x] 3.1 アセットディレクトリの読み込み（`skills/*/SKILL.md`、`CLAUDE.md`、`startup.sh`、`mcp.json`）と検証（SKILL.mdのないディレクトリ、stdioのMCP宣言の拒否）を実装する。ユニットテストで、正常系と`skills/broken`のエラーを確認する
- [x] 3.2 GCSへの配布を実装する（相対パスでのアップロード、余分なファイルの削除、`manifest.json`のハッシュ一覧を最後に書き込む）。偽のGCSでのユニットテストと、実バケットへの`satin publish --target gcs`で確認する
- [x] 3.3 Managed Agentsへの配布を実装する（Skillの作成・バージョン追加、Agentの`system`＝CLAUDE.md＋Skillsの場所の定型文、`skills`をlatestで、`mcp_servers`、`.satin-state.json`への記録、消えたSkillを外す）。偽のAnthropicクライアントで2回目の配布・Skill削除のテストを通し、実APIで`satin publish --target cma`を2回実行してAgentのバージョンが上がることを確認する
- [x] 3.4 状態ファイルとログに秘密が出ないことを、テスト（APIキーの値が出力・状態ファイルに含まれない）で確認する

## 4. ランタイムイメージ（sandbox-runtime-image）

- [x] 4.1 entrypoint `satin-sandbox`のアセット展開を実装する（マニフェストの取得、ハッシュの照合、`/workspace/.satin/assets/`への展開、(B)ではホスト設定ディレクトリ（`SELF_HOSTED_RUNNER_HOST_CONFIG_DIR`）の`skills/`と`CLAUDE.md`への配置）。偽のGCSでの不一致時に非0で終わることをユニットテストで確認する
- [x] 4.2 `startup.sh`の実行を実装する（GCS優先、なければイメージの`/opt/satin/startup.sh`、`SATIN_MODE` / `SATIN_SESSION_ID` / `SATIN_FIRST_RUN` / `AGENT_USER_EMAIL`、上限時間、失敗時の既定の停止と`continue`の設定、SIGTERMでのプロセスグループの停止）。初回・2回目・失敗・タイムアウトのテストを通す
- [x] 4.3 `cma`モードを実装する（Secretのファイルからwork itemを読み、`EnvironmentWorker.handle_item()`を`/workspace`で実行し、シグナルでcancel、子プロセスへの秘密の受け渡しを1.4の方法で防ぐ）。ローカルのDockerで、実のManaged Agentsのセッションを1つ処理できることを確認する
- [x] 4.4 `cc`モードを実装する（work orderで`claude self-hosted-runner`を1.2で確かめた引数で起動、`--capacity 1 --drain-grace-sec 0 --release-idle-session-min` / `--drain-wait-sec`の設定、終了コードの中継）。ローカルのDockerで、claude.aiからのセッションを1つ処理できることを確認する
- [x] 4.5 `docker/runtime.Dockerfile`を作る（非root uid 1000、`claude` CLIの固定版、`/etc/satin-image-id`、`/opt/satin/startup.sh`のサンプル）。`docker build`の成功と、`docker run ... foo`が未知のモードとして非0で終わることを確認する

## 5. セッションのSandboxのライフサイクル（session-sandbox-lifecycle）

- [x] 5.1 Sandboxテンプレート（`templates/sandbox.yaml`、gVisor、`automountServiceAccountToken: false`、ワークスペースのvolumeClaimTemplate、Secretのファイルマウント、1.1の結果による`restartPolicy`）と描画を実装する。描画結果のスナップショットテストを通す
- [x] 5.2 Sandbox CRの作成・再開・停止を実装する（セッションIDのハッシュによる名前、ラベル・アノテーションへの状態保存、`resourceVersion`による楽観ロック、作成時のenvの同一性の確認）。偽のKubernetesで、同時の起動指示でSandboxが1つになるテストを通す
- [x] 5.3 処理ごとのSecretの作成・更新を実装する（Sandbox CRをownerにする）。Secretの値がSandbox CRに現れないことをテストで確認する
- [x] 5.4 Podの終了の検出（watchと再列挙、1.1の結果に応じたPodの状態か終了マーカー）と、`Suspended`への切り替え、終了コードの記録を実装する。正常終了・異常終了・watchの切断のテストを通す
- [x] 5.5 準備完了の待機と上限時間、起動失敗時の停止を実装する。存在しないイメージで上限時間内に失敗が返るテストを通す
- [x] 5.6 保持期限後のSandboxとPVCの削除（`satin sandbox gc`）と、期限切れ後の新しい世代の作成（`SATIN_FIRST_RUN=1`）を実装する。偽の時計でのテストを通す
- [x] 5.7 NetworkPolicy（セッション間の通信の禁止）とFQDNNetworkPolicy（DNS、`api.anthropic.com`、`storage.googleapis.com`、設定の許可先）を実装する。描画テストと、GKE上で`curl https://example.com`が失敗することを確認する
- [x] 5.8 ライフサイクルログ（1行1JSON、開始と終了の対）を実装する。3セッション×2回の偽の実行で、対になっていることをテストで確認する

## 6. (A) Managed Agentsのオーケストレータ（cma-sandbox-orchestrator）

- [x] 6.1 `satin cma-orchestrator`を実装する（environment keyだけで`work.poller`、セッション以外のwork itemの無視、同時実行数の上限と上限中のpoll停止、work itemごとのSecretの更新 → Sandboxの起動 → 終了の待機 → 停止）。偽のpollerとKubernetesで、並行2件・上限超過のテストを通す
- [x] 6.2 起動時の`Running`のSandboxの監視の引き継ぎと、SIGTERMでの受け取り停止を実装する。偽の環境で、再起動後に残ったSandboxが停止されるテストを通す
- [x] 6.3 `AGENT_USER_EMAIL`を対応表（ConfigMap `satin-session-users`、HMAC付き）から読み、作成時のenvにする。対応表にないセッションは空にする。テストで確認する
- [x] 6.4 Sandboxの状態（起動中・実行中・停止）を、Bot向けのConfigMapに書き込む。テストで確認する

## 7. (B) Claude Codeのオーケストレータ（claude-code-sandbox-orchestrator）

- [x] 7.1 `spawn-runner`フック（`/opt/satin/hooks/spawn-runner` → `satin cc spawn`）を実装する（work orderのSecret化、Sandboxの作成・再開を準備完了を待たずに行う、終了コード0/1/2の使い分け）。偽のKubernetesで、一時的なエラーで1、設定の誤りで2を返すテストを通す
- [x] 7.2 `satin cc reconcile`（Podの終了の検出と停止、保持期限の削除）を、オーケストレータのPodのサイドカーとして実装する。テストで確認する
- [x] 7.3 `CLAUDE_RUNNER_ACCOUNT_EMAIL`を`AGENT_USER_EMAIL`として渡す。セッション内で`echo $AGENT_USER_EMAIL`が自分のメールになることを、GKE上で確認する

## 8. (A) Slack Bot（slack-bot）

- [x] 8.1 チャネル非依存の会話の中核を実装する（会話ごとの直列化、停止語の正規化、実行中の追加指示、1分ごとの更新、turnの状態の管理）。silk-agentと同じ種類のユニットテスト（停止語、追加指示、heartbeat）を偽の時計とセッションで通す
- [x] 8.2 Managed Agentsのセッションの操作を実装する（作成、`user.message`の送信、`events.stream`の購読、`events.list`での取りこぼしの補完、停止、`requires_action`の`agent.tool_use`への`user.tool_confirmation`の送信）。1.3の結果に合わせて、偽のセッションでのテストを通す
- [x] 8.3 Botの管理情報（スレッド → セッションID、進捗メッセージの位置、最後に処理したイベントID、turnの状態）をSQLiteに保存する。再起動時の中断turnの検出と、購読の再開または最終状態の反映、確認待ち（`requires_action`）のセッションへの応答を実装する。偽の環境での再起動テスト（ツール実行中・確認待ちの2通り）を通す
- [x] 8.4 Slackのチャネルを実装する（Socket Mode、メンション・DM・スレッドの返信、重複イベントの除外、進捗メッセージ、長い返答のファイル化、Slackのエスケープの復元）。偽のSlackでのテストを通す
- [x] 8.5 セッションの作成時に、会話を始めたユーザのメールを対応表（6.3）に書く。Sandboxの状態（6.4）を進捗メッセージの「作業環境を起動中」に反映する。テストで確認する
- [x] 8.6 エラーの表示（Sandboxの起動の失敗、セッションのエラー、APIのエラー）を実装する。偽の環境で、失敗の表示と案内の投稿をテストで確認する

## 8A. (A)のガード類（slack-botのガードの要件）

- [x] 8A.1 ルールのフィルタをsilk-agentから移植する（Bot・システムイベント・空の除外、許可workspace・チャンネル・ユーザ、DM・ゲスト・外部ユーザの除外（`users.info`の10分キャッシュ、判定できなければ除外）、長さの上限8,000文字、ユーザごとの送信頻度1分20件）。silk-agentのフィルタのテストを移植して通す
- [x] 8A.2 Geminiの入力ガードを移植する（`gemini-3.8-flash`、基準のファイル、直前5件の文脈、許可・ブロック・要確認と理由、失敗時は既定でブロック、設定で通す）。Botの`user.message`送信の直前（追加指示を含み、停止語を除く）につなぐ。偽のGeminiでのテストと、liveのテスト（上書き要求がブロックされ、Jevで「理由を説明している」と判定される）を通す
- [x] 8A.3 Model Armorの出力検査を移植する（返答・ツールの要約・アップロードの検査、個人情報だけならマスク、ほかのフィルタは非表示、jailbreakは無視（設定可）、境界での分割、失敗時は非表示、セッションは書き換えない）。偽のModel Armorでのテストと、liveのテスト（メールアドレスがマスクされる）を通す
- [x] 8A.4 bashの実行前確認を実装する。`bulk_files`の判定をsilk-agentから移植し、Agentの設定でbashだけを`always_ask`にする（`satin publish`のAgentの`tools`の設定に反映）。Botの`user.tool_confirmation`で`allow` / `deny`と理由を返し、進捗メッセージに拒否を表示する。silk-agentの`bulk_files`のテスト（拒否・許可の両方）と、偽のセッションでの拒否の表示のテストを通す
- [x] 8A.5 監査フックを移植する（イベントの種類にbashの承認の判定を追加、非同期キュー、上限を超えたら古いものを捨てる、例外の隔離、読み込めないフックでは起動しない、`json_lines`（本文の出力は既定で無効））。例外を出すフックでも返答が投稿されるテストと、ブロックした入力がフックに渡るテストを通す
- [x] 8A.6 GeminiとModel Armorの設定（モデル、リージョン、基準のファイル、テンプレート）をBotの設定ファイルに追加する。設定の読み込みのテストを通す

## 9. デプロイ

- [x] 9.0 ユーザからGKEクラスタの情報（クラスタ名、リージョン、Agent Sandbox controllerの版、Sandbox用のnamespace、FQDNNetworkPolicyの有無）を受け取り、design.mdのContextとOpen Questionsを更新する。`kubectl get crd sandboxes.agents.x-k8s.io`で、CRDが使えることを確認する
- [x] 9.1 `terraform/`（アセット用GCSバケット、Sandbox用とアプリ用のGSA、Workload Identity、バケットの権限、Botへの`aiplatform.user`と`modelarmor.user`、DLPテンプレートつきのModel Armorテンプレート）を作る。`terraform plan`の内容を確認してから`apply`する
- [x] 9.2 `docker/app.Dockerfile`（オーケストレータ2種とBotが共通、(B)用に`claude` CLIを含む）とビルド・pushスクリプトを作る。Artifact Registryへpushできることを確認する
- [x] 9.3 `k8s/base`（`satin-cma-orchestrator`、`satin-cc-orchestrator`＋reconcileサイドカー、`satin-slack-bot`＋PVC、KSA、Sandbox namespaceのRole / RoleBinding、Sandbox用KSA）と`k8s/overlays/dev`（設定のConfigMap、イメージ）を作る。`kubectl kustomize`が通り、applyで3つのDeploymentがReadyになることを確認する
- [x] 9.4 秘密（`.env`の`CLAUDE_API_KEY`、Managed Agentsのenvironment key、Claude Codeのenvironment secret、Slackのトークン、HMAC鍵）のSecretを作るデプロイスクリプトを作る。`JEV_API_TOKEN`はクラスタに入れない。スクリプトの出力とログに値が出ないことを確認する
- [x] 9.5 サンプルのアセット（`container-probe` Skill、CLAUDE.md、`startup.sh`、`mcp.json`）を作り、`satin publish --target cma,gcs`で配布する。Agentのバージョンと`manifest.json`を確認する

## 10. 検証（resilience-verification）

- [x] 10.1 (A)のシナリオ実行スクリプト（`verification/run_cma.py`）を作る。Managed Agentsのセッションを直接操作し、`kubectl`でPodを削除するなどの障害を注入し、合否と根拠（イベントID、ログの行、Jevの判定値）を出力する。応答の判定は、固有の値なら文字列一致、意味ならJev（2.6のヘルパー）で行う
- [x] 10.2 (A)の基本のシナリオを実行して記録する。Sandbox内でのツール実行（イメージ識別子）、Skillsの利用（Haikuと上位モデル）、MCPの利用、秘密がツールから見えないこと、許可していない宛先への通信の失敗
- [x] 10.3 (A)のライフサイクルのシナリオを実行して記録する。idle後の再開でのワークスペースの引き継ぎ、保持期限（10秒）後の新しいワークスペース、2セッションの並行実行、存在しないイメージでの起動失敗
- [x] 10.4 (A)の障害のシナリオを実行して記録する。ツール実行中のSandbox Podの削除、ツール実行中のオーケストレータの再起動
- [x] 10.5 Slackで(A)のシナリオを実行して記録する。進捗表示、停止語での中断、実行中の追加指示、ツール実行中のBotの再起動、bashの確認待ちでのBotの再起動、応答範囲の制限、`AGENT_USER_EMAIL`、Sandbox起動中の表示、起動失敗の表示
- [x] 10.5a Slackで(A)のガードのシナリオを実行して記録する。入力ガードによるブロック、Geminiの障害時のブロック、出力の個人情報のマスク、一括取り出しのコマンド（`tar czf all.tgz .`）の拒否と通常のコマンドの許可、フックのJSON Linesの出力
- [x] 10.6 claude.aiで(B)のシナリオを実行して記録する。Sandbox内でのツール実行、SkillsとCLAUDE.mdの読み込み（リポジトリあり・なし）、`startup.sh`の実行、idle後の再開でのワークスペースの引き継ぎ、ユーザ入力待ちでのSandbox Podの削除、オーケストレータの再起動、2セッションの並行実行
- [x] 10.7 検証記録（`docs/verification.md`）に、各シナリオの実施日・モデル・結果・観察した振る舞い・Jevの判定値と使用トークン数・満たせなかったケースの理由をまとめる。記録とログにトークンが含まれないことを`grep`で確認する

## 11. ドキュメント

- [x] 11.1 `README.md`を書く。構成（(A)と(B)の図、責務の境界の表）、(A)のガード類と監査フック、人間が用意するもの（`.env`の`CLAUDE_API_KEY` / Slack / `JEV_API_TOKEN`、environment key / secretの発行手順、Slackアプリ、GCP）、テストの実行（ユニットとlive）、セットアップとデプロイ、アセットの配布、ユースケースでのカスタマイズ（イメージの`FROM`、`startup.sh`、Skills、CLAUDE.md）、既知の制限
- [x] 11.2 片付けの手順（Agent / Environment / Skill / Vault、Sandbox / PVC、GCS、terraform）をREADMEに書く。取り消せない操作（Agentのarchive）には注意を明記する
