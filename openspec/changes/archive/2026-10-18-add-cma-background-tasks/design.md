# Design

## Context

- (A)のSandboxでは、`runtime/modes.py`の`sdk_handle_item()`が`EnvironmentWorker(client, workdir=..., max_idle=...)`を作り、`handle_item()`でwork itemを1つ処理する。`handle_item()`が返るとコンテナが終了コード0で終わり、オーケストレータの`SandboxManager.wait_exit()`が終了を検出して、Sandboxを停止する（Podを削除し、PVCは残す）。
- SDKの`EnvironmentWorker`は`tools=`（固定のリスト、またはセッションの`AgentToolContext`を受け取る関数）でカスタムツールを登録できる。既定は`beta_agent_toolset_20260401(env)`。
- SDKの組み込みbash（`anthropic/lib/tools/agent_toolset.py`）は、1本の`/bin/bash`を使い回し、タイムアウトするとそのbashごと殺す。環境変数は`_default_bash_env()`で秘密を除いたものを使う。
- アイドルでの停止は、`_beta_session_runner._IdleClock`が判定する。turnが終わると時計を進め、`max_idle`秒でrunnerを止める。確認待ちのツールのために`hold()`・`release()`があり、hold中は時計を進めない（最後の`release()`で、改めて時計を進め始める）。
- `runtime/timeouts.py`は、すでにSDKの定数（`BASH_DEFAULT_TIMEOUT`・`TOOL_TIMEOUT`）を、Sandboxの起動時に上書きしている。
- Botは、オーケストレータが書く状態のConfigMap（`satin-sandbox-states`）を、turnの間だけ読んでいる（`_poll_sandbox`）。追加指示には、保持（`buffer`）とツール完了後の送信（`_flush_buffer`）の仕組みがすでにある。

## Goals / Non-Goals

**Goals:**
- 長いコマンドで、1回のツール呼び出しとturnが長くならないようにする。
- バックグラウンドのタスクがturnの外でも動き続け、終わったらエージェントが報告できるようにする。
- 今の配置（オーケストレータはenvironment keyだけ、Sandboxにはkubernetesの資格情報なし、Botだけがセッションへ`user.message`を送る）を変えない。

**Non-Goals:**
- 組み込みのbashそのものの置き換えや、実行中のbashのコマンドを途中からバックグラウンドへ回すこと（案2。今回はしない）。
- `run`のコマンドに対する、Botの実行前確認（一括取り出しの判定）。カスタムツールには権限ポリシーが効かず、Sandbox内で実行されるので許容する。
- (B)（Claude Code）への同じ仕組みの追加。(B)は`run_in_background`を持っている。

## Decisions

### 1. ツールの形: 待ち時間つきの`run`と、`task_*`の3つ

`run(command, cwd="/workspace", wait_seconds=30)`、`task_status(task_id=None)`、`task_wait(task_id, seconds=60)`、`task_stop(task_id)`の4つを、`@beta_async_tool`で作り、`tools=lambda env: [*beta_agent_toolset_20260401(env), *bg_tools(env)]`でワーカーに渡す。

- 待ち時間の上限は60秒、`task_wait`の上限も60秒にする。Botは、これらのツールの実行中に受けた追加指示を、中断せずに最長60秒待って送る（spec: slack-bot）。
- 代替案: `run_in_background`のフラグをモデルに選ばせる（Claude Codeと同じ）。却下の理由: モデルが長くなるかどうかを読み違えると、今と同じ問題が残る。待ち時間で自動的に切り替えれば、判断が要らない。
- 代替案: 組み込みのbashを同名の実装で置き換える（案2）。却下の理由: SDKの内部に深く依存し、`cd`・`export`が引き継がれなくなる。モデルの使い方は今のbashのままにできるが、まず案1で効果を確かめる。

