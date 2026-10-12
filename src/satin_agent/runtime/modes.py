"""モードごとのワーカーの起動。(A) cma / (B) cc。"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("cma", "cc")


class RunInfoError(Exception):
    pass


@dataclass(frozen=True)
class CmaWork:
    work_id: str
    session_id: str
    environment_id: str
    work_secret: str


def read_cma_work(run_dir: Path) -> CmaWork:
    """Secretのマウント先（1キー1ファイル）から、処理するwork itemを読む。"""
    vals = {}
    for name in ("work_id", "session_id", "environment_id", "work_secret"):
        p = run_dir / name
        if not p.is_file():
            raise RunInfoError(f"{p} がありません")
        vals[name] = p.read_text().strip()
    return CmaWork(**vals)


# handle_item(work, workdir, max_idle) を1回実行する関数。テストでは偽物に差し替える。
CmaHandler = Callable[[CmaWork, str, float], Awaitable[None]]


async def sdk_handle_item(work: CmaWork, workdir: str, max_idle: float) -> None:
    """`EnvironmentWorker.handle_item()`。秘密は引数で渡し、環境変数には置かない。"""
    from anthropic import AsyncAnthropic
    from anthropic.lib.environments import EnvironmentWorker

    client = AsyncAnthropic(auth_token=work.work_secret)
    worker = EnvironmentWorker(client, workdir=workdir, max_idle=max_idle)
    await worker.handle_item(
        work_id=work.work_id,
        environment_id=work.environment_id,
        session_id=work.session_id,
        work_secret=work.work_secret,
    )


async def run_cma(
    work: CmaWork,
    *,
    workdir: str,
    max_idle: float = 10.0,
    handler: CmaHandler = sdk_handle_item,
    grace: float = 45.0,
    hard_exit: Callable[[int], None] = os._exit,
) -> int:
    """work itemを処理する。

    SIGTERM/SIGINT（Podの削除など）を受けても、ワーカーを**キャンセルしない**。SDKの
    `handle_item`は、キャンセルを含むどんな終わり方でも、最後にwork itemを`force-stop`する。
    停止されたwork itemは再配信されず、ツール結果待ちのセッションが放置される（実クラスタで確認）。

    そのため、猶予（`grace`秒）の間は、実行中のツールが終わって正常に終了するのを待ち、
    間に合わなければ、後始末なしでプロセスを即時終了する。work itemは停止されないので、
    リースが切れて再配信される（または、オーケストレータの再実行が引き継ぐ）。
    猶予は、Podの`terminationGracePeriodSeconds`（60秒）より短くする。
    """
    task = asyncio.ensure_future(handler(work, workdir, max_idle))
    loop = asyncio.get_running_loop()
    terminated = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, terminated.set)
    try:
        waiter = asyncio.ensure_future(terminated.wait())
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if task.done():
            await task
            return 0
        done, _ = await asyncio.wait({task}, timeout=grace)
        if done:
            await task
            return 0
        print(
            f"satin-sandbox: 猶予{grace:.0f}秒で終わらないため、work itemを停止せずに終了します",
            file=sys.stderr,
            flush=True,
        )
        hard_exit(143)
        return 143
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


@dataclass(frozen=True)
class CcConfig:
    work_order_file: Path
    base_dir: str = "/workspace/runner"
    host_config_dir: str = "/home/satin/.claude-host"
    release_idle_min: int = 10
    drain_wait_sec: int = 60
    claude_bin: str = "claude"
    extra_env: dict[str, str] = field(default_factory=dict)  # runnerに足す環境変数


def build_cc_command(cfg: CcConfig) -> tuple[list[str], dict[str, str]]:
    """(B) runnerの引数と追加の環境変数。work orderは環境変数ではなくファイルで渡す。"""
    cmd = [
        cfg.claude_bin,
        "self-hosted-runner",
        "--environment-secret-file",
        str(cfg.work_order_file),
        "--capacity",
        "1",
        "--drain-grace-sec",
        "0",
        "--release-idle-session-min",
        str(cfg.release_idle_min),
        "--drain-wait-sec",
        str(cfg.drain_wait_sec),
        "--base-dir",
        cfg.base_dir,
        "--health-port",
        "0",
    ]
    return cmd, {"SELF_HOSTED_RUNNER_HOST_CONFIG_DIR": cfg.host_config_dir, **cfg.extra_env}


async def run_cc(cfg: CcConfig, *, env: dict[str, str] | None = None) -> int:
    """runnerを起動し、終了コードを中継する。SIGTERMはrunnerへ転送して終了を待つ。"""
    cmd, extra = build_cc_command(cfg)
    child_env = dict(env if env is not None else os.environ)
    child_env.update(extra)
    proc = await asyncio.create_subprocess_exec(*cmd, env=child_env)
    loop = asyncio.get_running_loop()

    def forward(sig: int) -> None:
        if proc.returncode is None:
            proc.send_signal(sig)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, forward, sig)
    try:
        return await proc.wait()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
