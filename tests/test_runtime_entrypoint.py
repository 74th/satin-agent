import asyncio
import os
import signal
import sys

import pytest

from satin_agent.assets.gcs import publish_gcs
from satin_agent.assets.load import load_assets
from satin_agent.runtime.entrypoint import main, run
from satin_agent.runtime.modes import CcConfig, CmaWork, build_cc_command, run_cc
from tests.fakes import FakeObjectStore
from tests.test_assets import make_assets


def run_dir(tmp_path, mode="cma"):
    d = tmp_path / "run"
    d.mkdir()
    if mode == "cma":
        for k, v in dict(
            work_id="w1", session_id="S1", environment_id="env1", work_secret="s3cr3t"
        ).items():
            (d / k).write_text(v)
    else:
        (d / "work_order").write_text("JWT")
    return d


def base_env(tmp_path, mode, **extra):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return {
        "SATIN_MODE": mode,
        "SATIN_SESSION_ID": "S1",
        "SATIN_WORKSPACE": str(ws),
        "SATIN_RUN_DIR": str(run_dir(tmp_path, mode)),
        **extra,
    }


async def test_unknown_mode_exits_nonzero(tmp_path, capsys):
    assert await run({"SATIN_MODE": "foo"}) == 2
    assert "未知のモード" in capsys.readouterr().err
    assert await run({}) == 2


async def test_cma_runs_handler_with_work_after_startup(tmp_path):
    seen = []

    async def handler(work: CmaWork, workdir, max_idle):
        seen.append((work, workdir, max_idle, os.environ.get("ANTHROPIC_WORK_SECRET")))

    env = base_env(tmp_path, "cma")
    rc = await run(env, cma_handler=handler)
    assert rc == 0
    work, workdir, max_idle, leaked = seen[0]
    assert work.work_secret == "s3cr3t" and work.session_id == "S1"
    assert workdir == env["SATIN_WORKSPACE"] and leaked is None


async def test_broken_assets_bundle_exits_without_worker(tmp_path, capsys):
    store = FakeObjectStore()
    publish_gcs(load_assets(make_assets(tmp_path / "src")), store)
    store.data["assets.zip"] = store.data["assets.zip"][:-30]
    called = []

    async def handler(*a):
        called.append(1)

    env = base_env(tmp_path, "cma", SATIN_ASSETS_URL="gs://b/p/")
    rc = await run(env, store_factory=lambda url: store, cma_handler=handler)
    assert rc == 3 and not called
    assert "assets.zip" in capsys.readouterr().err


async def test_startup_failure_blocks_worker_unless_configured(tmp_path):
    store = FakeObjectStore()
    src = make_assets(tmp_path / "src")
    (src / "startup.sh").write_text("exit 3\n")
    publish_gcs(load_assets(src), store)
    called = []

    async def handler(*a):
        called.append(1)

    env = base_env(tmp_path, "cma", SATIN_ASSETS_URL="gs://b/p/")
    assert await run(env, store_factory=lambda u: store, cma_handler=handler) == 4
    assert not called
    env["SATIN_STARTUP_CONTINUE_ON_FAILURE"] = "1"
    assert await run(env, store_factory=lambda u: store, cma_handler=handler) == 0
    assert called


async def test_cma_sigterm_lets_the_worker_finish_within_grace_without_cancelling_it(tmp_path):
    """SIGTERMでもワーカーをキャンセルしない（すると、work itemが停止され再配信されない）。"""
    started, cancelled = asyncio.Event(), []

    async def handler(work, workdir, max_idle):
        started.set()
        try:
            await asyncio.sleep(0.3)  # 実行中のツールが猶予内に終わる
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    from satin_agent.runtime.modes import run_cma

    task = asyncio.create_task(
        run_cma(CmaWork("w", "S1", "env", "s"), workdir=str(tmp_path), handler=handler, grace=5)
    )
    await started.wait()
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 5) == 0
    assert cancelled == []


