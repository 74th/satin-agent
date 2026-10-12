# Design

## Context

- satin-agentは新規リポジトリで、コードはまだない。目的はproposal.mdの「Why」を参照。
- (A) Managed Agentsのself-hosted Environmentは`test-llm-agent/20260922-claude-managed-agents`で確認済み。
  - work queueのポーリング（`client.beta.environments.work.poller`）は、environment keyだけでできる。
  - `EnvironmentWorker.handle_item()`は、`ANTHROPIC_SESSION_ID` / `ANTHROPIC_WORK_ID` / `ANTHROPIC_ENVIRONMENT_ID` / `ANTHROPIC_WORK_SECRET`から1つのwork itemを処理し、セッションがidleになると終わる。
  - **work item ＝ セッションの1ラン**であり、turnごとではない。6 turnでもコンテナは1つだった。
  - Skillsは`/workspace/skills/<name>/`にダウンロードされるが、Haikuでは自発的に読まないことがあった。Skillのバージョンを明示すると、ダウンロードが失敗する不具合もあった（latestで回避）。
  - self-hostedでは、vaultの`environment_variable` credential、`file` / `github_repository`リソースは使えない。
- (B) Claude Codeのself-hosted Environmentは、`claude self-hosted-runner`（CLI 2.1.296）で動く。`claude self-hosted-runner orchestrator`は、spawn hintを受けるたびに`<hooks-dir>/spawn-runner`を実行する。
  - フックには`CLAUDE_RUNNER_WORK_ORDER_FILE`（work orderのJWT）、`CLAUDE_RUNNER_SESSION_ID`、`CLAUDE_RUNNER_ACCOUNT_EMAIL`、`CLAUDE_RUNNER_PRIMARY_REPO_URL`などが渡る。
  - フックは非同期で起動を投げて、`--hook-timeout`（既定60秒）内に終わる必要がある。終了コードは0=成功、1=再試行、2以上=再試行しない。
  - runner側には`--release-idle-session-min`（idleでセッションを保留にして解放）、`--drain-grace-sec 0`（1回限り）、`--drain-wait-sec`、`SELF_HOSTED_RUNNER_HOST_CONFIG_DIR`（環境変数。各セッションの`CLAUDE_CONFIG_DIR`の元）、`--hooks-dir`（checkout / post-session）がある。
- silk-agentの`gke_session_sandbox`は、Agent Sandbox controller v1.0.6の`agents.x-k8s.io/v1beta1` `Sandbox`を使う。
  - 実装済み: `operatingMode: Running | Suspended`での停止・再開、`volumeClaimTemplates`のワークスペースPVC、FQDNNetworkPolicy、作成時のenv注入（`AGENT_USER_EMAIL`）、SQLite / ConfigMapでのセッション管理、supervisorでの`startup.sh`実行（`SANDBOX_STARTUP_FIRST_RUN`）。
  - idle停止は、sandboxd上のCommandTrackerのコマンド数に基づく。
- silk-agentには、ガード（`guards/gemini.py`、`guards/model_armor.py`、`guards/bulk_files.py`）、フック（`hooks.py`）、会話の中核（停止語・追加指示・heartbeat）が実装・テスト済みである。
- GKEクラスタは、silk-agentで構築中のものを使う（`nnyn-dev`）。クラスタ名・リージョン・Sandbox用のnamespaceは、構築後にユーザから受け取る。
- `.env`のAnthropic APIキーは`CLAUDE_API_KEY`という名前である（SDKの既定の`ANTHROPIC_API_KEY`ではない）。Jevのトークンは`JEV_API_TOKEN`である。
- Jev（TypeSafe System One、`typesafe-sdk`）は、`test-llm-agent/20261010-claude-agent-sdk-sessionstore`で応答の判定に使った。閾値0.3で8ケース中7件が正しく、厳格すぎる面がある。そのため、文字列一致を主にして、Jevは「意味として述べているか」の補助に使うのがよい、という結論だった。

- 使うクラスタは`gke_nnyn-dev_us-central1-a_silk-agent-gke`（silk-agentで構築、controller v1.0.6、FQDNNetworkPolicyのCRDあり）。satin-agentのSandbox用namespaceは、silk-agentと分けるため`satin-sandboxes`とする（2026-10-11にユーザが決定・作成）。

## Goals / Non-Goals

**Goals:**

- (A)と(B)の違いを「Sandbox内で何を起動するか」と「処理の要求をどこから受けるか」だけに閉じ込め、Sandboxのライフサイクル・イメージ・アセットは共通にする。
- Sandboxは「1回の処理（(A)はwork item、(B)はrunnerの1回の割り当て）＝ Podの1回の起動」とする。ワークスペースはPVCで処理をまたいで引き継ぐ。
- silk-agentで確かめた中断・再起動のケースを、(A)のSlack Botで再現して結果を記録する。

**Non-Goals:**

- Sandboxを常駐させてセッション間で使い回すこと（WarmPool）。
- (B)をSlackから使うこと。
- (B)のガード類。(B)はclaude.aiの画面から使い、satin-agentの側に入力と出力の通り道がない。
- 複数のGKEクラスタやリージョン。

