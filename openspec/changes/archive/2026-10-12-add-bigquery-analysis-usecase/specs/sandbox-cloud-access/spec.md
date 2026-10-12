# Spec Delta

## Purpose

ユースケースが必要とする場合に限り、会話ごとのSandboxからGoogle Cloud APIを呼べるようにする。そのSandboxには、専用のKubernetes ServiceAccountと、明示した宛先だけを与える。

## ADDED Requirements

### Requirement: Sandboxのクラウド認証を分ける
BigQueryユースケースは、ユースケース専用のTerraformで次のものを管理しなければならない (SHALL)。
- 専用のGoogle Cloud Service Account（GSA）
- そのGSAへの、BigQueryのジョブの実行権限とアセットのバケットの読み取り権限
- 専用のKubernetes ServiceAccount（KSA）だけに与えるWorkload Identityのなりすまし許可

ユースケースのSandboxのPodは、そのKSAを使わなければならない (SHALL)。Botやオーケストレータの資格情報を共有してはならない (SHALL NOT)。既定の`satin-sandbox`のKSAで動くSandboxにBigQueryの権限を与えてはならない (SHALL NOT)。静的なサービスアカウント鍵を、イメージやワークスペースに含めてはならない (SHALL NOT)。KSAのKubernetes APIトークンはマウントしてはならない (SHALL NOT)。

#### Scenario: BigQuery用のSandboxを起動する
- **WHEN** 次の順に準備してから、会話のSandboxを作る
  1. Terraformを適用する
  2. 出力されたGSAを注釈に持つKSAを適用する
  3. ユースケースの設定でBotとオーケストレータを配備する
- **THEN** PodはそのKSAで起動する。Workload Identityを通じてGSAとして、アセットを取得し、BigQueryのジョブを実行できる

#### Scenario: KSAとGSAの関連付けを確かめる
- **WHEN** 管理者が、Terraformの出力とKubernetesに適用したKSAを見比べる
- **THEN** KSAのGSAの注釈と、Terraformがなりすましを許可したnamespace・KSAの名前が一致する

#### Scenario: 既定の構成
- **WHEN** 既定の設定（`k8s/overlays/dev`）でSandboxを起動する
- **THEN** SandboxはBigQueryのジョブを実行できない

### Requirement: 外向き通信を明示して許可する
Sandboxの外向き通信は、既定の制限を保たなければならない (SHALL)。許可するのは、DNS、Anthropic API、GCS、メタデータサーバ、設定で許可した宛先だけである。BigQueryユースケースは、BigQuery APIの宛先をユースケースの設定で許可しなければならない (SHALL)。

#### Scenario: BigQueryユースケースの通信
- **WHEN** ユースケースの設定で`bigquery.googleapis.com`を許可してSandboxを起動する
- **THEN** そのSandboxからBigQuery APIへ通信できる。許可していない宛先へは従来どおり通信できない
