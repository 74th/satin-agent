import pytest

from satin_agent.assets.gcs import publish_gcs
from satin_agent.assets.load import load_assets
from satin_agent.runtime.assets import AssetMismatchError, fetch_assets, place_for_claude_code
from tests.fakes import FakeObjectStore
from tests.test_assets import make_assets


def published(tmp_path):
    store = FakeObjectStore()
    publish_gcs(load_assets(make_assets(tmp_path / "src")), store)
    return store


def test_fetch_verifies_and_expands(tmp_path):
    store = published(tmp_path)
    got = fetch_assets(store, tmp_path / "ws/.satin/assets")
    assert (got.root / "CLAUDE.md").read_text() == "# rules\n"
    assert got.startup_sh and got.startup_sh.is_file()
    assert (got.root / "skills/report-writer/SKILL.md").is_file()


def test_mismatch_names_file_and_leaves_dest_untouched(tmp_path):
    store = published(tmp_path)
    store.data["CLAUDE.md"] = b"tampered"
    dest = tmp_path / "assets"
    with pytest.raises(AssetMismatchError) as e:
        fetch_assets(store, dest)
    assert e.value.files == ["CLAUDE.md"]
    assert "CLAUDE.md" in str(e.value)
    assert not dest.exists()


def test_missing_file_is_mismatch(tmp_path):
    store = published(tmp_path)
    del store.data["startup.sh"]
    with pytest.raises(AssetMismatchError) as e:
        fetch_assets(store, tmp_path / "a")
    assert e.value.files == ["startup.sh"]


def test_place_for_claude_code(tmp_path):
    store = published(tmp_path)
    got = fetch_assets(store, tmp_path / "assets")
    host = tmp_path / "claude-host"
    place_for_claude_code(got.root, host)
    assert (host / "skills/bq-query/SKILL.md").is_file()
    assert (host / "CLAUDE.md").read_text() == "# rules\n"
