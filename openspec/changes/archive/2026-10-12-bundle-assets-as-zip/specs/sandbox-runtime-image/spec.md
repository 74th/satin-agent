# Spec Delta

## REMOVED Requirements

### Requirement: GCSからのアセットの展開
**Reason**: アセットをファイルごとに取り出してマニフェストのハッシュと照合する方式から、1つのzipを取り出して検査する方式に変えるため。要件の名前と、シナリオ「マニフェストとの不一致」が、新しい方式に合わない。
**Migration**: 次の「GCSからのアセットのzipの展開」に置き換える。

## ADDED Requirements

### Requirement: GCSからのアセットのzipの展開
ランタイムイメージは、GCSの配布先が設定されている場合、ワーカーを起動する前に、配布先の`assets.zip`を1回で取り出し、Skillsと`CLAUDE.md`と`startup.sh`を展開しなければならない (SHALL)。展開は、一時的な場所で行い、検査に成功してから使う場所へ入れ替えなければならない (SHALL)。(B)のモードでは、展開したSkillsと`CLAUDE.md`を、すべてのClaude Codeのセッションが読み込む場所に配置しなければならない (SHALL)。`assets.zip`がない、zipとして読めない、中身が壊れている、または展開先の外へ出るパス（絶対パスや`..`を含むもの）を含む場合は、何も展開せず、ワーカーを起動せずに非0で終了しなければならない (SHALL)。

#### Scenario: (B)でのSkillsとCLAUDE.mdの読み込み
- **WHEN** GCSに`report-writer`のSkillと`CLAUDE.md`を配布した後、(B)のモードでSandboxを起動してセッションを始める
- **THEN** そのセッションのClaude Codeは`report-writer`をSkillとして列挙でき、`CLAUDE.md`の指示に従う

#### Scenario: zipが壊れている
- **WHEN** GCS上の`assets.zip`が壊れた状態でSandboxを起動する
- **THEN** コンテナはワーカーを起動せず、壊れていることを出力して非0で終了する。前回展開した内容は変わらない

#### Scenario: 展開先の外へ出るパス
- **WHEN** `assets.zip`に`../outside`のようなパスのエントリが含まれた状態でSandboxを起動する
- **THEN** コンテナは何も展開せず、そのエントリ名を出力して非0で終了する

#### Scenario: assets.zipがない
- **WHEN** 配布先に`assets.zip`がない状態でSandboxを起動する
- **THEN** コンテナはワーカーを起動せず、`assets.zip`がないことを出力して非0で終了する