- **検証結果（スパイク1.1、2026-10-14、`claude-haiku-5-5`、本番と同じ経路の手元のワーカー）**:
  - Agentに宣言した`custom`ツール`run`を、ワーカーが実行し、`user.custom_tool_result`を返せた（work secretの権限。結果のイベントは`custom_tool_use_id`で元の呼び出しを指す。Botは`tool_use_id`ではなくこちらを読む）。
  - `run`の実行中（`agent.custom_tool_use`の後、結果の前）の`user.message`は、400（`waiting on responses to events`）で拒否された。組み込みのbashと同じ。`run`が返った後は受け付けられ、次のturnが処理された。

### 2. プロセスの起動と記録

- `asyncio.create_subprocess_exec("/bin/bash", "-c", command, start_new_session=True, stdin=DEVNULL, stdout=log, stderr=STDOUT, cwd=..., env=_default_bash_env())`で起動する。新しいセッション（プロセスグループ）にして、`task_stop`・上限時間・SIGTERMで、グループごと止める（SIGTERM、10秒後にSIGKILL）。
- 作業ディレクトリは、`AgentToolContext`と同じ判定（`resolve_path`）でワークスペースの中に限る。
- 記録の置き場所は`/workspace/.satin/tasks/<task_id>/`。`output.log`と`meta.json`（コマンド、作業ディレクトリ、PID、開始・終了の時刻、状態、終了コード、伝えたかどうか）を置く。PVCにあるので、Sandboxが停止して再開しても残る。タスクIDは`t-<連番>-<短い乱数>`とする。
- 結果に含める出力は、`run`が待ち時間内に終わった場合は末尾100KB（組み込みのbashの上限に合わせる）、`task_status`・`task_wait`は末尾8KBにする。切り詰めたら、そのことと`output.log`の場所を書く。
- 起動時、`meta.json`が実行中のまま残っているタスク（前のSandboxのもの）は消失にする（1つのSandboxは1つのセッションだけを処理するので、残っているのは前のSandboxのものに限られる）。

### 3. ワーカーの維持: `_IdleClock`のholdを使う

実行中のタスク1つにつき、runnerの`_IdleClock`に`hold()`を1回かけ、タスクが終わったら`release()`する。これにより、タスクが動いている間はturnが終わってもアイドルの時計が進まず、最後のタスクが終わると、改めて`max_idle`の時計が進み始める。ワーカーが動いている間に来た依頼は、同じrunnerが処理する（SDKの既存の動作）。

- runnerの`_IdleClock`を取得するために、`timeouts.py`と同じように、起動時に`_IdleClock.__init__`を包んで、作られた時計を登録する（Sandbox 1つにセッション1つなので、時計は1つ）。
- 代替案: `max_idle=None`にして、セッションのイベントを自前で読み、`handle_item()`を止める。却下の理由: `handle_item()`をキャンセルするとwork itemが`force-stop`され（modes.pyの記録）、正常な停止との違いを自前で扱う必要がある。holdは、SDKが確認待ちのために用意した仕組みそのもので、変更が小さい。
- 時計が登録されていない場合（SDKの変更で内部が変わった場合）は、ワーカーの起動時にエラーで止める（テストでも検出する）。黙ってタスクが殺される状態にはしない。

- **検証結果（スパイク1.1）**: `run`を待ち時間5秒でバックグラウンドに回してturnを終えた後、`max_idle`（10秒）を超えて25秒が過ぎてもワーカーは残り、その間に送った`user.message`（`task_status`の依頼）を同じrunnerが処理した。タスクが終わった約10秒後に`handle_item()`が返り、ワーカーは正常に終了した（`c.txt`も書かれた）。

### 4. 終了の知らせ

