"""Sandbox起動時のアセット展開（GCS → /workspace/.satin/assets/ → ホスト設定）。"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..assets.load import sha256
from ..ports import NotFoundError, ObjectStore


class AssetMismatchError(Exception):
    def __init__(self, files: list[str]) -> None:
        super().__init__("マニフェストと一致しないファイル: " + ", ".join(files))
        self.files = files


@dataclass(frozen=True)
class FetchedAssets:
    root: Path
    startup_sh: Path | None


def fetch_assets(store: ObjectStore, dest: Path) -> FetchedAssets:
    """マニフェストに記されたファイルを取り出し、ハッシュを照合する。

    一致しないファイルがあれば`AssetMismatchError`（ワーカーは起動しない）。
    展開は一時ディレクトリで行い、照合に成功してから`dest`と入れ替える。
    """
    manifest = json.loads(store.read("manifest.json"))
    files: dict[str, bytes] = {}
    bad: list[str] = []
    for key, expected in sorted(manifest["files"].items()):
        try:
            data = store.read(key)
        except NotFoundError:
            bad.append(key)
            continue
        if sha256(data) != expected:
            bad.append(key)
        files[key] = data
    if bad:
        raise AssetMismatchError(bad)

    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    for key, data in files.items():
        p = tmp / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)
    startup = dest / "startup.sh"
    return FetchedAssets(dest, startup if startup.is_file() else None)


def place_for_claude_code(assets_root: Path, host_config_dir: Path) -> None:
    """(B): すべてのセッションが読むホスト設定ディレクトリへSkillsとCLAUDE.mdを置く。"""
    skills_src = assets_root / "skills"
    skills_dst = host_config_dir / "skills"
    if skills_src.is_dir():
        shutil.rmtree(skills_dst, ignore_errors=True)
        shutil.copytree(skills_src, skills_dst)
    claude_md = assets_root / "CLAUDE.md"
    if claude_md.is_file():
        host_config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(claude_md, host_config_dir / "CLAUDE.md")
