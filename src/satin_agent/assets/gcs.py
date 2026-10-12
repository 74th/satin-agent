"""GCSへの配布。アセット全体を1つのzip（`assets.zip`）にして、1オブジェクトとして書く。"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from ..ports import ObjectStore
from .load import Assets

BUNDLE = "assets.zip"
# 同じ内容なら同じバイト列にするため、更新日時と権限を固定する
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def build_bundle(files: dict[str, bytes]) -> bytes:
    """相対パス → 内容 から、決定的なzipを作る。`#!`で始まるファイルは実行可能にする。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, _FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix
            info.external_attr = (0o100755 if data.startswith(b"#!") else 0o100644) << 16
            zf.writestr(info, data)
    return buf.getvalue()


def publish_gcs(assets: Assets, store: ObjectStore) -> dict[str, Any]:
    files = assets.files()
    bundle = build_bundle(files)
    store.write(BUNDLE, bundle)
    # assets.zip以外（従来のファイルとmanifest.jsonを含む）を削除する。書き込みが失敗したら消さない
    for key in store.list():
        if key != BUNDLE:
            store.delete(key)
    return {"bundle": BUNDLE, "files": sorted(files), "bytes": len(bundle)}
