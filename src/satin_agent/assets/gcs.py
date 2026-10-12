"""GCSへの配布。manifest.jsonは最後に書く。"""

from __future__ import annotations

import json

from ..ports import NotFoundError, ObjectStore
from .load import Assets, sha256

MANIFEST = "manifest.json"


def build_manifest(files: dict[str, bytes]) -> dict:
    return {"files": {k: sha256(v) for k, v in sorted(files.items())}}


def publish_gcs(assets: Assets, store: ObjectStore) -> dict:
    files = assets.files()
    for key, data in files.items():
        store.write(key, data)
    # アセットにないファイルを削除（マニフェストの更新より前）
    for key in store.list():
        if key != MANIFEST and key not in files:
            store.delete(key)
    manifest = build_manifest(files)
    store.write(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode())
    return manifest


def read_manifest(store: ObjectStore) -> dict:
    try:
        return json.loads(store.read(MANIFEST))
    except NotFoundError:
        raise
