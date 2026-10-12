"""`startup.sh`の実行。"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 300.0
MARKER = ".satin/startup-done"


class StartupFailed(Exception):
    pass


@dataclass(frozen=True)
class StartupResult:
    ran: bool
    first_run: bool
    returncode: int | None
    timed_out: bool = False


def is_first_run(workspace: Path) -> bool:
    return not (workspace / MARKER).exists()


def _mark_done(workspace: Path) -> None:
    m = workspace / MARKER
    m.parent.mkdir(parents=True, exist_ok=True)
    m.touch()


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


async def run_startup(
    script: Path | None,
    *,
    workspace: Path,
    mode: str,
    session_id: str,
    user_email: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    continue_on_failure: bool = False,
    env: dict[str, str] | None = None,
) -> StartupResult:
    """`startup.sh`を、ワークスペースをカレントディレクトリとして実行する。

    失敗・タイムアウト時は既定で`StartupFailed`（ワーカーを起動しない）。
    `continue_on_failure`なら結果だけ返す。SIGTERMを受けてキャンセルされた場合は
    プロセスグループを止めてからCancelledErrorを再送出する。
    """
    first = is_first_run(workspace)
    if script is None:
        return StartupResult(False, first, None)
    child_env = dict(env if env is not None else os.environ)
    child_env.update(
        SATIN_MODE=mode,
        SATIN_SESSION_ID=session_id,
        SATIN_FIRST_RUN="1" if first else "0",
        AGENT_USER_EMAIL=user_email,
    )
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        str(script),
        cwd=workspace,
        env=child_env,
        start_new_session=True,  # 独立したプロセスグループ
    )
    try:
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout)
            timed_out = False
        except TimeoutError:
            await _kill_group(proc)
            rc, timed_out = proc.returncode, True
    except asyncio.CancelledError:
        await _kill_group(proc)
        raise
    result = StartupResult(True, first, rc, timed_out)
    if timed_out or rc != 0:
        reason = f"タイムアウト({timeout}s)" if timed_out else f"終了コード{rc}"
        if not continue_on_failure:
            raise StartupFailed(f"startup.sh が失敗しました: {reason}")
        return result
    _mark_done(workspace)
    return result
