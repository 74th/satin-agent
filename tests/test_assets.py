import json

import pytest

from satin_agent.assets.cma import State, publish_cma
from satin_agent.assets.gcs import publish_gcs
from satin_agent.assets.load import AssetError, load_assets
from tests.fakes import FakeAnthropic, FakeObjectStore


def make_assets(root, skills=("report-writer", "bq-query"), claude_md="# rules\n"):
    for s in skills:
        (root / "skills" / s).mkdir(parents=True, exist_ok=True)
        (root / "skills" / s / "SKILL.md").write_text(f"---\nname: {s}\n---\nbody\n")
    (root / "CLAUDE.md").write_text(claude_md)
    (root / "startup.sh").write_text("#!/bin/sh\necho hi\n")
    return root


def test_loads_multiple_skills(tmp_path):
    a = load_assets(make_assets(tmp_path))
    assert sorted(a.skills) == ["bq-query", "report-writer"]
    assert a.claude_md == "# rules\n"
    assert a.startup_sh


def test_skill_without_skill_md_errors_before_any_write(tmp_path):
    make_assets(tmp_path)
    (tmp_path / "skills" / "broken").mkdir()
    (tmp_path / "skills" / "broken" / "README").write_text("x")
    with pytest.raises(AssetError, match="skills/broken"):
        load_assets(tmp_path)


def test_stdio_mcp_rejected(tmp_path):
    make_assets(tmp_path)
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcp_servers": [{"name": "x", "command": "foo"}]})
    )
    with pytest.raises(AssetError, match="stdio"):
        load_assets(tmp_path)


def _bundle(store):
    import io
    import zipfile

    return zipfile.ZipFile(io.BytesIO(store.data["assets.zip"]))


def test_gcs_publish_writes_single_zip_and_prunes_legacy(tmp_path):
    assets = load_assets(make_assets(tmp_path))
    store = FakeObjectStore(
        {"skills/old/SKILL.md": b"x", "manifest.json": b"{}", "CLAUDE.md": b"legacy"}
    )
    result = publish_gcs(assets, store)
    assert set(store.data) == {"assets.zip"}
    assert result["bundle"] == "assets.zip" and result["bytes"] == len(store.data["assets.zip"])
    zf = _bundle(store)
    assert sorted(zf.namelist()) == sorted(assets.files()) == sorted(result["files"])
    assert zf.testzip() is None
    for name, data in assets.files().items():
        assert zf.read(name) == data
    assert "skills/report-writer/SKILL.md" in zf.namelist()
    assert "manifest.json" not in zf.namelist()


def test_gcs_publish_is_deterministic(tmp_path):
    assets = load_assets(make_assets(tmp_path))
    a, b = FakeObjectStore(), FakeObjectStore()
    publish_gcs(assets, a)
    publish_gcs(assets, b)
    assert a.data["assets.zip"] == b.data["assets.zip"]


def test_gcs_publish_keeps_legacy_objects_if_the_write_fails(tmp_path):
    assets = load_assets(make_assets(tmp_path))
    store = FakeObjectStore({"manifest.json": b"{}"})

    def boom(key, data):
        raise OSError("write failed")

    store.write = boom
    with pytest.raises(OSError):
        publish_gcs(assets, store)
    assert "manifest.json" in store.data


def test_gcs_bundle_marks_shebang_files_executable(tmp_path):
    root = make_assets(tmp_path)
    (root / "skills" / "bq-query" / "run.py").write_text("#!/usr/bin/env python3\nprint(1)\n")
    (root / "skills" / "bq-query" / "data.txt").write_text("plain\n")
    store = FakeObjectStore()
    publish_gcs(load_assets(root), store)
    zf = _bundle(store)
    mode = lambda n: zf.getinfo(n).external_attr >> 16  # noqa: E731
    assert mode("skills/bq-query/run.py") & 0o111
    assert not mode("skills/bq-query/data.txt") & 0o111


