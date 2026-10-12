# Tasks

## 1. 配布（zipの作成）

- [x] 1.1 `assets/gcs.py`の`publish_gcs()`を変更する。`Assets.files()`を、パスの昇順・固定の更新日時・固定の権限（元の実行ビットは保つ）で1つのzipにして、`assets.zip`の1オブジェクトとして書く。`assets.zip`を書いた後に、それ以外のオブジェクトを全て削除する。`tests/test_assets.py`で次を確かめる。
  - `assets.zip`だけが残る
  - zipの中身がアセットと一致する
  - 従来のファイルと`manifest.json`が残っていると削除される
  - アセットを変えずに2回配布すると、同じバイト列になる
- [x] 1.2 `commands.py`の配布結果の表示を、ファイル数（zipのエントリ数）に変える。`publish_gcs()`の返り値を、マニフェストの辞書から、エントリ数などの結果に変える。既存の`satin publish`のテスト（`tests/test_cli_config.py`など）が通ることを確かめる。

## 2. Sandboxでの取得（zipの検査と展開）

- [x] 2.1 `runtime/assets.py`の`fetch_assets()`を変更する。`assets.zip`を1回で読み、展開前に、読み込み・`testzip()`・全パスの検査（絶対パス、`..`、バックスラッシュ）を行い、一時ディレクトリへ展開してから`.satin/assets`と入れ替える。失敗は`AssetBundleError`（`AssetMismatchError`から置き換える）にして、`entrypoint.py`の終了コード3を保つ。`tests/test_runtime_assets.py`で次を確かめる。
  - 正常に展開される
  - 壊れたzipで失敗し、前回の展開内容が変わらない
  - `../outside`のエントリで何も展開されず、エントリ名が出る
  - `assets.zip`がないと失敗する
- [x] 2.2 (B)で、展開したSkillsと`CLAUDE.md`が`place_for_claude_code`でホスト設定に置かれることと、`startup.sh`が実行されることを、`tests/test_runtime_entrypoint.py`にcc用のテストを足して確かめる（既存のテストはcmaだけだった）。

## 3. 文書と切り替え

- [x] 3.1 `README.md`のアセット配布の説明（`manifest.json`の記述）と、GCSの片付けの説明を、`assets.zip`の1オブジェクトに合わせて直す。`usecases/bigquery-analysis/README.md`に`manifest`の記述があれば直す。`grep -rn manifest README.md usecases/*/README.md docs`で、アセットのマニフェストの記述が残っていないことを確かめる。
- [x] 3.2 切り替えの手順（design.mdの決定5）をREADMEに書き、`uv run pytest`と`uv run ruff check .`が通ることを確かめる。

## 4. GKEでの確認

- [x] 4.1 既定の配備のクラスタで、手順どおりに切り替える（新しいイメージのpush → `satin publish --target gcs` → `scripts/deploy.sh` → `satin sandbox gc --all`）。`gcloud storage ls`で`assets.zip`だけが残ることと、(A)のセッションでSkill（`container-probe`）が使えることを確かめる。
- [x] 4.2 (B)（claude.aiのself-hosted Environment）でセッションを始め、SkillsとCLAUDE.mdが読み込まれることを確かめる。結果を`docs/verification.md`に記録する。
