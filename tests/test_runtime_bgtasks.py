import asyncio
import json
import os
from pathlib import Path

import pytest
from anthropic.lib.tools.agent_toolset import AgentToolContext, resolve_path

from satin_agent.runtime import bgtasks
from satin_agent.runtime.bgtasks import (
    BACKGROUND_RE,
    BgConfig,
    TaskError,
    TaskManager,
)


def task_id(out: str) -> str:
    m = BACKGROUND_RE.search(out)
    assert m, out
    return m.group(1)


def make(tmp_path: Path, **cfg) -> TaskManager:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    ctx = AgentToolContext(workdir=str(ws))
    return TaskManager(
        root=ws / ".satin" / "tasks",
        resolve_cwd=lambda p: resolve_path(ctx, p if p != "/workspace" else str(ws)),
        config=BgConfig(**cfg),
    )


def pid_alive_not_zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_finishes_within_wait(tmp_path):
    m = make(tmp_path)
    assert (await m.run_tool("echo hello", None, 5)).strip() == "hello"
    assert m.running() == []


async def test_nonzero_exit_is_error_result(tmp_path):
    m = make(tmp_path)
    with pytest.raises(TaskError, match="終了コード 3"):
        await m.run_tool("echo oops; exit 3", None, 5)


async def test_goes_to_background_and_wait(tmp_path):
    m = make(tmp_path)
    out = await m.run_tool("sleep 2; echo done", None, 1)
    tid = task_id(out)
    assert m.get(tid).state == "running" and "output.log" in out
    text = await m.wait_tool(tid, 10)
    assert "終了" in text and "終了コード 0" in text and "done" in text


async def test_stop_kills_child_processes(tmp_path):
    m = make(tmp_path)
    out = await m.run_tool("sh -c 'sleep 600 & echo $! > pid; sleep 600'", None, 1)
    tid = task_id(out)
    child = int((tmp_path / "ws" / "pid").read_text())
    leader = m.get(tid).pid
    assert leader
    assert pid_alive(child)
    text = await m.stop_tool(tid)
    assert "停止" in text and m.get(tid).state == "stopped"
    await asyncio.sleep(0.2)
    assert not pid_alive(child) and not pid_alive(leader)


async def test_max_seconds(tmp_path):
    m = make(tmp_path, max_seconds=1)
    out = await m.run_tool("sleep 120", None, 1)
    tid = task_id(out)
    await m.wait(tid, 10)
    assert m.get(tid).state == "timeout"
    assert "上限超過" in await m.status_tool(tid)


async def test_max_tasks(tmp_path):
    m = make(tmp_path, max_tasks=1)
    out = await m.run_tool("sleep 30", None, 1)
    with pytest.raises(TaskError, match="1件まで"):
        await m.run_tool("echo x", None, 1)
    await m.stop(task_id(out))
    assert (await m.run_tool("echo x", None, 5)).strip() == "x"


async def test_cwd_outside_workspace_is_rejected(tmp_path):
    m = make(tmp_path)
    with pytest.raises(TaskError, match="outside"):
        await m.run_tool("touch /tmp/never-created-by-satin-test", "/etc", 5)
    assert not list(m.root.glob("t-*"))


async def test_unknown_task_is_error(tmp_path):
    m = make(tmp_path)
    with pytest.raises(TaskError, match="t-9"):
        await m.status_tool("t-9")


async def test_output_is_truncated_but_kept_whole(tmp_path):
    m = make(tmp_path)
    out = await m.run_tool("head -c 1048576 /dev/zero | tr '\\0' 'x'", None, 20)
    assert "末尾だけ" in out and len(out) < 110 * 1024
    log = next(m.root.glob("t-*/output.log"))
    assert log.stat().st_size == 1048576


async def test_lost_after_restart_and_record_survives(tmp_path):
    m = make(tmp_path)
    out = await m.run_tool("sleep 600", None, 1)
    tid = task_id(out)
    pid = m.get(tid).pid
    assert pid
    meta = json.loads((m.root / tid / "meta.json").read_text())
    assert meta["state"] == "running"
    # 前のSandboxが消えた状態を作る: 実行中のまま記録だけが残る
    os.killpg(pid, 9)
    m2 = make(tmp_path)
    assert m2.get(tid).state == "lost"
    assert "消失" in await m2.status_tool(tid)
    nxt = await m2.run_tool("sleep 30", None, 1)
    assert task_id(nxt).startswith("t-2-")  # 連番を引き継ぐ
    await m2.shutdown()


async def test_shutdown_stops_running_tasks_as_lost(tmp_path):
    m = make(tmp_path)
    out = await m.run_tool("sleep 600", None, 1)
    tid = task_id(out)
    pid = m.get(tid).pid
    assert pid
    await m.shutdown()
    assert m.get(tid).state == "lost"
    await asyncio.sleep(0.2)
    assert not pid_alive(pid)
    assert json.loads((m.root / tid / "meta.json").read_text())["state"] == "lost"


