# Spec Delta

## Purpose

会話ごとのSandboxからBigQueryの公開データを読み、Skillの手順に従って検証できる分析結果を返すサンプルユースケースを提供する。(A) Slack Botと(B) Claude Codeの両方で使える。

## ADDED Requirements

### Requirement: 指定されたGoogle Trendsのクエリを実行できる
ユースケースは、Skillの付属ファイルとしてBigQueryの実行スクリプトと固定のSQLを提供しなければならない (SHALL)。SQLの条件は次のとおり。
- 対象は`bigquery-public-data.google_trends.international_top_rising_terms`の日本の行
- 期間は直近2日。日付の計算には`Asia/Tokyo`を用いなければならない (SHALL)。
- `refresh_date`・`rank`・`term`を重複なく取る
- 日付の降順、順位の昇順で並べ、最大10行に限る

スクリプトは、Sandboxに与えた資格情報（Application Default Credentials）で実行しなければならない (SHALL)。結果は、列名の付いた機械で読める形式で出力しなければならない (SHALL)。利用者の入力からSQLを組み立ててはならない (SHALL NOT)。

#### Scenario: クエリの実行に成功する
- **WHEN** 利用者が日本の直近の急上昇検索語を尋ねる。BigQueryへの認証と通信は利用できる
- **THEN** エージェントはSkillのスクリプトを実行し、取得した行に基づいて答える

#### Scenario: 取得した行がない
- **WHEN** クエリは成功するが、結果が0行である
- **THEN** エージェントは該当するデータがなかったことを伝え、検索語を作り出さない

### Requirement: Skillに従って結果を説明する
ユースケースは、スクリプトの使い方と結果の読み方を記したSkillを提供しなければならない (SHALL)。あわせて、分析を頼まれたときにそのSkillを使うよう指示する`CLAUDE.md`も提供しなければならない (SHALL)。回答には、取得した日付・順位・検索語と、取得の条件を示さなければならない (SHALL)。結果からは確かめられない背景や原因を、事実として述べてはならない (SHALL NOT)。

#### Scenario: 分析を依頼する
- **WHEN** 利用者がSlackで、日本の急上昇検索語の分析を依頼する
- **THEN** エージェントはSkillの手順でスクリプトを実行し、取得した結果に対応した日本語の要約を返す

### Requirement: 実行の失敗を結果と区別する
スクリプトは、認証・権限・通信・クエリのどれかが失敗したとき、成功の結果を出力してはならない (SHALL NOT)。代わりに、原因の分類が分かるエラーを出力し、非0で終了しなければならない (SHALL)。エージェントは、その失敗を利用者へ明示しなければならない (SHALL)。

#### Scenario: BigQueryのジョブの権限がない
- **WHEN** 認証の主体に、クエリを実行する権限がない
- **THEN** スクリプトは権限不足として非0で終了し、エージェントは分析結果を作らずに権限不足を伝える

#### Scenario: 通信できない
- **WHEN** SandboxからBigQuery APIへの通信が許可されていない
- **THEN** スクリプトは通信の失敗として非0で終了し、エージェントは結果を作らずに失敗を伝える
