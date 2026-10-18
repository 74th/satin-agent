import pytest

from satin_agent.runtime import idle


@pytest.fixture(autouse=True)
def fresh():
    idle.reset()
    yield
    idle.reset()


def test_hold_stops_the_clock_until_last_release():
    idle.install()
    idle.install()  # 何度呼んでも二重に包まない
    from anthropic.lib.tools._beta_session_runner import _IdleClock

    clock = _IdleClock()
    idle.hold()
    idle.hold()
    clock.arm()  # turnが終わった
    assert clock.turn_ended_at is None  # hold中は進まない
    idle.release()
    assert clock.turn_ended_at is None
    idle.release()
    assert clock.turn_ended_at is not None  # 最後のreleaseで進み始める


def test_hold_fails_without_a_registered_clock():
    idle.install()
    with pytest.raises(idle.IdleClockMissing):
        idle.hold()


def test_install_fails_when_sdk_internals_change(monkeypatch):
    from anthropic.lib.tools import _beta_session_runner as r

    monkeypatch.delattr(r._IdleClock, "hold")
    with pytest.raises(idle.IdleClockMissing):
        idle.install()


async def test_task_start_fails_without_clock(tmp_path):
    from anthropic.lib.tools.agent_toolset import AgentToolContext

    from satin_agent.runtime import bgtools

    idle.install()  # 時計は作られていない
    tools = {t.name: t for t in bgtools.worker_tools(AgentToolContext(workdir=str(tmp_path)), {})}
    from anthropic.lib.tools import ToolError

    with pytest.raises(ToolError, match="時計"):
        await tools["run"].call({"command": "echo x"})
    assert not [p for p in (tmp_path / ".satin" / "tasks").glob("t-*/meta.json")]
    bgtools.reset()