async def test_hold_and_release_follow_task_lifetime(tmp_path):
    m = make(tmp_path)
    calls: list[str] = []
    m.hold = lambda: calls.append("hold")
    m.release = lambda: calls.append("release")
    out = await m.run_tool("sleep 3", None, 1)
    assert calls == ["hold"]
    await m.wait(task_id(out), 10)
    assert calls == ["hold", "release"]


async def test_notices_once_and_status_counts_as_told(tmp_path):
    m = make(tmp_path)
    a = task_id(await m.run_tool("sleep 3", None, 1))
    b = task_id(await m.run_tool("sleep 3", None, 1))
    assert m.take_notices() == []
    await m.wait(a, 10)
    await m.wait(b, 10)
    await m.status_tool(b)  # statusで読んだものは、伝えたものとして扱う
    notices = m.take_notices()
    assert len(notices) == 1 and a in notices[0] and "終了コード 0" in notices[0]
    assert m.take_notices() == []
    assert m.report() == []


async def test_report_lists_unnotified_without_secrets(tmp_path):
    m = make(tmp_path)
    a = task_id(await m.run_tool("sleep 2; exit 4", None, 1))
    await m.wait(a, 10)
    rep = m.report()
    assert rep == [{"id": a, "command": "sleep 2; exit 4", "state": "exited", "exit_code": 4}]


async def test_env_has_no_anthropic_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_ENVIRONMENT_KEY", "env-secret")
    m = make(tmp_path)
    out = await m.run_tool("env", None, 5)
    assert "ANTHROPIC_" not in out and "sk-secret" not in out and "env-secret" not in out


async def test_children_left_by_an_exited_shell_are_stopped_and_noted(tmp_path):
    m = make(tmp_path)
    out = await m.run_tool("sleep 120 & echo $! > child.pid; echo launched", None, 10)
    child = int((tmp_path / "ws" / "child.pid").read_text())
    await asyncio.sleep(0.2)
    assert "launched" in out and "残っていた子プロセスを停止" in out
    assert not pid_alive_not_zombie(child)
    [t] = m._tasks.values()
    assert t.state == "exited" and t.leftover
    assert "残っていた子プロセスを停止" in await m.status_tool(t.id)


async def test_leftover_children_do_not_hold_the_task_slot(tmp_path):
    m = make(tmp_path, max_tasks=1)
    await m.run_tool("sleep 120 & echo ok", None, 10)
    assert (await m.run_tool("echo next", None, 5)).strip() == "next"
    assert m.running() == []


async def test_shutdown_stops_tasks_in_parallel(tmp_path):
    m = make(tmp_path)
    # SIGTERMを無視するタスクが複数あっても、停止の時間は足し算にならない
    for _ in range(3):
        await m.run_tool("trap '' TERM; sleep 600", None, 1)
    t0 = asyncio.get_running_loop().time()
    await m.shutdown()
    took = asyncio.get_running_loop().time() - t0
    assert m.running() == [] and took < bgtasks.KILL_GRACE * 1.5


async def test_status_without_id_is_bounded(tmp_path):
    # PVCに過去のタスクが積もっても、task_status（IDなし）の結果は青天井にならない
    m = make(tmp_path)
    ids = []
    for _ in range(bgtasks.STATUS_ALL_MAX_TASKS + 5):
        out = await m.run_tool("yes 0123456789 | head -c 4000", None, 10)
        ids.append(task_id(out) if BACKGROUND_RE.search(out) else None)
    out = await m.status_tool(None)
    assert len(m._tasks) == bgtasks.STATUS_ALL_MAX_TASKS + 5
    assert "ほかに" in out and "件のタスクがあります" in out
    assert len(out) < bgtasks.STATUS_ALL_BUDGET * 2


async def test_status_without_id_shows_running_first(tmp_path):
    m = make(tmp_path)
    await m.run_tool("echo done", None, 10)
    running = task_id(await m.run_tool("sleep 30", None, 1))
    assert (await m.status_tool(None)).startswith(running)


def test_bg_config_falls_back_on_bad_values(capsys):
    cfg = BgConfig.from_env(
        {
            "SATIN_BG_MAX_TASKS": "abc",  # 数値として読めない
            "SATIN_BG_MAX_SECONDS": "-1",  # 範囲外
            "SATIN_BG_DEFAULT_WAIT": "0",  # 下限より小さい（全てのrunが即バックグラウンドへ回る）
        }
    )
    assert cfg.max_tasks == bgtasks.DEFAULT_MAX_TASKS
    assert cfg.max_seconds == 1.0
    assert cfg.default_wait == bgtasks.WAIT_MIN
    assert "SATIN_BG_MAX_TASKS" in capsys.readouterr().err


def test_bg_config_reads_valid_values():
    cfg = BgConfig.from_env(
        {"SATIN_BG_MAX_TASKS": "2", "SATIN_BG_MAX_SECONDS": "60", "SATIN_BG_DEFAULT_WAIT": "120"}
    )
    assert cfg.max_tasks == 2 and cfg.max_seconds == 60.0
    assert cfg.default_wait == bgtasks.WAIT_MAX  # 上限で頭打ち（既存の挙動）
