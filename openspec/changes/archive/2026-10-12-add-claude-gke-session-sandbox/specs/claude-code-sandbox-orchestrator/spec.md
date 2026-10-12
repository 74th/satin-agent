# Spec Delta

## Purpose

(B) Claude Codeのself-hosted Environment（claude.aiの画面から選ぶ）のセッションを、セッションごとに起動するSandbox内のrunnerで処理する。

## ADDED Requirements

### Requirement: セッションごとのSandbox起動
システムは、Claude Codeのself-hosted Environmentからセッションの起動の要求（spawn hint）を受けるたびに、そのセッションのSandboxを起動し、Sandbox内でそのセッション専用のrunnerを動かさなければならない (SHALL)。起動の要求への応答は、Sandboxの準備完了を待たずに、要求の上限時間（既定60秒）内に返さなければならない (SHALL)。

#### Scenario: claude.aiからのセッション開始
- **WHEN** claude.aiでこのself-hosted Environmentを選んでセッションを始め、`cat /etc/satin-image-id`を実行させる
- **THEN** 結果にはランタイムイメージの識別子が含まれ、そのセッション専用のSandboxが1つ作られている

### Requirement: 起動の失敗の伝え方
システムは、Sandboxの起動を受け付けられなかった場合、再試行できる失敗（一時的なKubernetes APIのエラーなど）と、再試行できない失敗（設定の誤りなど）を区別して返さなければならない (SHALL)。

#### Scenario: Kubernetes APIの一時的な失敗
- **WHEN** Sandboxの作成時にKubernetes APIが一時的にエラーを返す
- **THEN** 再試行できる失敗として返され、後の再試行でSandboxが作られる

### Requirement: idleでの停止と再開
Sandbox内のrunnerは、ユーザの入力が設定した時間（既定10分）なかった場合、セッションをAnthropic側で保留にして終了しなければならない (SHALL)。その後ユーザが同じセッションへ書き込んだ場合、システムは同じセッションのSandboxを同じワークスペースのまま再開しなければならない (SHALL)。

#### Scenario: idleの後の続き
- **WHEN** セッションでファイル`/workspace/note.txt`を書かせ、idleでSandboxが停止した後に「note.txtの内容は？」と書き込む
- **THEN** Sandboxが再開され、エージェントは`note.txt`の内容を答える

### Requirement: SkillsとCLAUDE.mdの共通化
Sandbox内のClaude Codeのセッションは、GCSに配布したSkillsと`CLAUDE.md`を読み込まなければならない (SHALL)。セッションのリポジトリのチェックアウトの有無にかかわらず読み込まなければならない (SHALL)。

#### Scenario: リポジトリなしのセッション
- **WHEN** リポジトリを指定せずにセッションを始め、利用できるSkillsを尋ねる
- **THEN** エージェントはGCSに配布したSkillsを列挙する

### Requirement: runnerの資格情報
システムは、self-hosted Environmentのenvironment secretを、起動の要求を受けるオーケストレータだけに置かなければならない (SHALL)。Sandboxへは、そのセッション専用のwork orderだけを渡さなければならない (SHALL)。

#### Scenario: Sandboxにenvironment secretがない
- **WHEN** セッション内でエージェントにSandboxの環境変数とファイルを探させる
- **THEN** environment secretは見つからない

### Requirement: 停止時の扱い
Sandbox内のrunnerは、SIGTERMを受けたら実行中のturnの終了を設定した時間（既定60秒）だけ待ち、セッションを保留にしてから終了しなければならない (SHALL)。保留にしたセッションは、次のユーザの書き込みで新しいSandbox（同じワークスペース）から再開できなければならない (SHALL)。

#### Scenario: ノードの入れ替えによるPodの終了
- **WHEN** セッションがユーザの入力を待っている間にSandboxのPodを削除する
- **THEN** セッションは保留になり、次の書き込みでSandboxが再開されて会話が続く