## Decisions

### D1. ワーカー自体をSandbox内で動かす（sandboxdを使わない）

(A)では`EnvironmentWorker`、(B)では`claude self-hosted-runner`を、そのセッション専用のSandbox Podのメインプロセスとして動かす。ツール実行はワーカーのプロセス内（＝Sandbox内）で行われる。エージェントループはAnthropic側にあるため、sandboxdのような中継は不要になる。

- 代替案: ワーカーをオーケストレータのPodで動かし、ツール実行だけをsandboxdでSandboxへ転送する（silk-agent方式）。却下の理由は2つある。`EnvironmentWorker`の組み込みツールはローカルのプロセスとファイルシステムを前提にしていて、差し替えるにはSDKの内部に手を入れる必要がある。(B)のClaude Codeでは差し替えの口がない。
- 結果として、ワーカーが使うAnthropic APIへの通信と、work secret / work orderがSandbox内に入る。対策はD7。

### D2. 「処理の終了 ＝ Podの終了 ＝ Sandboxの停止」

ワーカーは1回の処理を終えると終了する。(A)は`handle_item()`の完了、(B)は`--drain-grace-sec 0`と`--release-idle-session-min`によるrunnerの終了である。オーケストレータ（どちらも共通のライフサイクル部品を使う）は、Sandbox Podのコンテナの終了をwatchで検出し、Sandboxを`Suspended`にする。

- Sandbox controller v1.0.6のPodが`restartPolicy: Never`（または`OnFailure`）を受け付けるかは未確認である。受け付けない場合は、ワーカーの終了後にentrypointが終了マーカー（`/workspace/.satin/exit.json`と、readinessを落とすHTTP）を出して待機し、オーケストレータがそれを見て`Suspended`にする。これは最初のスパイク（tasks 1.x）で決める。どちらでも外から見える振る舞い（spec）は変わらない。
- **スパイク1.1の結果（2026-10-11、クラスタ`gke_nnyn-dev_us-central1-a_silk-agent-gke`、Sandbox用namespace `session-sandboxes`（このスパイクのみ。satin-agentの本番用は`satin-sandboxes`）、controller v1.0.6、gvisor、FQDNNetworkPolicyのCRDあり）**: `podTemplate.spec.restartPolicy: Never`は受け付けられた。コンテナが`exit 0`で終わるとPodは`Completed`で止まり、再起動されない。Sandboxの状態は`Ready=False`、reason `PodSucceeded`になる。よってD2の本命（Podの状態を見る方式）を採用し、終了マーカーの代替は使わない。
- 代替案: Sandboxを常に`Running`にしておき、idle時間で止める（silk-agent方式）。却下の理由: idleの判断がAnthropic側（セッションのidle）にあり、ワーカーの終了がそのまま最良のシグナルになるため。

### D3. 処理ごとの情報はKubernetes Secretで渡す

Sandboxの`podTemplate`は作成後に変えにくく、変えればPodが作り直される。そのため、処理ごとに変わる値は、セッション専用のSecret `satin-run-<hash(session)>`に入れてPodにファイルとしてマウントし、再開の前に書き換える。値は(A)ならwork ID・work secret、(B)ならwork orderのJWTである。

- 一方`AGENT_USER_EMAIL`やモードのように、セッションの中で変わらない値は、作成時のenvとして`podTemplate`に入れる（silk-agentと同じ）。
- entrypointはSecretのファイルを読んだらワーカーに渡し、ツール実行の子プロセスの環境変数には入れない（D7）。

### D4. ライフサイクル部品は`gke_session_sandbox`を依存にせず、必要な部分を移植する

satin-agent内に`satin_agent.sandbox`を作り、次をsilk-agentから移植・簡略化する。

- Sandboxテンプレートの描画（`{$param: ...}`）
- `operatingMode`での停止・再開
- PVC管理
- FQDNNetworkPolicy
- env注入と同一性の確認（作成時のenvが変わっていないこと）
- 保持期限後の削除
- ライフサイクルログ

セッションの状態は、Sandbox CRのラベルとアノテーションに持たせる。具体的には、セッションID、世代、最後の終了時刻、終了コードである。別のDBは持たない。

- 代替案: silk-agentの`gke_session_sandbox`をそのまま依存にする。却下の理由: sandboxd / CommandTracker / gRPCの生成コード / protobuf制約が前提に深く入っていて、satin-agentでは使わない部分が大半を占めるため。
- 状態をCRに置くことで、オーケストレータを複数動かしても、`resourceVersion`による楽観ロックで「1セッション1Sandbox」を守れる（specの同時起動のシナリオ）。

### D5. (A)のオーケストレータ

Deployment `satin-cma-orchestrator`（1レプリカ）が`work.poller`でwork itemを受け取り、次の順に処理する。

1. Secretを更新する。
2. Sandboxを作成または再開する。
3. 準備完了を待つ。
4. Podの終了を待つ。
5. Sandboxを`Suspended`にする。

