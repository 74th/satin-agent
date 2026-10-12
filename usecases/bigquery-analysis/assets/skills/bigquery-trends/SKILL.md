---
name: bigquery-trends
description: Google Trendsの日本の急上昇検索語（直近2日・東京の日付・最大10行）をBigQueryの公開データから取得して説明する。急上昇している検索語や、日本のトレンドの分析を頼まれたときに使う。
---

# bigquery-trends

固定のSQLでBigQueryの公開データ `bigquery-public-data.google_trends.international_top_rising_terms` を読み、日本の急上昇検索語を説明する。

## 手順

1. 次のコマンドを実行する。引数はない。SQLは `query.sql` に固定されており、変更しない。
   ```
   python3 /workspace/skills/bigquery-trends/scripts/run_query.py
   ```
2. 終了コードが0なら、標準出力のJSONを読む。
   - `rows` に `refresh_date`・`rank`・`term` が入っている。`row_count` が0なら、該当するデータがなかったと伝える。検索語を作り出さない。
   - 条件は `conditions`、取得元は `source` に入っている。
3. 非0なら、標準エラーのJSON `error.category` を見る。結果は作らず、失敗したことと分類を利用者に伝える。
   - `config`: 設定の不足。`auth`: 認証の失敗。`permission`: 権限不足。`network`: 通信の失敗。`query`: クエリの失敗。

## 答え方

- 日本語で簡潔にまとめる。
- 取得した日付（`refresh_date`）・順位（`rank`）・検索語（`term`）を示す。
- 取得の条件（日本、東京の日付で直近2日、最大10行）を添える。
- 結果から確かめられない背景や原因を、事実として述べない。推測は推測と断る。
