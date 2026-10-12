#!/usr/bin/env python3
"""固定のSQL（../query.sql）をBigQueryで実行し、結果をJSONで標準出力に出す。

- 認証はApplication Default Credentials。ジョブのプロジェクトは環境変数BIGQUERY_JOB_PROJECT。
- SQLは引数で受け取らない（利用者の入力からSQLを組み立てない）。
- 失敗時は成功の結果を出さず、分類付きのJSONを標準エラーに出して非0で終了する。
  終了コード: 2=config 3=auth 4=permission 5=network 6=query
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

SQL_PATH = Path(__file__).resolve().parent.parent / "query.sql"
DEFAULT_MAX_BYTES_BILLED = 10 * 1024**3
EXIT_CODES = {"config": 2, "auth": 3, "permission": 4, "network": 5, "query": 6}


def classify(exc: BaseException) -> str:
    from google.api_core import exceptions as api
    from google.auth import exceptions as auth

    if isinstance(exc, auth.GoogleAuthError | api.Unauthorized):
        # RefreshErrorなどの認証の失敗。TransportErrorは通信の失敗として下で扱う
        if not isinstance(exc, auth.TransportError):
            return "auth"
    if isinstance(exc, api.Forbidden):
        return "permission"
    if isinstance(
        exc,
        auth.TransportError
        | api.ServiceUnavailable
        | api.GatewayTimeout
        | api.RetryError
        | api.DeadlineExceeded
        | ConnectionError
        | TimeoutError,
    ):
        return "network"
    return "query"


def _json_default(o: Any) -> str:
    if isinstance(o, datetime.date | datetime.datetime):
        return o.isoformat()
    return str(o)


def _default_client(project: str, max_bytes: int):
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes, use_legacy_sql=False)
    return client, config


def run(
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    client_factory: Callable[[str, int], Any] = _default_client,
) -> int:
    project = env.get("BIGQUERY_JOB_PROJECT", "")
    if not project:
        return _fail(err, "config", "環境変数 BIGQUERY_JOB_PROJECT が設定されていません")
    max_bytes = int(env.get("BIGQUERY_MAX_BYTES_BILLED") or DEFAULT_MAX_BYTES_BILLED)
    sql = SQL_PATH.read_text()
    try:
        client, job_config = client_factory(project, max_bytes)
        result = client.query(sql, job_config=job_config).result()
        columns = [f.name for f in result.schema]
        rows = [{c: row[c] for c in columns} for row in result]
    except Exception as e:  # noqa: BLE001  失敗は分類して必ず非0で終える
        return _fail(err, classify(e), f"{type(e).__name__}: {e}")
    json.dump(
        {
            "source": "bigquery-public-data.google_trends.international_top_rising_terms",
            "conditions": (
                "country_name='Japan', refresh_date >= CURRENT_DATE('Asia/Tokyo') - 1 day"
            ),
            "job_project": project,
            "columns": columns,
            "row_count": len(rows),
            "rows": rows,
        },
        out,
        ensure_ascii=False,
        default=_json_default,
    )
    out.write("\n")
    return 0


def _fail(err: TextIO, category: str, message: str) -> int:
    json.dump({"error": {"category": category, "message": message}}, err, ensure_ascii=False)
    err.write("\n")
    return EXIT_CODES[category]


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description="Google Trendsの日本の急上昇検索語（固定のSQL）をBigQueryで取得し、"
        "JSONを出力する。引数はない。"
        "環境変数: BIGQUERY_JOB_PROJECT（必須）、BIGQUERY_MAX_BYTES_BILLED（任意）"
    ).parse_args(argv)
    return run(os.environ, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
