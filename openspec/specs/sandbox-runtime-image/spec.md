# sandbox-runtime-image Specification

## Purpose
(A)と(B)で共通に使うSandboxランタイムイメージの振る舞いを定める。起動時の準備（アセットの展開と`startup.sh`）と、モードごとのワーカーの起動・終了をそろえる。

## Requirements

### Requirement: 1つのイメージで2つのモード
ランタイムイメージは、起動時に指定されたモードに応じて、(A)のManaged Agentsのワーカー（1つのwork itemを処理して終了する）か、(B)のClaude Codeのrunner（1つのセッションを処理して終了する）のどちらかを起動しなければならない (SHALL)。モードの指定がない、または未知のモードの場合は、ワーカーを起動せずに非0で終了しなければならない (SHALL)。

#### Scenario: (A)のモード
- **WHEN** モードに`cma`を指定し、work itemの情報を与えてコンテナを起動する
- **THEN** コンテナはそのwork itemのツール実行を処理し、セッションがidleになると終了する

#### Scenario: 未知のモード
- **WHEN** モードに`foo`を指定してコンテナを起動する
- **THEN** コンテナは未知のモードであることを出力して非0で終了する

### Requirement: GCSからのアセットの展開
ランタイムイメージは、GCSの配布先が設定されている場合、ワーカーを起動する前に、マニフェストに記されたSkillsと`CLAUDE.md`と`startup.sh`をGCSから取り出さなければならない (SHALL)。(B)のモードでは、取り出したSkillsと`CLAUDE.md`を、すべてのClaude Codeのセッションが読み込む場所に配置しなければならない (SHALL)。マニフェストのハッシュと一致しないファイルがある場合は、ワーカーを起動せずに非0で終了しなければならない (SHALL)。

#### Scenario: (B)でのSkillsとCLAUDE.mdの読み込み
- **WHEN** GCSに`report-writer`のSkillと`CLAUDE.md`を配布した後、(B)のモードでSandboxを起動してセッションを始める
- **THEN** そのセッションのClaude Codeは`report-writer`をSkillとして列挙でき、`CLAUDE.md`の指示に従う

#### Scenario: マニフェストとの不一致
- **WHEN** GCS上の`CLAUDE.md`がマニフェストのハッシュと一致しない状態でSandboxを起動する
- **THEN** コンテナはワーカーを起動せず、不一致のファイル名を出力して非0で終了する

### Requirement: startup.shの実行
ランタイムイメージは、ワーカーを起動する前に、`startup.sh`（GCSから取り出したもの、なければイメージに含めたもの）を、ワークスペースをカレントディレクトリとして実行しなければならない (SHALL)。`startup.sh`には、モード、セッションID、ワークスペースの初回起動かどうか、セッションを始めたユーザのメールアドレス（分かる場合）を環境変数で渡さなければならない (SHALL)。`startup.sh`が非0で終了するか上限時間（既定300秒）を超えた場合は、既定ではワーカーを起動せずに非0で終了しなければならない (SHALL)。設定により、失敗してもワーカーを起動するようにできなければならない (SHALL)。

#### Scenario: 初回と再開
- **WHEN** 同じセッションのSandboxを2回起動する（idleで停止した後に再開する）
- **THEN** `startup.sh`は2回とも実行され、1回目には初回であること、2回目には初回でないことが渡される

#### Scenario: startup.shの失敗
- **WHEN** `startup.sh`が終了コード3で終わる
- **THEN** ワーカーは起動せず、コンテナは非0で終了し、`startup.sh`の失敗がログに残る

### Requirement: ワークスペースと資格情報
ランタイムイメージは、ツール実行の作業ディレクトリを、セッション専用の永続ワークスペース（`/workspace`）にしなければならない (SHALL)。コンテナは非rootユーザで動かなければならない (SHALL)。Anthropicの組織スコープのAPIキーをコンテナの環境変数・ファイルに置いてはならない (SHALL NOT)。ワーカーに渡すwork itemやwork orderの秘密は、ツール実行（bashなど）の環境変数から参照できてはならない (SHALL NOT)。

#### Scenario: 秘密がツールから見えない
- **WHEN** エージェントにSandbox内で`env`と`cat /proc/1/environ`を実行させる
- **THEN** 出力にAnthropicのAPIキー、environment key、work secret、work orderのいずれも含まれない

### Requirement: 終了シグナルの扱い
ランタイムイメージは、SIGTERMを受けたとき、ワーカーに処理中のツール実行の結果を送り終える機会を与えてから終了しなければならない (SHALL)。`startup.sh`の実行中にSIGTERMを受けた場合は、`startup.sh`のプロセスグループを止めてから終了しなければならない (SHALL)。

#### Scenario: ツール実行中のPod削除
- **WHEN** エージェントが`sleep 5`を実行している間にSandboxのPodに削除の指示が出る
- **THEN** ワーカーは猶予時間内に終了し、セッションのイベントにはツールの結果かエラーのどちらかが記録される（結果が記録されないまま放置されない）