受け取りと実行を分けるため、同時実行数の上限（セマフォ）が埋まっている間はpollしない。

- 起動時には、`Running`のまま残っているSandboxを列挙して監視を引き継ぐ。
- 受け取ったまま起動できなかったwork itemは、`reclaim_older_than_ms`でAnthropic側から再配布される。そのため、オーケストレータは受け取った事実を永続化しない。

### D6. (B)のオーケストレータ

Deployment `satin-cc-orchestrator`が、`claude self-hosted-runner orchestrator --hooks-dir /opt/satin/hooks`を動かす。

- `spawn-runner`フックは`satin cc spawn`（Python）を呼び、次を行って、すぐに0を返す。
  - work orderをSecretに入れる。
  - Sandboxを作成または再開する（準備完了は待たない）。
- Kubernetes APIの一時的なエラーには1を返し、設定の誤りには2を返す。
- Podの終了の監視と停止は、同じPod内で動く`satin cc reconcile`（サイドカー）が行う。
- セッションのキーには`CLAUDE_RUNNER_SESSION_ID`を使う。`CLAUDE_RUNNER_ACCOUNT_EMAIL`は`AGENT_USER_EMAIL`として渡す。
- Sandbox内では、`claude self-hosted-runner`を次の設定で動かす。
  - `--capacity 1 --drain-grace-sec 0`
  - `--release-idle-session-min 10 --drain-wait-sec 60`
  - `--base-dir /workspace/runner`
  - 環境変数`SELF_HOSTED_RUNNER_HOST_CONFIG_DIR=/home/satin/.claude-host`
- work orderのJWTを、spawnしたrunnerへどう渡すかはCLIの文字列から推定した。「environment secretとして渡す（`--environment-secret-file`にwork orderのファイルを指す）」と読める（`startedFromWorkOrder`）が、ドキュメントで確認できていない。最初のスパイクで確定する。違った場合も、変わるのはentrypointの引数だけである。

- **スパイク1.2の結果（2026-10-11、claude 2.1.296、手元で実行）**:
  - `spawn-runner`フックには、`CLAUDE_RUNNER_ACCOUNT_EMAIL` / `ACCOUNT_ID` / `ATTEMPT` / `CLIENT_PLATFORM` / `CORRELATION_ID` / `ORDER_ID` / `ORDER_SERVER_TIME` / `POOL_ID` / `PRIMARY_REPO_REVISION` / `PRIMARY_REPO_URL` / `REPO_SOURCES` / `SESSION_ID` / `SESSION_UUID` / `WORK_ORDER_FILE`が渡る。`SESSION_ID`は`session_...`形式（ランナーのログでは`cse_...`）。work orderのファイルは`/tmp/work-order-<jti>-...`（0600）。
  - `--debug-dir`のJSONのキーは、`jti` / `session_id` / `attempt` / `pool_id` / `account_id` / `account_email` / `server_time` / `repo_sources` / `primary_repo_url` / `primary_repo_revision` / `correlation_id` / `client_platform`（claude.aiからは`web_claude_ai`）。
  - **work orderのJWTを`--environment-secret-file`に渡して`claude self-hosted-runner --capacity 1 --drain-grace-sec 0`を起動すると、runnerが登録され（`Registered`）、そのセッションを取得して処理できた（確認済み）。** D6の推定は正しい。
  - work orderは1回限りで短命。同じものでの再起動は不可。runnerが登録されない間、orchestratorは`--expected-spawn-seconds`（既定120秒）ごとに新しいwork orderでフックを再実行する。フックは、そのセッションのSandboxが既に`Running`なら何もせず、Secretのwork orderも書き換えない（`Running`のPodは古いwork orderで起動済みのため）。`Suspended`か未作成なら、Secretを最新のwork orderにして起動する。
  - **`--host-config-dir`というフラグはない。** ホスト設定のディレクトリは環境変数`SELF_HOSTED_RUNNER_HOST_CONFIG_DIR`で渡す（D6・D9の「`--host-config-dir`」はこれに読み替える）。
  - `--base-dir`が子の書き込み許可範囲と重なる場所（今回は`/tmp/claude-1000`配下）だと、「overlaps the child's auto-allowed write scope — refusing to spawn」でセッションが失敗する。Sandboxでは`/workspace`を使うので問題ないが、`TMPDIR`の下に置かない。
  - 所要時間: 「こんにちは」のturnは約5秒。`--release-idle-session-min 1`のとき、turn終了の1分後にセッションを解放（サーバ側で保留）し、`--drain-wait-sec`の後にrunnerが終了した（解放から約15秒）。つまりPodの終了は、idleの設定値＋十数秒になる。
  - 失敗したセッションはサーバ側で`excluded`として数えられる（`excluded_count=1`）。

### D7. 秘密の置き場所

