"""Sandbox起動時のアセット展開（GCS → /workspace/.satin/assets/ → ホスト設定）。"""

from __future__ import annotations

import io
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..assets.gcs import BUNDLE
from ..ports import NotFoundError, ObjectStore


class AssetBundleError(Exception):
    """assets.zipがない・読めない・壊れている・不正なパスを含む。ワーカーは起動しない。"""


@dataclass(frozen=True)
class FetchedAssets:
    root: Path
    startup_sh: Path | None


def _check_names(zf: zipfile.ZipFile) -> None:
    """展開先の外へ出るパス（絶対パス・`..`・バックスラッシュ）や、通常のファイル以外を拒否する。"""
    bad = []
    for info in zf.infolist():
        name = info.filename
        parts = PurePosixPath(name).parts
        mode = info.external_attr >> 16
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or ".." in parts
            or (mode and not stat.S_ISREG(mode) and not info.is_dir())
        ):
            bad.append(name)
    if bad:
        raise AssetBundleError("不正なパスのエントリを含みます: " + ", ".join(sorted(bad)))


def fetch_assets(store: ObjectStore, dest: Path) -> FetchedAssets:
    """`assets.zip`を1回で取り出して検査し、一時ディレクトリへ展開してから`dest`と入れ替える。

    ない・壊れている・不正なパスを含むときは`AssetBundleError`（何も展開せず、ワーカーは起動しない）。
    """
    try:
        data = store.read(BUNDLE)
    except NotFoundError as e:
        raise AssetBundleError(f"{BUNDLE} がありません") from e
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise AssetBundleError(f"{BUNDLE} がzipとして読めません: {e}") from e
    with zf:
        _check_names(zf)
        if (broken := zf.testzip()) is not None:
            raise AssetBundleError(f"{BUNDLE} の中身が壊れています: {broken}")
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        for info in zf.infolist():
            if info.is_dir():
                continue
            p = tmp / info.filename
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(zf.read(info))
            p.chmod((info.external_attr >> 16) & 0o777 or 0o644)
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
