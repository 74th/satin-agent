from pathlib import Path

from satin_agent.runtime.modes import CcConfig, build_cc_command
from satin_agent.runtime.timeouts import apply_cma_timeouts, claude_code_env, read_timeouts
from satin_agent.sandbox.config import SandboxConfig
from satin_agent.sandbox.manager import RunRequest, SandboxManager
from tests.fakes import FakeClock, FakeKube


def test_defaults_are_one_hour_and_two_hours():
    assert read_timeouts({}) == (3600.0, 7200.0)
    assert read_timeouts({"SATIN_BASH_DEFAULT_TIMEOUT": "60", "SATIN_BASH_MAX_TIMEOUT": "30"}) == (
        60.0,
        60.0,
    )  # 上限は既定より小さくならない


def test_cma_overrides_sdk_constants_and_keeps_outer_limit_above_bash(monkeypatch):
    from anthropic.lib.tools import _beta_session_runner, agent_toolset

    monkeypatch.setattr(agent_toolset, "BASH_DEFAULT_TIMEOUT", 120.0)
    monkeypatch.setattr(_beta_session_runner, "TOOL_TIMEOUT", 150.0)
    default, maximum = apply_cma_timeouts(
        {"SATIN_BASH_DEFAULT_TIMEOUT": "1800", "SATIN_BASH_MAX_TIMEOUT": "3600"}
    )
    assert (default, maximum) == (1800.0, 3600.0)
    assert agent_toolset.BASH_DEFAULT_TIMEOUT == 1800.0
    # SDKの規約: 外側の上限は、bashの上限より必ず長い
    assert _beta_session_runner.TOOL_TIMEOUT == 3630.0 > agent_toolset.BASH_DEFAULT_TIMEOUT


def test_claude_code_env_respects_existing_values():
    env = claude_code_env({"SATIN_BASH_DEFAULT_TIMEOUT": "600", "SATIN_BASH_MAX_TIMEOUT": "1200"})
    assert env == {"BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "1200000"}
    assert claude_code_env({"BASH_DEFAULT_TIMEOUT_MS": "5000"}).keys() == {"BASH_MAX_TIMEOUT_MS"}
    cmd, extra = build_cc_command(CcConfig(Path("/run/satin/work_order"), extra_env=env))
    assert (
        extra["BASH_DEFAULT_TIMEOUT_MS"] == "600000"
        and "SELF_HOSTED_RUNNER_HOST_CONFIG_DIR" in extra
    )
    assert "600000" not in " ".join(cmd)


async def test_sandbox_env_carries_timeouts_from_config():
    kube, clock = FakeKube(), FakeClock()
    cfg = SandboxConfig(bash_default_timeout_seconds=1800.0, bash_max_timeout_seconds=5400.0)
    mgr = SandboxManager(kube, clock, cfg)
    res = await mgr.ensure_started(RunRequest("S1", "cma", {"work_secret": "x"}))
    sb = await kube.get("sandbox", res.name)
    env = {e["name"]: e["value"] for e in sb["spec"]["podTemplate"]["spec"]["containers"][0]["env"]}
    assert env["SATIN_BASH_DEFAULT_TIMEOUT"] == "1800" and env["SATIN_BASH_MAX_TIMEOUT"] == "5400"