| 秘密 | 置き場所 | 置かない場所 |
| --- | --- | --- |
| Anthropic APIキー | 配布スクリプトの実行環境、Slack BotのSecret | オーケストレータ、Sandbox |
| Managed Agentsのenvironment key | `satin-cma-orchestrator`のSecret | Sandbox（work secretを優先して渡す） |
| Claude Codeのenvironment secret | `satin-cc-orchestrator`のSecret | Sandbox |
| work secret / work order | セッション専用のSecret → Sandboxにファイルでマウント | ツール実行の環境変数 |

- entrypointは、秘密のファイルを読んだらワーカーのプロセスにだけ渡す。ツール実行の子プロセスには、`env`の許可リストで渡す変数を絞る。
- (A)では`EnvironmentWorker`のツール実行の環境変数を確かめ、必要ならentrypointで`ANTHROPIC_*`を取り除く。
- (B)ではrunnerが子のClaude Codeに渡す環境変数を確かめる。
- **スパイク1.4（A）の結果（2026-10-11、anthropic SDK 1.13.0）**:
  - `EnvironmentWorker`のbashツールの子プロセスには、`ANTHROPIC_*`は渡らない。見える変数は`HOME` / `LC_CTYPE` / `PATH` / `PWD` / `SHLVL` / `TERM`だけだった。
  - ただし、ワーカー自身のプロセスの環境変数は、ツールの子から`/proc/$PPID/environ`で読める（同じユーザ）。`ANTHROPIC_SESSION_ID` / `WORK_ID` / `ENVIRONMENT_ID` / `WORK_SECRET`が見えた。`/proc/1/environ`は権限で読めなかった。
  - 対策（確認済み）: entrypointはSecretのファイルを読んで、`EnvironmentWorker.handle_item(work_id=..., environment_id=..., session_id=..., work_secret=...)`に**引数で渡す**。クライアントは`AsyncAnthropic(auth_token=<work secret>)`で作る。ワーカーのプロセスの環境変数には`PATH`と`HOME`だけを置く。このとき`/proc/$PPID/environ`に秘密は出なかった。ファイルは読んだ直後に削除する（Secretのマウントは読み取り専用なので、実際はコピーを経由せず、マウントしたファイルを読むだけにする）。
  - `handle_item`はidleになってから`max_idle`（既定60秒）待って終わる。`max_idle=10`で約16秒に短縮できた。この値はD2の「処理の終了」までの時間に直接効くので、設定で変えられるようにする。
  - 訂正: bashツールの既定の環境は「プロセスの環境変数から`ANTHROPIC_*`だけを除いたもの」である（`agent_toolset.py`のソースで確認）。上の「見える変数が少ない」のは、スパイクがワーカーに`PATH`と`HOME`しか渡していなかったため。したがって`AGENT_USER_EMAIL`などは、Podの環境変数に置けばツールにも渡る見込み（4.3のローカルDockerで実測する）。逆に、ワーカーの環境変数に置いた秘密は`/proc/$PPID/environ`から読めるので、秘密は環境変数に置かない。
  - 補足: `EnvironmentWorker.run()`で積まれたwork itemは、別のpoller（`reclaim_older_than_ms`）にも拾われる。オーケストレータは、受け取ったwork itemのセッションIDだけを処理する。
- **スパイク1.4（B）の結果（2026-10-11、claude 2.1.296）**: runnerが起動した子のClaude Codeのbashツールから見える環境変数は、runner自身の環境変数をそのまま引き継いだものに、セッション専用の値が加わったものだった（変数名のみ確認）。
  - runnerの環境変数: 手元のシェルの変数がすべて見えた。Sandboxでは、entrypointが渡した環境変数がそのまま見えることになる。`AGENT_USER_EMAIL`をrunnerの環境に置けば、ツールから`$AGENT_USER_EMAIL`で読める見込み（7.3で実測）。`CCR_SESSION_ACCOUNT_EMAIL`（セッションを始めたアカウントのメール）も最初から渡る。
  - runnerが追加する値: `CLAUDE_CODE_SESSION_ACCESS_TOKEN`、`CLAUDE_CODE_MESSAGING_TOKEN`、`CLAUDE_SESSION_INGRESS_TOKEN_FILE`、`SESSION_INGRESS_URL`、`CLAUDE_CODE_REMOTE_SESSION_ID`など（`CLAUDE_CODE_*`が約50個）。claude.aiのセッションのための**セッション専用の短命なトークン**が、ツールから読める。これらは除けない（子のClaude Code自身が使う）。
  - 環境secret（`SELF_HOSTED_RUNNER_ENVIRONMENT_SECRET`）は、`--environment-secret-file`で渡したので、子の環境変数には出ない。**work orderは環境変数でなく、ファイルで渡す。** work orderはrunnerの登録後は不要になるので、読んだ後にファイルを消す。
  - 結論: (B)では、セッション専用のトークンがツールから見える。これは(B)の仕様上の制約で、除けない。残るのは、Sandbox内にその他の秘密（Anthropic APIキー、environment secret）を置かないこと。Sandboxの外への通信はFQDNNetworkPolicyで制限する。
