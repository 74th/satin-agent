import io
import zipfile

import pytest

from satin_agent.assets.gcs import publish_gcs
from satin_agent.assets.load import load_assets
from satin_agent.runtime.assets import AssetBundleError, fetch_assets, place_for_claude_code
from tests.fakes import FakeObjectStore
from tests.test_assets import make_assets


def published(tmp_path):
    store = FakeObjectStore()
    publish_gcs(load_assets(make_assets(tmp_path / "src")), store)
    return store


def zip_store(entries: dict[str, bytes]) -> FakeObjectStore:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return FakeObjectStore({"assets.zip": buf.getvalue()})


def test_fetch_validates_and_expands(tmp_path):
    store = published(tmp_path)
    got = fetch_assets(store, tmp_path / "ws/.satin/assets")
    assert (got.root / "CLAUDE.md").read_text() == "# rules\n"
    assert got.startup_sh and got.startup_sh.is_file()
    assert (got.root / "skills/report-writer/SKILL.md").is_file()
    assert not (got.root.parent / "assets.tmp").exists()


def test_fetch_keeps_executable_bit(tmp_path):
    src = make_assets(tmp_path / "src")
    (src / "skills/bq-query/run.py").write_text("#!/usr/bin/env python3\n")
    (src / "skills/bq-query/data.txt").write_text("x")
    store = FakeObjectStore()
    publish_gcs(load_assets(src), store)
    got = fetch_assets(store, tmp_path / "a")
    assert (got.root / "skills/bq-query/run.py").stat().st_mode & 0o111
    assert not (got.root / "skills/bq-query/data.txt").stat().st_mode & 0o111


def test_corrupt_zip_fails_and_leaves_previous_expansion_untouched(tmp_path):
    dest = tmp_path / "assets"
    fetch_assets(published(tmp_path), dest)
    store = published(tmp_path / "again")
    store.data["assets.zip"] = store.data["assets.zip"][:-30]  # 末尾を欠く
    with pytest.raises(AssetBundleError, match="zip"):
        fetch_assets(store, dest)
    assert (dest / "CLAUDE.md").read_text() == "# rules\n"


def test_damaged_entry_is_detected_by_crc(tmp_path):
    store = published(tmp_path)
    raw = bytearray(store.data["assets.zip"])
    # 圧縮データの先頭付近の1バイトを壊す（エントリの中身だけ壊れる）
    raw[40] ^= 0xFF
    store.data["assets.zip"] = bytes(raw)
    with pytest.raises(AssetBundleError):
        fetch_assets(store, tmp_path / "a")
    assert not (tmp_path / "a").exists()


@pytest.mark.parametrize(
    "name", ["../outside", "skills/../../outside", "/abs/path", "a\\b", "skills/ok/../../../x"]
)
def test_unsafe_paths_are_rejected_before_anything_is_written(tmp_path, name):
    store = zip_store({"CLAUDE.md": b"ok", name: b"bad"})
    dest = tmp_path / "ws" / "assets"
    with pytest.raises(AssetBundleError) as e:
        fetch_assets(store, dest)
    assert name in str(e.value)
    assert not dest.exists() and not (tmp_path / "ws" / "assets.tmp").exists()
    assert not (tmp_path / "outside").exists() and not (tmp_path / "ws" / "outside").exists()


def test_missing_bundle_fails(tmp_path):
    with pytest.raises(AssetBundleError, match="assets.zip"):
        fetch_assets(FakeObjectStore(), tmp_path / "a")


def test_legacy_layout_is_not_read(tmp_path):
    store = FakeObjectStore({"manifest.json": b"{}", "CLAUDE.md": b"x"})
    with pytest.raises(AssetBundleError):
        fetch_assets(store, tmp_path / "a")


def test_place_for_claude_code(tmp_path):
    store = published(tmp_path)
    got = fetch_assets(store, tmp_path / "assets")
    host = tmp_path / "claude-host"
    place_for_claude_code(got.root, host)
    assert (host / "skills/bq-query/SKILL.md").is_file()
    assert (host / "CLAUDE.md").read_text() == "# rules\n"