async def test_cma_sigterm_exits_hard_without_cleanup_when_grace_is_exceeded(tmp_path):
    """猶予内に終わらないときは、後始末なし（work itemを停止しない）で即時終了する。"""
    from satin_agent.runtime.modes import run_cma

    started, release, exits = asyncio.Event(), asyncio.Event(), []

    async def handler(work, workdir, max_idle):
        started.set()
        await release.wait()

    task = asyncio.create_task(
        run_cma(
            CmaWork("w", "S1", "env", "s"),
            workdir=str(tmp_path),
            handler=handler,
            grace=0.2,
            hard_exit=exits.append,
        )
    )
    await started.wait()
    os.kill(os.getpid(), signal.SIGTERM)
    try:
        assert await asyncio.wait_for(task, 5) == 143
        assert exits == [143]
    finally:
        release.set()


def test_cc_command_uses_file_not_env():
    cmd, extra = build_cc_command(
        CcConfig(work_order_file=__import__("pathlib").Path("/run/satin/work_order"))
    )
    assert cmd[cmd.index("--environment-secret-file") + 1] == "/run/satin/work_order"
    assert "--capacity" in cmd and cmd[cmd.index("--drain-grace-sec") + 1] == "0"
    assert extra == {"SELF_HOSTED_RUNNER_HOST_CONFIG_DIR": "/home/satin/.claude-host"}
    assert "JWT" not in " ".join(cmd)


async def test_cc_relays_exit_code_and_forwards_sigterm(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(
        f"#!{sys.executable}\nimport signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(7))\n"
        "print('ready', flush=True)\ntime.sleep(30)\n"
    )
    fake.chmod(0o755)
    cfg = CcConfig(work_order_file=tmp_path / "wo", claude_bin=str(fake))
    task = asyncio.create_task(run_cc(cfg, env={"PATH": os.environ["PATH"]}))
    await asyncio.sleep(0.8)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 5) == 7


def test_main_mode_arg_overrides_env(monkeypatch, capsys):
    monkeypatch.setenv("SATIN_MODE", "cma")
    assert main(["foo"]) == 2
    assert "'foo'" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["cma", "cc"])
async def test_missing_run_info_is_error(tmp_path, mode):
    env = {"SATIN_MODE": mode, "SATIN_RUN_DIR": str(tmp_path / "none")}
    assert await run(env) == 2


async def test_cc_places_skills_and_claude_md_from_the_bundle_and_runs_startup(
    tmp_path, monkeypatch
):
    src = make_assets(tmp_path / "src")
    (src / "startup.sh").write_text('#!/bin/sh\necho "$SATIN_MODE" > "$PWD/startup-ran"\n')
    store = FakeObjectStore()
    publish_gcs(load_assets(src), store)
    seen = []

    async def fake_run_cc(cfg, **kw):
        seen.append(cfg)
        return 0

    monkeypatch.setattr("satin_agent.runtime.entrypoint.run_cc", fake_run_cc)
    host = tmp_path / "host"
    env = base_env(tmp_path, "cc", SATIN_ASSETS_URL="gs://b/p/", SATIN_HOST_CONFIG_DIR=str(host))
    assert await run(env, store_factory=lambda u: store) == 0
    assert (host / "skills/report-writer/SKILL.md").is_file()
    assert (host / "CLAUDE.md").read_text() == "# rules\n"
    assert (tmp_path / "ws" / "startup-ran").read_text().strip() == "cc"
    assert seen and seen[0].host_config_dir == str(host)