- **実装後の確認（4.3、ローカルDocker、2026-10-11）**: (A)のコンテナ内で、ツールから見える環境変数に`ANTHROPIC_*`・work secretは出なかった（`/proc/$PPID/environ`も同様）。`AGENT_USER_EMAIL`はツールから読めた。一方、Secretのマウント先（`/run/satin/work_secret`など）は**ファイルとしては読める**。環境変数からは見えないが、ファイルは同じユーザなので読める。work secretは短命でそのセッション専用のため、許容する（specの「環境変数から参照できない」は満たす）。
- 完全な秘匿ではない。同じユーザのプロセスなので`/proc`から読める可能性が残る。そのため、Sandbox内の秘密は「そのセッションにしか使えない短命なもの」に限る（specの「秘密がツールから見えない」のシナリオで実測する）。

### D8. ランタイムイメージ

`python:3.12-slim-bookworm`をベースに、次を入れる。

- 非rootユーザ`satin`（uid 1000）
- `anthropic` SDK
- `claude` CLI（ネイティブバイナリをビルド時に固定版で取得）
- `google-cloud-storage`
- `satin_agent.runtime`（entrypoint）
- git、ca-certificates
- `/etc/satin-image-id`（ビルド時の識別子）

entrypoint `satin-sandbox`の処理は次の順である。

1. Secretのファイルを読む。
2. GCSのマニフェストを取り出す。
3. Skills / CLAUDE.md / `startup.sh`を展開する。
4. `startup.sh`を実行する。
5. モードのワーカーを起動する。
6. シグナルを中継する。

- ユースケースは、このイメージを`FROM`にしてツールと`startup.sh`を足す。`startup.sh`はGCSに置いたものが優先で、なければイメージの`/opt/satin/startup.sh`を使う。
- `startup.sh`に渡す環境変数は`SATIN_MODE`、`SATIN_SESSION_ID`、`SATIN_FIRST_RUN`、`AGENT_USER_EMAIL`である。silk-agentの`SANDBOX_STARTUP_FIRST_RUN`に相当する判定は、`/workspace/.satin/startup-done`の有無で行う。

### D9. アセットの配置

- アセットディレクトリ（例: `assets/`）は`skills/*/SKILL.md`、`CLAUDE.md`、`startup.sh`、`mcp.json`（(A)のAgentのMCP宣言）を持つ。
- 配布スクリプト`satin publish --assets assets/ --target cma,gcs`の処理:
  - (A): Skills APIへの作成・バージョン追加、Agentの`system`（CLAUDE.md＋定型文）・`skills`（latest）・`mcp_servers`の更新、`.satin-state.json`への記録。
  - GCS: `gs://<bucket>/<prefix>/`へのアップロード。`manifest.json`は最後に書き込み、余分なファイルは削除する。
  - 状態の再利用とlatest指定は、`provision.py`の実績を踏襲する。
- (A)のSkillsの見落とし対策として、システムプロンプトの定型文で「Skillsは`/workspace/skills/<name>/SKILL.md`にある。作業の前に一覧を確認する」と指示する。
- (B)での展開先:
  - Skillsと`CLAUDE.md`は、runnerのホスト設定ディレクトリ（`SELF_HOSTED_RUNNER_HOST_CONFIG_DIR`）（`/home/satin/.claude-host/skills/`、`/home/satin/.claude-host/CLAUDE.md`）に置く。runnerがそこを各セッションの`CLAUDE_CONFIG_DIR`に複製するので、ユーザレベルのSkillsとメモリとして、リポジトリのチェックアウトの有無にかかわらず読まれる。
  - 依頼の「ワークスペースへ展開」は、`/workspace/.satin/assets/`へ取り出してから上記へ配置することで満たす。PVC上に残すのは、再開時の差分の確認と、`startup.sh`からの参照のためである。
  - 代替案: 各セッションのリポジトリの`.claude/skills`へ置く。却下の理由: チェックアウト先がセッションごとに変わり、リポジトリの内容と衝突し、`--confine-repo-settings`の対象にもなるため。

### D10. Slack Bot

silk-agentの`ConversationOrchestrator`の考え方（チャネル非依存のイベント、会話ごとの直列化、停止語、追加指示、1分ごとの更新）を、Managed Agentsのセッション操作に合わせて作り直す。

| silk-agent | satin-agent（(A)） |
| --- | --- |
| エージェントをBotのプロセスで実行 | セッションのイベント（`user.message`の送信、`events.stream`の購読）を送受信するだけ |
| 停止 ＝ SDKのinterrupt | `user.interrupt`イベント（スパイク1.3で確認済み。ユーザも採用に同意） |
| 追加指示 ＝ SDKへのストリーム入力 | 実行中に`user.message`を追加送信（キューされる動作をスパイクで確認） |
| Bot再起動でturnが中断 | turnはAnthropic側で続く。Botは`events.list`で取りこぼしを読み、streamを再購読して表示を再開する |
| ツールの承認 | （なし） | bashだけ`always_ask`にして、Botが`user.tool_confirmation`で自動応答する（D12）。ほかは`always_allow` |
| Sandboxの準備中 | オーケストレータがSandboxの状態をConfigMap（セッションID → 状態）に書き、Botが読む |
| 会話の保存 | Managed Agentsのセッション自体が履歴を持つ。Botはスレッド → セッションID、進捗メッセージの位置、最後に処理したイベントIDだけを持つ |

