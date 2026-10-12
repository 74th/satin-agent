import asyncio
import os

import pytest

from satin_agent.runtime.startup import StartupFailed, run_startup


def script(tmp_path, body):
    p = tmp_path / "startup.sh"
    p.write_text(body)
    return p


async def run(tmp_path, body, **kw):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return await run_startup(
        script(tmp_path, body),
        workspace=ws,
        mode="cma",
        session_id="S1",
        user_email="a@example.com",
        env={"PATH": os.environ["PATH"]},
        **kw,
    )


async def test_first_then_second_run_and_env(tmp_path):
    body = (
        'echo "$SATIN_MODE $SATIN_SESSION_ID $SATIN_FIRST_RUN $AGENT_USER_EMAIL $(pwd)"'
        " >> out.txt\n"
    )
    r1 = await run(tmp_path, body)
    r2 = await run(tmp_path, body)
    assert (r1.first_run, r2.first_run) == (True, False)
    lines = (tmp_path / "ws/out.txt").read_text().splitlines()
    assert lines[0] == f"cma S1 1 a@example.com {tmp_path / 'ws'}"
    assert lines[1].split()[2] == "0"


async def test_failure_blocks_worker_by_default(tmp_path):
    with pytest.raises(StartupFailed, match="3"):
        await run(tmp_path, "exit 3\n")


async def test_failure_can_continue(tmp_path):
    r = await run(tmp_path, "exit 3\n", continue_on_failure=True)
    assert r.returncode == 3


async def test_timeout_kills_process_group(tmp_path):
    body = "sleep 30 &\necho $! > child.pid\nwait\n"
    with pytest.raises(StartupFailed, match="タイムアウト"):
        await run(tmp_path, body, timeout=0.5)
    pid = int((tmp_path / "ws/child.pid").read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_cancel_stops_process_group(tmp_path):
    body = "sleep 30 &\necho $! > child.pid\nwait\n"
    task = asyncio.create_task(run(tmp_path, body))
    for _ in range(50):
        await asyncio.sleep(0.05)
        if (tmp_path / "ws/child.pid").exists():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int((tmp_path / "ws/child.pid").read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_no_script_is_noop(tmp_path):
    r = await run_startup(None, workspace=tmp_path, mode="cc", session_id="S")
    assert not r.ran and r.first_run
