# Proposal

## Why

アセット（Skills・`CLAUDE.md`・`startup.sh`・`mcp.json`）は、GCSへファイルごとに置き、最後に`manifest.json`（各ファイルのハッシュ）を書いている。Sandboxは起動のたびに、マニフェストを読み、ファイルを1つずつ取り出して照合する。

- ファイルが増えるほど、GCSへの要求が増える（Skillsの付属ファイルが多いと、起動が遅くなる）。
- 複数のオブジェクトを順に書くため、配布の途中にSandboxが起動すると、新旧が混ざった内容を見るおそれがある（マニフェストを最後に書くことで緩和しているが、ファイルの上書きは途中で見える）。
- 配布の仕組みを、「ファイル + マニフェスト」の2種類のオブジェクトを扱う複雑なものにしている。

アセットを1つのzipにまとめれば、取得も更新も1オブジェクトで済み、原子的になる。

## What Changes

- 配布スクリプトは、アセット全体を1つのzip（`assets.zip`）にして、設定したGCSの場所へ1オブジェクトとして書く。zipは、同じ内容なら同じバイト列になるように作る。
- 配布先の場所にある、`assets.zip`以外のオブジェクト（従来のファイルと`manifest.json`）は削除する。
- `manifest.json`は廃止する。壊れた配布は、zipの検査（読み込み・各エントリのCRC・不正なパスの拒否）で検出する。
- Sandboxのランタイムは、`assets.zip`を1回で取り出し、一時ディレクトリへ展開して検査してから、置き換える。検査に失敗したら、ワーカーを起動せずに非0で終了する。
- **BREAKING**: GCS上の配置が変わる。新しいイメージは古い配置（`manifest.json`と個別のファイル）を読めず、古いイメージは新しい配置を読めない。配布とイメージの入れ替えを続けて行い、残った古いSandboxは削除する（README・design.mdに手順を書く）。
- (A)のSkills API、`satin publish`の引数・設定、状態ファイル、アセットディレクトリの構成は変えない。GCSのIAM（読み取りだけ）も変えない。

## Capabilities

### New Capabilities

なし。

### Modified Capabilities

- `shared-agent-assets`: 「GCSへのアップロード」を、ファイルごとのアップロードとマニフェストから、1つのzipへの置き換えに変える。
- `sandbox-runtime-image`: 「GCSからのアセットの展開」を、マニフェストのハッシュ照合から、zipの取得・検査・展開に置き換える（要件を「GCSからのアセットのzipの展開」へ改名し、旧要件は削除する）。

## Impact

- 変更:
  - `src/satin_agent/assets/gcs.py`: zipの作成と書き込み。
  - `src/satin_agent/runtime/assets.py`: zipの取得・検査・展開。
  - `src/satin_agent/commands.py`: 配布結果の表示（ファイル数）。
  - テスト: `tests/test_assets.py`、`tests/test_runtime_assets.py`など。
  - `README.md`: アセットの配布の説明。
- 変更しない: `ObjectStore`のport、`GcsStore`、`satin publish`の引数、Terraform・IAM、(A)のSkills API、(B)の配置（`place_for_claude_code`）。
- 既存の配備への影響: 切り替えの間、古いSandboxは起動に失敗する（開発用クラスタでは保持期限が2時間なので、`satin sandbox gc`で消せる）。