- **スパイク1.3の結果（2026-10-11、claude-haiku-5-5、手元のEnvironmentWorker）**:
  - `user.interrupt`は実行中のツールを中断できる。`agent.tool_result`が「Tool execution was interrupted before completion」になり、セッションは`end_turn`でidleに戻る。その後の`user.message`は通常どおり処理される。
  - **ツールの実行中は、`user.message`が400で拒否される。** メッセージは「waiting on responses to events [...]; only `user.tool_confirmation`, `user.custom_tool_result`, `user.tool_result`, `user.interrupt` may be sent」。このときセッションは`idle`（`stop_reason: requires_action`）に見えるので、`idle`はツール実行の完了を意味しない。
  - モデルが生成している最中（ツール待ちでない）の`user.message`は受け付けられ、キューされて、現在のturnの終了後に別のturnとして処理される。
  - 影響: 実行中の追加指示は、(a)モデル生成中ならそのまま送ればキューされるが、現在のturnには反映されない、(b)ツール実行中は送れない。Botは、ツール実行中の追加指示を自分でバッファして、turnの終了後に送るか、利用者に「中断するか」を確認して`user.interrupt`を使う。8.2・specの「実行中の追加指示」は、この制約に合わせて実装する。
  - 補足: `default_config`に`permission_policy: always_allow`がないと、組み込みツール（bash）はask扱いで承認待ちになる。`satin publish`のAgent設定に`always_allow`を入れた（bashだけ`bash_always_ask`で上書き）。
  - 補足: `EnvironmentWorker.run()`は1つのwork itemを処理している間、次のwork itemを取らない（常駐ワーカー1つにつき1セッション）。work itemのIDはセッションIDと同じ。
- Botの管理情報は、Bot PodのPVC上のSQLiteに置く（silk-agentと同じく1インスタンス前提）。
- `AGENT_USER_EMAIL`は、オーケストレータにAnthropic APIキーを持たせない（D7）という制約から、Botが共有の対応表に書き、オーケストレータがSandbox作成時に読む。対応表はKubernetes ConfigMap `satin-session-users`で、セッションID → メール、のHMAC付きである。対応表にないセッション（Console・スクリプトから作ったもの）は空にする。
- Slackとの接続はSocket Modeで、`slack-bolt`のasyncを使う。

### D11. パッケージ構成

```
src/satin_agent/
  assets/        アセットディレクトリの読み込み、publish（cma / gcs）
  sandbox/       ライフサイクル（テンプレート、Sandbox CR、Secret、NetworkPolicy、watch、保持期限、ログ）
  runtime/       Sandbox内のentrypoint（GCS展開、startup.sh、cmaワーカー、ccランナー、シグナル）
  cma/           (A)オーケストレータ
  cc/            (B) spawn-runnerフックとreconcile
  slack/         (A) Slack Bot（core / channel / store）
  guards/        (A) ルールのフィルタ、Gemini入力ガード、Model Armor出力検査、bashの一括取り出し判定
  hooks.py       (A) 監査フック（イベント、基底クラス、非同期配信、json_lines）
  cli.py         satin publish | sandbox | cma-orchestrator | cc spawn | cc reconcile | slack
docker/runtime.Dockerfile, docker/app.Dockerfile（オーケストレータとBotは同じイメージ）
k8s/base, k8s/overlays/dev
terraform/       GCSバケット、GSA/KSA、Workload Identity
assets/          サンプルのアセット（container-probe Skill、CLAUDE.md、startup.sh、mcp.json）
verification/    シナリオ実行スクリプトと検証記録
tests/           ユニットテスト（偽物のみ）。tests/live/ は実サービス（Anthropic、Jev、GKE）に接続するテスト
```

- 外部につながる部分（Kubernetes、Anthropic、GCS、Slack、時計）はportにして、ユニットテストでは偽物を使う（silk-agentと同じ方針）。

### D12. (A)のガード類はsilk-agentから移植し、Botの中で完結させる

`silk_agent/guards/`と`hooks.py`を`satin_agent/guards/`と`satin_agent/hooks.py`に移植する。判定の基準と振る舞い（specのslack-bot）はsilk-agentと同じにする。違うのは、つなぐ先がClaude Agent SDKではなくManaged Agentsのセッションのイベントになる点だけである。