def test_cma_second_publish_adds_version_not_duplicate_skill(tmp_path):
    root = make_assets(tmp_path / "a")
    api, state = FakeAnthropic(), State(tmp_path / ".satin-state.json")
    a1 = publish_cma(load_assets(root), api, state)
    (root / "skills" / "report-writer" / "SKILL.md").write_text("changed")
    a2 = publish_cma(load_assets(root), api, state)
    assert len(api.skills) == 2
    rw = state.data["skills"]["report-writer"]
    assert len(api.skills[rw]["versions"]) == 2
    assert a2["version"] == a1["version"] + 1
    skills = api.agents[a2["id"]]["config"]["skills"]
    assert all("version" not in s for s in skills)


def test_cma_skill_removed_and_system_prompt(tmp_path):
    root = make_assets(tmp_path / "a")
    api, state = FakeAnthropic(), State(tmp_path / "s.json")
    publish_cma(load_assets(root), api, state)
    import shutil

    shutil.rmtree(root / "skills" / "bq-query")
    (root / "CLAUDE.md").write_text("# new rules\n")
    a = publish_cma(load_assets(root), api, state)
    cfg = api.agents[a["id"]]["config"]
    assert [s["skill_id"] for s in cfg["skills"]] == [state.data["skills"]["report-writer"]]
    assert "# new rules" in cfg["system"] and "/workspace/skills/" in cfg["system"]


def test_state_and_logs_have_no_secrets(tmp_path, capsys, monkeypatch):
    secret = "sk-ant-SECRETVALUE-123"
    monkeypatch.setenv("CLAUDE_API_KEY", secret)
    root = make_assets(tmp_path / "a")
    state = State(tmp_path / "s.json")
    agent = publish_cma(load_assets(root), FakeAnthropic(), state)
    print(agent)
    text = (tmp_path / "s.json").read_text() + capsys.readouterr().out
    assert secret not in text


def test_bash_always_ask_only_for_bash(tmp_path):
    api, state = FakeAnthropic(), State(tmp_path / "s.json")
    a = publish_cma(load_assets(make_assets(tmp_path / "a")), api, state, bash_always_ask=True)
    tools = api.agents[a["id"]]["config"]["tools"][0]
    assert tools["default_config"]["permission_policy"] == {"type": "always_allow"}
    asks = [c for c in tools["configs"] if c.get("permission_policy")]
    assert asks == [{"name": "bash", "permission_policy": {"type": "always_ask"}}]


def test_mcp_servers_get_always_allow_mcp_toolset(tmp_path):
    root = make_assets(tmp_path / "a")
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "mcp_servers": [
                    {"type": "url", "name": "echo", "url": "https://echo.example/mcp"},
                    {"type": "url", "name": "other", "url": "https://other.example/mcp"},
                ]
            }
        )
    )
    api, state = FakeAnthropic(), State(tmp_path / "s.json")
    a = publish_cma(load_assets(root), api, state)
    cfg = api.agents[a["id"]]["config"]
    assert [s["name"] for s in cfg["mcp_servers"]] == ["echo", "other"]
    toolsets = [t for t in cfg["tools"] if t["type"] == "mcp_toolset"]
    assert [t["mcp_server_name"] for t in toolsets] == ["echo", "other"]
    assert all(
        t["default_config"]["permission_policy"] == {"type": "always_allow"} for t in toolsets
    )


def test_no_mcp_servers_leaves_tools_unchanged(tmp_path):
    root = make_assets(tmp_path / "a")
    (root / "mcp.json").write_text(json.dumps({"mcp_servers": []}))
    api, state = FakeAnthropic(), State(tmp_path / "s.json")
    a = publish_cma(load_assets(root), api, state)
    cfg = api.agents[a["id"]]["config"]
    assert "mcp_servers" not in cfg
    assert [t["type"] for t in cfg["tools"]] == ["agent_toolset_20260401"]