@pytest.fixture
def bg(tmp_path):
    from anthropic.lib.tools.agent_toolset import AgentToolContext

    from satin_agent.runtime import bgtools

    ctx = AgentToolContext(workdir=str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    mgr = bgtools.create_manager(ctx, {}, hold=lambda: None, release=lambda: None)
    yield mgr
    bgtools.reset()


async def test_cma_reports_unnotified_finished_tasks_in_termination_message(tmp_path, bg):
    import json

    from satin_agent.runtime.modes import run_cma

    log = tmp_path / "termination-log"

    async def handler(work, workdir, max_idle):
        t = await bg.start("echo hi; exit 5")
        await bg.wait(t.id, 5)

    assert (
        await run_cma(
            CmaWork("w", "S1", "env", "s"),
            workdir=str(tmp_path),
            handler=handler,
            termination_log=log,
        )
        == 0
    )
    [item] = json.loads(log.read_text())["finished_tasks"]
    assert item["exit_code"] == 5 and item["state"] == "exited" and item["id"].startswith("t-1-")


async def test_cma_writes_nothing_when_no_task_is_pending(tmp_path, bg):
    from satin_agent.runtime.modes import run_cma

    log = tmp_path / "termination-log"

    async def handler(work, workdir, max_idle):
        t = await bg.start("true")
        await bg.wait(t.id, 5)
        bg.get(t.id).notified = True  # task_statusで伝えた

    await run_cma(
        CmaWork("w", "S1", "env", "s"), workdir=str(tmp_path), handler=handler, termination_log=log
    )
    assert not log.exists()


async def test_cma_termination_message_falls_back_to_a_count_over_4kb(tmp_path, bg):
    import json

    from satin_agent.runtime.modes import run_cma

    log = tmp_path / "termination-log"

    async def handler(work, workdir, max_idle):
        for _ in range(3):
            t = await bg.start("echo " + "x" * 70 + "; sleep 0")
            await bg.wait(t.id, 5)
        for t in list(bg._tasks.values()):  # 長いコマンドが多数ある状態にする
            t.command = "c" * 80
        bg._tasks.update(
            {
                f"t-9{i}-aaaa": type(t)(f"t-9{i}-aaaa", "c" * 80, "/", 0.0, "exited", exit_code=0)
                for i in range(60)
            }
        )

    await run_cma(
        CmaWork("w", "S1", "env", "s"), workdir=str(tmp_path), handler=handler, termination_log=log
    )
    assert json.loads(log.read_text()) == {"finished_count": 63}


async def test_cma_sigterm_stops_running_tasks_and_records_them_lost(tmp_path, bg):
    import json

    from satin_agent.runtime.modes import run_cma

    log = tmp_path / "termination-log"
    started, stop = asyncio.Event(), asyncio.Event()

    async def handler(work, workdir, max_idle):
        t = await bg.start("sleep 600")
        started.set()
        await bg.wait(t.id, 30)  # タスクが止まると、ワーカーはアイドルで終われる
        stop.set()

    task = asyncio.create_task(
        run_cma(
            CmaWork("w", "S1", "env", "s"),
            workdir=str(tmp_path),
            handler=handler,
            grace=10,
            termination_log=log,
        )
    )
    await started.wait()
    [t] = bg.running()
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 15) == 0
    assert bg.get(t.id).state == "lost"
    assert json.loads(log.read_text())["finished_tasks"][0]["state"] == "lost"


async def test_cma_sigterm_task_shutdown_counts_against_the_same_grace(tmp_path, bg):
    """停止処理が長くても、猶予（grace）を超えて待たない（Podの猶予60秒を超えないため）。"""
    from satin_agent.runtime.modes import run_cma

    release, exits = asyncio.Event(), []

    async def slow_shutdown():
        await asyncio.sleep(5)

    bg.shutdown = slow_shutdown

    async def handler(work, workdir, max_idle):
        await release.wait()

    task = asyncio.create_task(
        run_cma(
            CmaWork("w", "S1", "env", "s"),
            workdir=str(tmp_path),
            handler=handler,
            grace=0.3,
            hard_exit=exits.append,
            termination_log=tmp_path / "log",
        )
    )
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), signal.SIGTERM)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        assert await asyncio.wait_for(task, 3) == 143
        assert loop.time() - t0 < 1.0 and exits == [143]
    finally:
        release.set()