- 入力ガード: Botがセッションへ`user.message`を送る直前に判定する。実行中の追加指示も同じ経路を通す。
- 出力検査: `agent.message`と、進捗メッセージに出すツールの要約を、Slackへ投稿する直前に検査する。セッションの中身には触れない。
- bashの実行前確認: silk-agentでは、Botのプロセスでツールを実行していたので、実行前に拒否できた。(A)ではbashがSandbox内のワーカーで実行されるため、Bot側で止める口はツール承認しかない。Agentの設定でbashの権限ポリシーだけを`always_ask`にし、`agent.tool_use`（`requires_action`）に対して、Botが`bulk_files`の判定で`allow` / `deny`（理由つき）を返す。
  - 代替案: Sandbox内のbashをラップして拒否する。却下の理由: Sandbox内はエージェントが自由に書き換えられるため、ガードとして信頼できない。
  - 代償として、bashのたびにBotとの往復が1回増える。Botが止まっている間は、bashが確認待ちで止まる。Botは起動時に、`requires_action`のまま残ったセッションを探して応答する（再起動からの復帰と同じ経路）。
- 認証: GeminiとModel Armorは、Bot PodのKSAのWorkload Identityで呼ぶ（silk-agentと同じ）。Model Armorのテンプレートは設定で指定する。個人情報のマスクには、DLPテンプレートつきのテンプレート（silk-agentの`silk-agent-test`相当）が必要である。
- フック: 観測専用で、非同期のキューで配信する（silk-agentと同じ）。フックにはbashの承認の判定も渡す。

### D13. テストの層とJev

- ユニットテスト（`uv run pytest`）: Kubernetes・Anthropic・GCS・Slack・Gemini・Model Armor・時計をすべて偽物にする。silk-agentの、停止語・追加指示・heartbeat・再起動からの復元・ガードのテストを、satin-agentの構成に合わせて移植する。
- 実サービスのテスト（`uv run pytest -m live`、`tests/live/`）: 実のManaged Agentsのセッション（`claude-haiku-5-5`）を使う。silk-agentの`integration`に相当する。
  - 応答の判定は、固有の値なら文字列一致、言い換えを許す内容や「拒否した理由を説明したか」ならJevで行う（`noul > 0.3`）。
  - Jevのクライアントは`tests/live/jev.py`に小さくまとめる（前回の`verify/jev.py`と同じ形）。判定値と`usage`を結果のJSONに残す。
- 検証スクリプト（`verification/run_cma.py`）も、同じ判定の関数を使う。
- APIキーの読み込み: `CLAUDE_API_KEY`を読んで、`anthropic.Anthropic(api_key=...)`へ明示的に渡す。`ANTHROPIC_API_KEY`があればそれも受け付ける。`JEV_API_TOKEN`は、liveのテストと検証スクリプトだけが読む。

## Risks / Trade-offs

- [ワーカーの通信とwork secretがSandbox内に入る] → secretはセッション限定の短命なものに限り、環境変数から除き、FQDNNetworkPolicyでAnthropic API・GCS・許可先以外を止める。それでもエージェントが`/proc`やマウントから読める可能性は残るので、検証で実測し、記録する。
- [Sandbox controllerの`restartPolicy`が使えない] → D2の代替（終了マーカー＋待機）で同じ振る舞いにする。スパイクで先に決める。
- [(B)のwork orderの渡し方が推定] → スパイクで確定する。合わなければ、Sandboxで`--lock-to-account`のrunnerを動かす方式（アカウント単位）に退避し、その制約（同じユーザのセッションが同じSandboxに乗る）を検証記録に書く。
- [コールドスタートの遅さ] → Sandboxの新規作成は約25秒（silk-agentの実測）。そこにイメージのpullとGCSの展開が加わる。(B)の`--expected-spawn-seconds`を180にし、(A)は`reclaim_older_than_ms`を起動時間より長くする。イメージはノードにキャッシュさせる。
- [(A)でSkillsを自発的に読まない] → システムプロンプトで場所を明示する。検証はHaikuと上位モデルの両方で行い、結果を記録する。
- [`user.interrupt`や実行中の`user.message`の振る舞いが想定と違う] → スパイクで確認し、違えばspecのシナリオを満たせないことを検証記録に残す（resilience-verificationの「満たせなかったケース」）。
- [bashのたびに承認の往復がある / Botの停止中はbashが止まる] → 承認の判定はローカルの正規表現なので速い。Botの再起動時に、確認待ちのセッションへ応答する（slack-botの「確認待ちの間のBotの再起動」のシナリオで確かめる）。
- [Jevの判定が厳格・曖昧] → 固有の値は文字列一致で判定し、Jevは補助に限る。判定値を記録して、閾値を見直せるようにする。
- [オーケストレータが単一レプリカ] → 停止中はwork itemが受け取られないだけで、再起動後に再配布される。CR上の楽観ロックで、将来の複数レプリカも可能にしておく。

## Migration Plan

新規のため移行はない。デプロイは次の順に行う。

1. terraformでGCS・GSAを用意する。
2. ランタイムイメージをビルドしてpushする。
3. `satin publish`でアセットを配布する。
4. Anthropic ConsoleでManaged Agentsのenvironment keyを発行する。claude.aiでClaude Codeのself-hosted Environmentを作ってsecretを発行する。どちらもk8sのSecretに入れる。
5. `k8s/overlays/dev`をapplyする。

ロールバックはDeploymentの削除で行う。残ったSandboxとPVCは、`satin sandbox gc --all`で削除する。

## Open Questions

