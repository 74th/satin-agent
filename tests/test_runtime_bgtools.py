import inspect

import pytest
from anthropic.lib.tools import ToolError
from anthropic.lib.tools.agent_toolset import AgentToolContext

from satin_agent.runtime import bgtools, idle
from satin_agent.runtime.bgtasks import BACKGROUND_RE, TOOL_DEFS


def task_id(out: str) -> str:
    m = BACKGROUND_RE.search(out)
    assert m, out
    return m.group(1)


def mgr():
    m = bgtools.manager()
    assert m
    return m


@pytest.fixture(autouse=True)
def clock():
    """SDKの実物の`_IdleClock`を、登録つきで使う。"""
    idle.reset()
    idle.install()
    from anthropic.lib.tools._beta_session_runner import _IdleClock

    c = _IdleClock()
    yield c
    idle.reset()
    bgtools.reset()


def tools_by_name(tmp_path, env=None):
    ctx = AgentToolContext(workdir=str(tmp_path))
    return {t.name: t for t in bgtools.worker_tools(ctx, env or {})}


async def test_tool_inputs_match_definitions(tmp_path):
    tools = tools_by_name(tmp_path)
    for d in TOOL_DEFS:
        t = tools[d["name"]]
        assert t.to_dict()["input_schema"] == d["input_schema"]
        assert t.description == d["description"]
        params = set(inspect.signature(t.func).parameters)
        assert set(d["input_schema"]["properties"]) == params
        assert set(d["input_schema"].get("required", [])) <= params
    assert {"bash", "read", "write", "edit", "glob", "grep"} <= tools.keys()


async def test_run_does_not_leak_anthropic_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_ENVIRONMENT_KEY", "env-secret")
    out = await tools_by_name(tmp_path)["run"].call({"command": "env"})
    assert not any(line.startswith("ANTHROPIC_") for line in out.splitlines())
    assert "sk-secret" not in out and "env-secret" not in out


async def test_nonzero_exit_is_error_result(tmp_path):
    with pytest.raises(ToolError, match="終了コード 2"):
        await tools_by_name(tmp_path)["run"].call({"command": "echo bad; exit 2"})


async def test_cwd_outside_is_error_result(tmp_path):
    with pytest.raises(ToolError, match="outside"):
        await tools_by_name(tmp_path)["run"].call({"command": "pwd", "cwd": "/etc"})


async def test_notice_is_appended_once_to_next_result(tmp_path):
    tools = tools_by_name(tmp_path)
    out = await tools["run"].call({"command": "sleep 2; echo hi", "wait_seconds": 1})
    tid = task_id(out)
    assert idle.hold  # holdされている
    await mgr().wait(tid, 10)
    first = await tools["glob"].call({"pattern": "*"})
    assert "[satin]" in first and tid in first and "終了コード 0" in first
    second = await tools["glob"].call({"pattern": "*"})
    assert "[satin]" not in second


async def test_notice_is_appended_to_error_results_too(tmp_path):
    tools = tools_by_name(tmp_path)
    out = await tools["run"].call({"command": "sleep 2", "wait_seconds": 1})
    tid = task_id(out)
    await mgr().wait(tid, 10)
    with pytest.raises(ToolError) as e:
        await tools["read"].call({"file_path": str(tmp_path / "nope.txt")})
    assert "[satin]" in str(e.value) and tid in str(e.value)
    assert mgr().take_notices() == []


async def test_status_and_wait_count_as_told(tmp_path):
    tools = tools_by_name(tmp_path)
    out = await tools["run"].call({"command": "sleep 2", "wait_seconds": 1})
    tid = task_id(out)
    waited = await tools["task_wait"].call({"task_id": tid, "seconds": 10})
    assert "終了" in waited and "[satin]" not in waited
    assert "[satin]" not in await tools["glob"].call({"pattern": "*"})


async def test_unknown_task_is_error_result(tmp_path):
    with pytest.raises(ToolError, match="t-9"):
        await tools_by_name(tmp_path)["task_stop"].call({"task_id": "t-9"})
