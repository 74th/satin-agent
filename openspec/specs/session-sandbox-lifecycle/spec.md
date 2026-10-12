# session-sandbox-lifecycle Specification

## Purpose
セッションIDごとに1つのGKE Agent Sandboxを持ち、必要なときに起動し、使い終わったら停止し、保持期限が過ぎたら削除する。(A)と(B)の両方のオーケストレータが使う共通の仕組みである。

## Requirements

### Requirement: セッションごとに1つのSandbox
システムは、1つのセッションID（(A)はManaged AgentsのセッションID、(B)はClaude CodeのセッションID）につき、使用可能なSandboxを最大1つしか持ってはならない (SHALL)。同じセッションに対して同時に2回起動を指示された場合も、Sandboxは1つでなければならない (SHALL)。

#### Scenario: 同時の起動指示
- **WHEN** 同じセッションID`S`に対して、2つのオーケストレータのインスタンスがほぼ同時にSandboxの起動を指示する
- **THEN** `S`のSandboxは1つだけ作られ、もう一方の指示は既存のSandboxを使う

### Requirement: 起動と再開
システムは、セッションのSandboxがなければ新しく作り、停止中であれば同じワークスペースのまま再開しなければならない (SHALL)。起動時には、そのセッションの処理に必要な情報（モード、work itemまたはwork order、セッションを始めたユーザのメールアドレス）をコンテナへ渡さなければならない (SHALL)。work itemやwork orderの秘密は、Kubernetes Secretとして渡し、Sandboxのマニフェストやアノテーションに平文で書いてはならない (SHALL NOT)。

#### Scenario: idleの後の再開
- **WHEN** セッション`S`の1回目の処理で`/workspace/note.txt`を書き、Sandboxが停止した後、`S`に2回目の処理が来る
- **THEN** `S`のSandboxが再開され、2回目の処理で`/workspace/note.txt`を読める

### Requirement: 処理の終了による停止
システムは、Sandbox内のワーカーが終了したことを検出したら、そのSandboxを停止状態（Podを削除し、ワークスペースは保持）にしなければならない (SHALL)。ワーカーが異常終了した場合も停止状態にし、終了コードを記録しなければならない (SHALL)。

#### Scenario: 正常終了
- **WHEN** セッション`S`のワーカーが処理を終えて終了コード0で終了する
- **THEN** `S`のSandboxは停止状態になり、ワークスペースのPVCは残る

### Requirement: 保持期限と削除
システムは、停止状態になってから保持期限（既定7日）が過ぎたSandboxを、ワークスペースごと削除しなければならない (SHALL)。削除後に同じセッションに処理が来た場合は、新しいワークスペースでSandboxを作り、ワークスペースが新しくなったことを`startup.sh`とワーカーが分かるようにしなければならない (SHALL)。

#### Scenario: 期限切れの後の処理
- **WHEN** 保持期限を10秒に設定し、セッション`S`のSandboxが停止してから10秒以上たった後に`S`に処理が来る
- **THEN** 古いSandboxとPVCは削除されており、新しいSandboxが作られ、`startup.sh`には初回であることが渡される

### Requirement: 起動の失敗と上限時間
システムは、Sandboxが準備完了になるまでの上限時間（既定180秒）を超えた場合、または起動に失敗した場合、その処理を失敗として呼び出し元へ返さなければならない (SHALL)。失敗したSandboxは停止状態にしなければならない (SHALL)。

#### Scenario: 存在しないイメージ
- **WHEN** 存在しないイメージを設定してセッションのSandboxを起動する
- **THEN** 上限時間内に起動の失敗が呼び出し元へ返り、Sandboxは停止状態になる

### Requirement: 隔離
Sandboxは、gVisorで動かし、Kubernetes APIの資格情報をマウントしてはならない (SHALL NOT)。Sandboxからの外部への通信は、DNS、Anthropic API、GCS、および設定で許可した宛先だけに制限しなければならない (SHALL)。別のセッションのSandboxへ通信できてはならない (SHALL NOT)。

#### Scenario: 許可していない宛先
- **WHEN** エージェントにSandbox内で`curl https://example.com`を実行させる（許可先に含めていない）
- **THEN** 通信は失敗する

### Requirement: ライフサイクルの記録
システムは、Sandboxの作成・再開・準備完了・ワーカーの終了・停止・削除・失敗を、セッションID、Sandbox名、時刻、所要時間、終了コード、エラーを含む1行1JSONのログとして出力しなければならない (SHALL)。1回の起動につき、開始のイベントと終了のイベント（終了または失敗）が必ず対になっていなければならない (SHALL)。

#### Scenario: 開始と終了の対応
- **WHEN** 3つのセッションでそれぞれ2回ずつ処理を行う
- **THEN** ログには各セッションについて開始と終了のイベントが2組ずつ記録される
