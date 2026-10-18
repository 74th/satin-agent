# Proposal

## Why

(A)では、Self-hosted Environmentのツール実行中、セッションが`requires_action`のままになり、`user.message`が400で拒否される（design.md D10・`docs/verification.md`）。そのため、長いコマンド（BigQueryのクエリ、ビルドなど）の実行中に追加指示を出すと、Botは`user.interrupt`でツールを止めるしかない。組み込みのbashには、Claude Codeの`run_in_background`にあたる機能がない（Managed Agentsの公式ドキュメントのTools・Self-hosted sandboxesで確認）。

Self-hosted Environmentのワーカーは、Sandbox内でカスタムツールを提供できる（公式の「Custom tools in self-hosted sandboxes」）。これを使って、「一定時間で終わらなければ、殺さずにバックグラウンドへ回す」ツールを用意すれば、1回のツール呼び出しは短く終わり、turnも短くなる。長い処理はturnの外で動き続け、追加指示は中断せずに届けられる。

## What Changes

- Sandbox内のワーカーに、カスタムツールを追加する:
  - `run`: コマンドを別のプロセスとして起動し、待ち時間（既定30秒、上限60秒）以内に終われば結果を返す。終わらなければ、**止めずに**タスクIDを返し、バックグラウンドで動かし続ける。
  - `task_status`・`task_wait`・`task_stop`: バックグラウンドのタスクの状態と出力の末尾を読む、終わるまで待つ（上限60秒）、止める。
  - 出力はワークスペースのファイルに書き、ツールの結果には末尾だけを返す。
- バックグラウンドのタスクが動いている間は、ワーカーはアイドルで終了せず、Sandboxも停止しない。タスクには上限時間（既定2時間）と同時実行数の上限（既定4）を設ける。
- タスクの終了の知らせ:
  - turnの途中で終わったら、次のツール結果の末尾に短く付ける。
  - turnの外で終わったら、ワーカーは終わったタスクを終了時に報告して終了し、オーケストレータが状態のConfigMapに公開する。Botはそれを読み、セッションへ`user.message`で知らせて、エージェントに結果を報告させる。
- 配布スクリプトは、(A)のAgentにこれらのカスタムツールを宣言し、system promptに使い方（シェルの状態を変える操作以外は`run`を使う、待つ必要がなければturnを終える）を書き加える。
- Botは、`run`・`task_*`の実行中に受けた追加指示は、中断せずにツールの結果を待ってから送る（これらのツールは最長60秒で返る）。組み込みのbashの実行中は、今どおり中断してから送る。進捗メッセージに、バックグラウンドで動いているタスクを表示する。
- カスタムツールには権限ポリシー（`always_ask`）が効かないため、`run`はBotのbashの実行前確認を通らない。Sandbox内で実行されることから、これを許容する（ユーザが判断）。
- **BREAKING（配布の順序）**: Agentにカスタムツールを宣言してから、ツールを提供しない古いイメージでセッションを処理すると、エージェントがツールを呼んだところでセッションが`requires_action`のまま止まる。新しいイメージを先に入れてから`satin publish`する。

## Capabilities

### New Capabilities

- `cma-background-tasks`: (A)のSandbox内のワーカーが提供する、待ち時間つきのコマンド実行とバックグラウンドのタスクの管理（起動・状態・待機・停止・出力・上限・終了の知らせ・ワーカーとSandboxの維持）。

### Modified Capabilities

- `cma-sandbox-orchestrator`: 「エージェントループとツール実行の分担」に、Sandbox内で実行するカスタムツールを加える。バックグラウンドのタスクの終了を、状態のConfigMapへ公開する要件を追加する。
- `shared-agent-assets`: (A)のAgentへの、バックグラウンド実行のカスタムツールの宣言と、system promptへの使い方の追記を追加する。
- `slack-bot`: 「実行中の追加指示」を、バックグラウンド実行のツールの実行中は中断せずに待って送るように変える。バックグラウンドのタスクの表示と、終了の通知を追加する。

## Impact

- 追加: `src/satin_agent/runtime/bgtasks.py`（タスクの管理とカスタムツール）、テスト。
- 変更:
  - `src/satin_agent/runtime/modes.py`: ワーカーへのツールの登録、終了時の報告。
  - `src/satin_agent/runtime/timeouts.py`と同じ方式で、SDKの`_IdleClock`に手を入れる（新しいモジュール）。
  - `src/satin_agent/sandbox/manager.py`・`src/satin_agent/cma/orchestrator.py`・`src/satin_agent/cma/states.py`: 終了時の報告の読み取りと公開。
  - `src/satin_agent/assets/cma.py`: カスタムツールの宣言とsystem prompt。
  - `src/satin_agent/slack/core.py`ほか: 追加指示の扱い、表示、終了の通知。
  - `README.md`、`docs/verification.md`、`verification/`のシナリオ。
- 同時に動くSandboxの数（既定5）に、バックグラウンドのタスクを待っているSandboxも数えられる。
- 依存の追加はない。SDKの内部（`_IdleClock`）への依存が増える。
