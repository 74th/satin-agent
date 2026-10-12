# Spec Delta

## Purpose

(A) Claude Managed Agentsのself-hosted Environmentのwork queueを処理し、エージェントのツール実行（bash・ファイル操作）だけを、そのセッション専用のSandboxで行わせる。

## ADDED Requirements

### Requirement: work itemごとのSandbox起動
オーケストレータは、self-hosted Environmentのwork queueからセッションのwork itemを受け取るたびに、そのセッションのSandboxを起動し、Sandbox内のワーカーにそのwork itemを処理させなければならない (SHALL)。セッション以外の種類のwork itemには何もしてはならない (SHALL NOT)。

#### Scenario: Sandbox内でのツール実行
- **WHEN** Managed Agentsのセッションでエージェントに`cat /etc/satin-image-id`を実行させる
- **THEN** 結果にはランタイムイメージのビルド時に埋め込んだ識別子が含まれる（Anthropicのクラウドサンドボックスでは出ない値）

### Requirement: エージェントループとツール実行の分担
システムは、エージェントループ（推論とツール呼び出しの決定）、MCPツール、`web_search`・`web_fetch`をAnthropic側で実行させ、組み込みのツール（bash、ファイルの読み書き・編集、glob、grep）だけをSandbox内で実行させなければならない (SHALL)。Sandbox内にsandboxdのような別のコマンド実行サーバを置いてはならない (SHALL NOT)。

#### Scenario: MCPツールの呼び出し
- **WHEN** Agentに設定したHTTPのMCPサーバのツールをエージェントが呼び出す
- **THEN** 呼び出しは成功し、Sandboxのログにはその呼び出しの記録がない

### Requirement: 並行処理と上限
オーケストレータは、異なるセッションのwork itemを並行して処理しなければならない (SHALL)。同時に動かすSandboxの数は設定した上限（既定5）を超えてはならない (SHALL NOT)。上限に達している間は、新しいwork itemを受け取ってはならない (SHALL NOT)。

#### Scenario: 2つのセッションの並行処理
- **WHEN** 2つのセッションでほぼ同時にエージェントに`sleep 30`を実行させる
- **THEN** 2つのSandboxが同時に動き、両方のセッションがおよそ30秒で応答する

### Requirement: 資格情報の範囲
オーケストレータは、self-hosted Environmentのenvironment keyだけでwork queueを操作しなければならない (SHALL)。Anthropicの組織スコープのAPIキーを持ってはならない (SHALL NOT)。Sandboxへは、そのwork itemの処理に必要な秘密だけを渡さなければならない (SHALL)。

#### Scenario: APIキーなしでの起動
- **WHEN** `ANTHROPIC_API_KEY`を設定せず、environment IDとenvironment keyだけを設定してオーケストレータを起動する
- **THEN** オーケストレータは起動し、work itemを処理できる

### Requirement: オーケストレータの再起動
オーケストレータは、SIGTERMを受けたら新しいwork itemの受け取りを止め、起動済みのSandboxの処理は中断せずに終了しなければならない (SHALL)。再起動後は、動作中のSandboxの監視を引き継ぎ、ワーカーの終了を検出したらSandboxを停止しなければならない (SHALL)。オーケストレータが止まっている間に受け取られなかったwork itemは、再起動後に処理されなければならない (SHALL)。

#### Scenario: ツール実行中のオーケストレータの再起動
- **WHEN** エージェントが`sleep 60`を実行している間にオーケストレータのPodを削除する
- **THEN** ツールの実行は完了してエージェントは応答し、再起動したオーケストレータがその後にSandboxを停止する

### Requirement: Skillsの配置の確認
システムは、AgentにつけたSkillsが、Sandbox内のワーカーによって決まった場所へダウンロードされることを確認できなければならない (SHALL)。

#### Scenario: Skillの利用
- **WHEN** 配布済みのSkill`container-probe`を使うように依頼する
- **THEN** エージェントはSandbox内のSkillのファイルを読み、そのSkillの手順に従った応答を返す