- GKEクラスタの情報（クラスタ名、リージョン、Agent Sandbox controllerの版、Sandbox用のnamespace、FQDNNetworkPolicyの有無）。silk-agentでの構築後にユーザから受け取り、`k8s/overlays/dev`とterraformの変数に入れる。controllerの版が違う場合は、スパイク1.1をその版で行う。

- Managed Agentsのセッションを`always_allow`にしたとき、MCPツールの承認も不要になるか（ならなければBotが自動で`allow`を返す）。
- Sandboxのワークスペースの容量（既定10Gi）と保持期限（既定7日）の運用値。

## 実装中の決定（2026-10-11）

- **namespace**: オーケストレータとBotは`satin-system`、Sandboxは`satin-sandboxes`（silk-agentの`session-sandbox-system` / `session-sandboxes`とは別。ユーザが決定）。
- **Workload Identity**: GSAを作らず、silk-agentと同じくKSAを直接IAMのprincipalにする（`principal://.../subject/ns/<ns>/sa/<ksa>`）。Bot（`satin-slack-bot`）に`aiplatform.user` / `modelarmor.user`、Sandbox（`satin-sandbox`）にアセットのバケットの`objectViewer`。
- **Model Armor / DLPのテンプレート**: silk-agentのterraformと同じプロジェクト・リージョンにあるため、`satin-simple`・`satin-agent-pii-*`として別に作る。
- **追加指示（slack-botのspec）**: ツール実行中は`user.message`を送れない（1.3）。ツールを止めずに指示を届ける方法はなく、Managed Agentsの`bash`にはバックグラウンド実行もない。そのため、ツール実行・確認待ちの間の追加指示は、`user.interrupt`でツールを中断してから送る（ユーザが決定）。モデル生成中の追加指示はそのまま送り、現在のturnの後に続けて処理される。
- **(B)のオーケストレータ**: `--expected-spawn-seconds=180`、`--hook-timeout=55`。フックが何も起動しないと、`expected-spawn-seconds`ごとに同じセッションのhintが再発行される（1.2）。
- **デプロイスクリプト**: 誤ったクラスタへの適用を避けるため、`KUBE_CONTEXT`を必須にした（検証中に、似た名前の別クラスタを参照していたことがあった）。

## 実装中の決定: ワーカーが落ちたとき（2026-10-12、実クラスタで確認）

ツール実行中にSandboxのPodが消える（ノードの入れ替え・OOM・削除）と、ワーカーはツールの結果を返せないまま終わる。Managed Agentsのセッションは、ツールの結果待ちのまま、動かすワーカーがいなくなる。実測で次のことが分かった。

- SDKの`EnvironmentWorker.handle_item()`は、**キャンセルを含むどんな終わり方でも、最後にwork itemを`force-stop`する**（リース喪失時を除く）。停止されたwork itemは再配信されない。
- ワーカーが落ちた後、そのwork itemの**リースが約5分、サーバ側に残る**。その間、同じセッションの新しいwork itemは割り当てられない。再実行したワーカーは、最初のハートビート（`NO_HEARTBEAT`を期待）がリースと食い違って、リース喪失として手を引き、正常終了（0）する。ワーカー側からリースを引き継ぐ手段はない。
- オーケストレータの環境キーでは、セッションの操作（`user.interrupt`）はできない（403）。ワーカーのwork secretからは、セッション用トークンを取り出せば、イベントの取得と`user.tool_result`の送信はできる。ただし、エラーの結果を返すと、モデルが同じツールを呼び直して、また結果待ちになる（解決にならない）。
- `reclaim_older_than_ms`を短くしても、リースの失効（約5分）は変わらない。

これを踏まえた方針は次のとおり。

1. **SIGTERMでワーカーをキャンセルしない。** 猶予（45秒、Podの`terminationGracePeriodSeconds`は60秒）の間は、実行中のツールが終わって正常終了するのを待つ。間に合わなければ、後始末なしで即時終了（`os._exit(143)`）して、work itemを停止しない（再実行が引き継げる余地を残す）。
2. **異常終了したSandboxは、同じwork itemで再実行する**（最大2回）。ワーカーが最初のハートビートの前に落ちたとき等は、再実行で回復する。
3. **再実行したワーカーが手を引いた（work itemが`stopped`にならない）場合は、`failed`として公開する。** 理由は「ワーカーが処理を完了できませんでした（Podの異常終了の後）」。あわせて、落ちたワーカーのリースを解放するため、オーケストレータがwork itemを強制停止する（次の依頼のwork itemがすぐ割り当てられる）。
4. **Botは、`failed`を受けてturnを失敗にし、結果待ちのセッションに`user.interrupt`を送る**（次の依頼を受けられる状態に戻す）。Botのないセッション（ConsoleやAPI直接）は、失敗の公開までで、ユーザが新しいメッセージを送る必要がある。

したがって、このツール実行中のPod削除では、(a) 自動で回復して完了する、(b) 失敗が明示的に公開され、Botが通知して次の依頼を受けられる、のどちらかになり、黙って放置されることはない。
