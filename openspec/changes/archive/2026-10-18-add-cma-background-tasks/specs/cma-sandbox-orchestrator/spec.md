# Spec Delta

## MODIFIED Requirements

### Requirement: エージェントループとツール実行の分担
システムは、エージェントループ（推論とツール呼び出しの決定）、MCPツール、`web_search`・`web_fetch`をAnthropic側で実行させ、組み込みのツール（bash、ファイルの読み書き・編集、glob、grep）と、ワーカーが提供するカスタムツール（`cma-background-tasks`の`run`・`task_status`・`task_wait`・`task_stop`）だけをSandbox内で実行させなければならない (SHALL)。Sandbox内にsandboxdのような別のコマンド実行サーバを置いてはならない (SHALL NOT)。

#### Scenario: MCPツールの呼び出し
- **WHEN** Agentに設定したHTTPのMCPサーバのツールをエージェントが呼び出す
- **THEN** 呼び出しは成功し、Sandboxのログにはその呼び出しの記録がない

#### Scenario: カスタムツールの呼び出し
- **WHEN** エージェントが`run`で`cat /etc/satin-image-id`を実行する
- **THEN** 結果にはランタイムイメージのビルド時に埋め込んだ識別子が含まれる

## ADDED Requirements

### Requirement: バックグラウンドのタスクの終了の公開
オーケストレータは、Sandbox内のワーカーが終了時に報告した、エージェントに伝えていないまま終わったバックグラウンドのタスクを、そのセッションの状態としてBot向けのConfigMapに公開しなければならない (SHALL)。報告がない場合や読めない場合は、タスクの項目を空にして、通常どおり状態を公開しなければならない (SHALL)。報告の内容に秘密を含めてはならない (SHALL NOT)。

#### Scenario: turnの外で終わったタスク
- **WHEN** エージェントが`sleep 60`をバックグラウンドへ回してturnを終え、タスクが終わってワーカーが終了する
- **THEN** 状態のConfigMapのそのセッションの項目は停止になり、そのタスクのIDと終了コードが含まれる
