# Spec Delta

## MODIFIED Requirements

### Requirement: GCSへのアップロード
配布スクリプトは、Skills・`CLAUDE.md`・`startup.sh`・`mcp.json`（あれば）を、アセットディレクトリと同じ相対パスで1つのzipにまとめ、設定したGCSの場所へ1つのオブジェクト`assets.zip`としてアップロードしなければならない (SHALL)。zipの内容が同じなら、バイト列も同じでなければならない (SHALL)。配布先の場所にある`assets.zip`以外のオブジェクト（従来のファイルや`manifest.json`を含む）は、アップロードの完了後に削除しなければならない (SHALL)。

#### Scenario: GCSへの配布
- **WHEN** 配布先を`gs://<bucket>/satin/default/`として配布する
- **THEN** `gs://<bucket>/satin/default/assets.zip`だけが存在し、zipには`skills/report-writer/SKILL.md`、`CLAUDE.md`、`startup.sh`がアセットディレクトリと同じ相対パスで含まれる

#### Scenario: 従来の配置からの切り替え
- **WHEN** 配布先に従来のファイルと`manifest.json`が残っている状態で配布する
- **THEN** `assets.zip`が書き込まれ、従来のファイルと`manifest.json`は削除される

#### Scenario: 同じ内容の再配布
- **WHEN** アセットを変更せずに、配布スクリプトを2回実行する
- **THEN** どちらも同じ内容の`assets.zip`になる
