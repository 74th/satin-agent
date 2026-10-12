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


async def test_assets_mismatch_exits_without_worker(tmp_path, capsys):
    store = FakeObjectStore()
    publish_gcs(load_assets(make_assets(tmp_path / "src")), store)
    store.data["CLAUDE.md"] = b"tampered"
    called = []

    async def handler(*a):
        called.append(1)

    env = base_env(tmp_path, "cma", SATIN_ASSETS_URL="gs://b/p/")
    rc = await run(env, store_factory=lambda url: store, cma_handler=handler)
    assert rc == 3 and not called
    assert "CLAUDE.md" in capsys.readouterr().err


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
