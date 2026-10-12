import asyncio
import io

import pytest

from satin_agent.cc.reconcile import CcReconciler
from satin_agent.cc.spawn import FATAL, OK, RETRY, classify, spawn
from satin_agent.ports import ConflictError
from satin_agent.sandbox.config import SandboxConfig
from satin_agent.sandbox.lifecycle_log import LifecycleLog
from satin_agent.sandbox.manager import RunRequest, SandboxManager
from satin_agent.sandbox.naming import sandbox_name, secret_name
from tests.fakes import FakeClock, FakeKube


def make(**kw):
    kube, clock = FakeKube(), FakeClock()
    mgr = SandboxManager(kube, clock, SandboxConfig(**kw), LifecycleLog(clock.now, io.StringIO()))
    return mgr, kube, clock


def hook_env(tmp_path, sid="session_X", email="me@example.com", jwt="JWT1"):
    wo = tmp_path / "wo"
    wo.write_text(jwt)
    return {
        "CLAUDE_RUNNER_SESSION_ID": sid,
        "CLAUDE_RUNNER_WORK_ORDER_FILE": str(wo),
        "CLAUDE_RUNNER_ACCOUNT_EMAIL": email,
    }


class ApiErr(Exception):
    def __init__(self, status):
        self.status = status


async def test_spawn_creates_sandbox_without_waiting_for_ready(tmp_path):
    mgr, kube, _ = make()
    rc = await asyncio.wait_for(spawn(hook_env(tmp_path), mgr), 1)  # Podは準備完了にならない
    assert rc == OK
    name = sandbox_name("session_X", 1)
    sb = await kube.get("sandbox", name)
    envs = {
        e["name"]: e["value"] for e in sb["spec"]["podTemplate"]["spec"]["containers"][0]["env"]
    }
    assert envs["SATIN_MODE"] == "cc" and envs["AGENT_USER_EMAIL"] == "me@example.com"
    assert (await kube.get("secret", secret_name("session_X")))["stringData"] == {
        "work_order": "JWT1"
    }
    assert "JWT1" not in str(sb)


async def test_rehint_while_running_is_noop(tmp_path):
    mgr, kube, _ = make()
    await spawn(hook_env(tmp_path, jwt="A"), mgr)
    assert await spawn(hook_env(tmp_path, jwt="B"), mgr) == OK
    assert (await kube.get("secret", secret_name("session_X")))["stringData"]["work_order"] == "A"
    assert len(await kube.list("sandbox")) == 1


async def test_resume_after_idle_uses_new_work_order(tmp_path):
    mgr, kube, _ = make()
    await spawn(hook_env(tmp_path, jwt="A"), mgr)
    name = sandbox_name("session_X", 1)
    await kube.pod_exit(name, 0)
    await CcReconciler(mgr, kube).reconcile_once()
    assert await spawn(hook_env(tmp_path, jwt="B"), mgr) == OK
    assert (await kube.get("secret", secret_name("session_X")))["stringData"]["work_order"] == "B"
    assert (await kube.get("sandbox", name))["spec"]["operatingMode"] == "Running"


async def test_transient_error_returns_retry_then_succeeds(tmp_path):
    mgr, kube, _ = make()
    real = kube.create
    calls = []

    async def flaky(kind, obj):
        if kind == "sandbox" and not calls:
            calls.append(1)
            raise ApiErr(503)
        return await real(kind, obj)

    kube.create = flaky  # type: ignore[method-assign]
    assert await spawn(hook_env(tmp_path), mgr) == RETRY
    assert await spawn(hook_env(tmp_path), mgr) == OK  # 後の再試行で作られる
    assert len(await kube.list("sandbox")) == 1


async def test_config_error_returns_fatal(tmp_path, capsys):
    mgr, kube, _ = make()

    async def forbidden(kind, obj):
        raise ApiErr(403)

    kube.create = forbidden  # type: ignore[method-assign]
    assert await spawn(hook_env(tmp_path), mgr) == FATAL
    assert await spawn({}, mgr) == FATAL
    assert (
        await spawn(
            {"CLAUDE_RUNNER_SESSION_ID": "s", "CLAUDE_RUNNER_WORK_ORDER_FILE": "/nope"}, mgr
        )
        == FATAL
    )
    assert "JWT" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc,code",
    [
        (ApiErr(429), RETRY),
        (ApiErr(500), RETRY),
        (ApiErr(404), FATAL),
        (ApiErr(422), FATAL),
        (TimeoutError(), RETRY),
        (ConnectionError(), RETRY),
        (ConflictError(), RETRY),
    ],
)
def test_classify(exc, code):
    assert classify(exc) == code


async def test_reconcile_stops_finished_sandbox_and_gcs_expired():
    mgr, kube, clock = make(retention_seconds=10)
    r = await mgr.ensure_started(RunRequest("S1", "cc", {"work_order": "x"}))
    await kube.pod_ready(r.name)
    await kube.pod_exit(r.name, 0)
    rec = CcReconciler(mgr, kube)
    assert await rec.reconcile_once() == [r.name]
    assert (await kube.get("sandbox", r.name))["spec"]["operatingMode"] == "Suspended"
    assert await rec.reconcile_once() == []
    clock.advance(11)
    assert await mgr.gc() == [r.name]