- **turnの中**: 伝えていないまま終わったタスクがあれば、次のツール結果の末尾に、`[satin] バックグラウンドのタスク t-3 が終了しました（終了コード 0）。task_statusで結果を確認できます。`のように付ける。組み込みのツールも対象にするため、`tools=`に渡す組み込みのツールを包む。付けるのは終わったときの1回だけにする（毎回付けると、モデルがプロンプトインジェクションを疑いやすい）。
- **turnの外**: ワーカーが終了するとき、伝えていないまま終わったタスクの一覧（ID、コマンドの先頭80文字、状態、終了コード）をJSONにして、Kubernetesの終了メッセージ（`/dev/termination-log`、4KBまで。超えたら件数だけにする）に書く。オーケストレータは、`exit_info()`で`containerStatuses[].state.terminated.message`を読み、状態のConfigMapの項目に`finished_tasks`として足す。書いた後、`meta.json`の「伝えた」は書き換えない（Botが知らせ、エージェントが`task_status`で読んだときに伝えたことになる）。
- **Bot**: スレッドに対応するセッションのうち、最後のturnの完了時にバックグラウンドのタスクが残っていたものだけ、ConfigMapを定期的に（10秒ごと）読む。`finished_tasks`を見つけたら、SQLiteに知らせたタスクIDを記録してから、`[自動通知] バックグラウンドのタスク t-3（`sleep 60; echo done`）が終了しました（終了コード 0）。task_statusで結果を確認し、依頼に沿って報告してください。`を`user.message`で送り、新しいturnを始める。記録により、Botが再起動しても2回は送らない。
- 代替案: ワーカーがwork secretで`user.message`を送る。却下の理由: work secretの権限では`user.interrupt`も401だった。送れたとしても、Sandbox（信頼しない側、D12）がユーザの権限で発言することになる。
- 代替案: ワーカーがKubernetesのConfigMapに直接書く。却下の理由: Sandboxにkubernetesの資格情報を渡さない（session-sandbox-lifecycleの隔離）。

### 5. 設定

ランタイムの環境変数で上書きできるようにする。`SATIN_BG_MAX_TASKS`（既定4）、`SATIN_BG_MAX_SECONDS`（既定7200）、`SATIN_BG_DEFAULT_WAIT`（既定30）。上限の60秒はコードの定数にする（Botの待ち時間と対になるため）。

### 6. Agentの宣言とsystem prompt

`assets/cma.py`の`agent_config()`で、4つのカスタムツールを宣言する。入力の形は、ワーカーの`@beta_async_tool`から作るのではなく、`runtime/bgtasks.py`に置く1つの定義から、ワーカーとAgentの両方が使う（名前と形の食い違いを防ぐ）。system promptには`SKILLS_NOTICE`と同じように、使い方の節を足す。

## Risks / Trade-offs

- [SDKの内部（`_IdleClock`）への依存] → 起動時に登録を確かめ、なければエラーで止める。ユニットテストでSDKの実物を使って検出する。SDKを上げるときの確認項目にする。
- [`run`はBotの一括取り出しの判定を通らない] → ユーザが許容した。監査フックとBotの進捗には`agent.custom_tool_use`として現れる。README・design.mdに記す。
- [バックグラウンドのタスクがSandboxの枠（既定5）を使い続ける] → タスクの上限時間（既定2時間）と同時実行数の上限で抑える。README・設定例に記す。
- [ツール結果の末尾の知らせを、モデルがインジェクションと疑う] → 終わったときの1回だけ、短く、`[satin]`で始める定型にする。シナリオで、知らせを読んで`task_status`を呼ぶことを確かめる。疑う場合は、turnの中での知らせをやめ、`task_status`だけにする。
- [モデルが`task_wait`を繰り返し、turnが長くなる] → system promptで、結果をすぐに必要としなければturnを終えるよう書く。`task_wait`は60秒で返るので、その間の追加指示は待って送れる。
- [終了メッセージの4KBの上限] → 超えたら件数だけを書き、Botの知らせは「n件のタスクが終わりました。task_statusで確認してください」にする。

## Migration Plan

1. 新しいランタイムイメージ（ツールを提供するワーカー）をpushし、`scripts/deploy.sh`でオーケストレータとBotを入れ替える。古いAgentはカスタムツールを宣言していないので、新しいワーカーと組み合わせても動作は変わらない。
2. `satin publish --target cma`で、カスタムツールとsystem promptをAgentに足す。
3. 戻すときは、古いAgentのバージョンへ戻してから（または`agent_config`を戻して`satin publish`してから）、イメージを戻す。逆の順にすると、エージェントが提供されないツールを呼んで、セッションが`requires_action`のまま止まる。
