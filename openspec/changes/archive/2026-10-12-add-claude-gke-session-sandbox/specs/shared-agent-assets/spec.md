# Spec Delta

## Purpose

Skills・CLAUDE.md・`startup.sh`を1つのアセットディレクトリで管理し、(A) Managed AgentsとGCSへ同じ内容を配布する。(A)と(B)のエージェントが同じSkillsと指示で動くようにする。

## ADDED Requirements

### Requirement: アセットディレクトリの構成
システムは、1つのアセットディレクトリから、Skills（`skills/<スキル名>/SKILL.md`と付属ファイルを持つディレクトリを複数）、`CLAUDE.md`、`startup.sh`を読み込まなければならない (SHALL)。アセットディレクトリの場所は配布スクリプトの引数で指定できなければならない (SHALL)。`SKILL.md`を持たないディレクトリが`skills/`の直下にある場合、配布スクリプトはAPIを呼ぶ前にエラーで止まらなければならない (SHALL)。

#### Scenario: 複数のSkillsの読み込み
- **WHEN** `skills/`の下に`report-writer/SKILL.md`と`bq-query/SKILL.md`があるアセットディレクトリを指定して配布スクリプトを実行する
- **THEN** 2つのSkillsが配布の対象になる

#### Scenario: SKILL.mdのないディレクトリ
- **WHEN** `skills/broken/`に`SKILL.md`がないアセットディレクトリを指定して配布スクリプトを実行する
- **THEN** スクリプトは`skills/broken`を名指ししたエラーで終了し、Anthropic APIとGCSのどちらにも書き込まない

### Requirement: Managed AgentsへのSkillsの登録
配布スクリプトは、各Skillを(A)のSkills APIへ登録しなければならない (SHALL)。登録済みのSkillは新しいバージョンとして追加し、同じ名前のSkillを重複して作ってはならない (SHALL NOT)。登録した全Skillを、バージョンを指定せず（latest）Agentへ付けなければならない (SHALL)。アセットディレクトリから消えたSkillは、Agentから外さなければならない (SHALL)。

#### Scenario: 2回目の配布
- **WHEN** `report-writer`を一度配布した後、その`SKILL.md`を変更して再度配布する
- **THEN** `report-writer`のSkillは1つのままで新しいバージョンが追加され、Agentは引き続きそのSkillをlatestで参照する

#### Scenario: Skillの削除
- **WHEN** 配布済みの`bq-query`をアセットディレクトリから削除して配布する
- **THEN** Agentの新しいバージョンの`skills`に`bq-query`が含まれない

### Requirement: CLAUDE.mdのAgentへの反映
配布スクリプトは、`CLAUDE.md`の内容を(A)のAgentのシステムプロンプトに反映し、Agentの新しいバージョンを作らなければならない (SHALL)。システムプロンプトには、Sandbox内のSkillsの置き場所をエージェントに知らせる定型の文を加えなければならない (SHALL)。

#### Scenario: CLAUDE.mdの更新
- **WHEN** `CLAUDE.md`を変更して配布スクリプトを実行する
- **THEN** Agentのバージョンが1つ上がり、そのシステムプロンプトに変更後の`CLAUDE.md`の内容とSkillsの置き場所の説明が含まれる

### Requirement: GCSへのアップロード
配布スクリプトは、Skills・`CLAUDE.md`・`startup.sh`を、設定したGCSの場所へ、アセットディレクトリと同じ相対パスでアップロードしなければならない (SHALL)。アップロードの完了後に、配布した内容の一覧とハッシュを記したマニフェストを最後に書き込まなければならない (SHALL)。GCS上にあってアセットディレクトリにないファイルは削除しなければならない (SHALL)。

#### Scenario: GCSへの配布
- **WHEN** 配布先を`gs://<bucket>/satin/default/`として配布する
- **THEN** `gs://<bucket>/satin/default/skills/report-writer/SKILL.md`、`.../CLAUDE.md`、`.../startup.sh`、`.../manifest.json`が存在し、マニフェストの各ハッシュはアップロードしたファイルと一致する

### Requirement: 配布先の選択と結果の記録
配布スクリプトは、(A)のManaged AgentsとGCSのそれぞれを配布先として個別に選べなければならない (SHALL)。作成・更新したリソースのID（Skill ID、Agent IDとバージョン）を状態ファイルに保存し、次回の実行で再利用しなければならない (SHALL)。状態ファイルとログにAPIキーなどの秘密情報を書いてはならない (SHALL NOT)。

#### Scenario: GCSだけへの配布
- **WHEN** 配布先にGCSだけを指定して配布スクリプトを実行する
- **THEN** GCSの内容は更新され、Managed AgentsのSkillとAgentのバージョンは変わらない
