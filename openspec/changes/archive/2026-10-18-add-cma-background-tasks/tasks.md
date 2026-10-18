# Tasks

## 1. スパイク（SDKの前提の確認）

- [x] 1.1 手元の`EnvironmentWorker`（`claude-haiku-5-5`）で、`tools=`に`beta_agent_toolset_20260401(env)`と試作の`run`を渡したワーカーを動かし、次を確かめる。結果をdesign.mdの決定1・3に追記する。
  - Agentに宣言した`custom`のツールを、ワーカーが実行して`user.custom_tool_result`を返す（work secretの権限で返せる）
  - `run`の実行中は、`user.message`が400になる（組み込みのbashと同じ）こと、`run`が返った後は受け付けられること
  - `_IdleClock`に`hold()`をかけている間は、turnが終わってもrunnerが止まらず、`release()`の`max_idle`秒後に止まること
  - runnerが止まらずに残っている間に送った`user.message`を、同じrunnerが処理すること
- [x] 1.2 終了メッセージ（`/dev/termination-log`）に書いたJSONが、Sandboxの停止後に`containerStatuses[].state.terminated.message`として読めることを、devのクラスタで確かめる（Pod削除の前に読めるか、オーケストレータの`exit_info()`の時点で確かめる）。

## 2. ワーカーのツール（cma-background-tasks）

- [x] 2.1 `runtime/bgtasks.py`に、タスクの記録（`/workspace/.satin/tasks/<id>/`の`meta.json`・`output.log`）と管理（起動・状態・待機・停止・上限時間・同時実行数の上限・起動時の消失の判定）を作る。`tests/test_runtime_bgtasks.py`で、実際のプロセスを使って、待ち時間内の終了、バックグラウンドへの切り替え、`task_wait`、`task_stop`による子プロセスを含む停止、上限時間、同時実行数の上限、作業ディレクトリの外の拒否、出力の切り詰め、消失の判定を確かめる。
- [x] 2.2 4つのツールの名前・説明・入力の形を1つの定義にまとめ、`@beta_async_tool`のツールを作る。終了コードが0以外の`run`はエラーの結果にする。環境変数はSDKの`_default_bash_env()`と同じものを使う。テストで、`env`の出力に`ANTHROPIC_`で始まる秘密が含まれないこと、ツールの入力の形が定義と一致することを確かめる。
- [x] 2.3 組み込みのツールを包み、伝えていないまま終わったタスクがあれば、次のツール結果の末尾に`[satin]`の知らせを1回だけ付ける。`task_status`・`task_wait`で終わりを返したら、伝えたものとして記録する。テストで、知らせが1回だけ付くことと、エラーの結果にも付くことを確かめる。

## 3. ワーカーの維持と終了時の報告

- [x] 3.1 `_IdleClock`を包んで時計を登録するモジュールを作り（`timeouts.py`と同じく起動時に適用）、実行中のタスクごとに`hold()`・`release()`する。時計が登録されないとワーカーの起動を失敗にする。SDKの実物の`_IdleClock`を使ったテストで、hold中は時計が進まず、最後の`release()`で進み始めることを確かめる。
- [x] 3.2 `runtime/modes.py`の`sdk_handle_item()`で、ツールを`tools=`に渡す。`run_cma()`で、`handle_item()`が返った後に、伝えていないまま終わったタスクを終了メッセージに書く（4KBを超えたら件数だけ）。SIGTERMの猶予の間に、実行中のタスクを止めて消失として記録する。`tests/test_runtime_entrypoint.py`などで、終了メッセージの内容と、SIGTERMでのタスクの停止を確かめる。

## 4. オーケストレータ（cma-sandbox-orchestrator）

- [x] 4.1 `sandbox/manager.py`の`ExitInfo`に終了メッセージを足し、`exit_info()`で読む。`cma/states.py`の`StatePublisher.set()`に`finished_tasks`を足し、`cma/orchestrator.py`で、終了メッセージのタスクの一覧を公開する（JSONとして読めなければ空にする）。`tests/test_sandbox_manager.py`・`tests/test_cma_orchestrator.py`で、タスクの一覧が状態の項目に入ること、壊れた終了メッセージでも状態が公開されることを確かめる。

## 5. Agentの宣言（shared-agent-assets）

- [x] 5.1 `assets/cma.py`の`agent_config()`で、2.2の定義から4つの`custom`ツールを宣言し、`build_system()`に使い方の節を足す。`tests/test_assets.py`（スナップショットがあれば更新）で、ツールの宣言とsystem promptの節が含まれ、ワーカーの定義と名前・形が一致することを確かめる。

## 6. Bot（slack-bot）

- [x] 6.1 `slack/core.py`の`append()`で、実行中のツールがすべてバックグラウンド実行のツール（`agent.custom_tool_use`の`run`・`task_*`）なら、中断せずに保持し、結果の後に`_flush_buffer()`で送る。`tests/test_slack_core.py`で、`run`の実行中の追加指示が中断なしで結果の後に送られることと、組み込みのbashの実行中は今どおり中断されることを確かめる。
- [x] 6.2 進捗メッセージに、`run`がバックグラウンドへ回したタスクを「バックグラウンドで実行中」と表示し、turnの完了の表示に残りの件数を出す（`run`の結果の定型の文からタスクIDを読む）。`tests/test_slack_core.py`で表示を確かめる。
- [x] 6.3 最後のturnでバックグラウンドのタスクが残ったセッションについて、状態のConfigMapを10秒ごとに読み、`finished_tasks`を見つけたら、知らせたタスクIDをSQLiteに記録してから`user.message`を送り、新しいturnを始める（turnの実行中なら追加指示として扱う）。監査フックに渡す。`tests/test_slack_core.py`で、1回だけ知らせることと、Botの再起動の後も2回送らないことを確かめる。`tests/fakes.py`の偽物の状態に`finished_tasks`を足す。

## 7. 文書と検証

- [x] 7.1 `README.md`の「Managed Agentsの制約」と追加指示の説明（56行目と121行目の食い違いも含む）を、バックグラウンド実行に合わせて直す。設定（`SATIN_BG_*`）、Sandboxの枠を使うこと、`run`がbashの実行前確認を通らないこと、配布の順序（design.mdのMigration Plan）を書く。`uv run pytest`と`uv run ruff check .`が通ることを確かめる。
- [x] 7.2 `verification/run_cma.py`に、specのシナリオに沿ったシナリオを足す（待ち時間を超える`run`、turnの後に終わるタスクの報告、`task_stop`、上限時間、Pod削除による消失）。devのクラスタで、Migration Planの順に入れ替えてから実行し、結果を`docs/verification.md`に記録する。
- [x] 7.3 Slackで、`sleep 120`を`run`で実行させている間（バックグラウンドへ回った後）に追加指示を出し、中断なしで処理されることと、タスクの終了がスレッドに報告されることを確かめ、`verification/manual_scenarios.md`と`docs/verification.md`に記録する。
