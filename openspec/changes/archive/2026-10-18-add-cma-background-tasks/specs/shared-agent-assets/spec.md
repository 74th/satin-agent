# Spec Delta

## ADDED Requirements

### Requirement: バックグラウンド実行のツールのAgentへの宣言
配布スクリプトは、(A)のAgentに、ワーカーが提供するカスタムツール`run`・`task_status`・`task_wait`・`task_stop`を、ワーカーの実装と同じ名前と入力の形で宣言しなければならない (SHALL)。AgentのSystem promptには、これらのツールの使い方（シェルの状態を変える操作以外のコマンドには`run`を使うこと、結果をすぐに必要としなければバックグラウンドのタスクを待たずにturnを終えてよいこと、終了は知らされること）を加えなければならない (SHALL)。(B)には何も加えてはならない (SHALL NOT)。

#### Scenario: カスタムツールの宣言
- **WHEN** アセットを(A)へ配布する
- **THEN** Agentの新しいバージョンに、4つのカスタムツールと、その使い方を書いたsystem promptが含まれる

#### Scenario: 長いコマンドの扱い
- **WHEN** 配布したAgentのセッションで、「`sleep 120; echo done`を実行して、終わったら結果を教えて」と依頼する
- **THEN** エージェントは`run`でコマンドを実行し、バックグラウンドへ回った後にturnを終え、終わった後で結果を報告する
