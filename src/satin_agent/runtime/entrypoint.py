"""Sandbox内のentrypoint `satin-sandbox`。

環境変数:
  SATIN_MODE            cma | cc（必須）
  SATIN_SESSION_ID      セッションID（startup.shに渡す）
  AGENT_USER_EMAIL      セッションを始めたユーザのメール（任意、startup.shとツールへ渡る）
  SATIN_RUN_DIR         処理ごとのSecretのマウント先（既定 /run/satin）
  SATIN_ASSETS_URL      gs://bucket/prefix/（任意）
  SATIN_WORKSPACE       既定 /workspace
  SATIN_STARTUP_TIMEOUT startup.shの上限秒（既定300）
  SATIN_STARTUP_CONTINUE_ON_FAILURE  1ならstartup.shが失敗してもワーカーを起動
  SATIN_CMA_MAX_IDLE    (A) idle後の待機秒（既定10）
  SATIN_BASH_DEFAULT_TIMEOUT / SATIN_BASH_MAX_TIMEOUT  bashの既定・上限の秒（既定3600 / 7200）
  SATIN_CC_RELEASE_IDLE_MIN / SATIN_CC_DRAIN_WAIT_SEC / SATIN_HOST_CONFIG_DIR  (B)
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from ..ports import ObjectStore
from .assets import AssetBundleError, fetch_assets, place_for_claude_code
from .modes import (
    MODES,
    CcConfig,
    CmaHandler,
    RunInfoError,
    read_cma_work,
    run_cc,
    run_cma,
    sdk_handle_item,
)
from .startup import StartupFailed, run_startup
from .timeouts import apply_cma_timeouts, claude_code_env


def _log(msg: str) -> None:
    print(f"satin-sandbox: {msg}", file=sys.stderr, flush=True)


async def run(
    env: Mapping[str, str],
    *,
    store_factory: Callable[[str], ObjectStore] | None = None,
    cma_handler: CmaHandler = sdk_handle_item,
) -> int:
    mode = env.get("SATIN_MODE", "")
    if mode not in MODES:
        _log(f"未知のモードです: {mode!r}（cma または cc を指定してください）")
        return 2

    workspace = Path(env.get("SATIN_WORKSPACE", "/workspace"))
    run_dir = Path(env.get("SATIN_RUN_DIR", "/run/satin"))
    host_config = Path(env.get("SATIN_HOST_CONFIG_DIR", "/home/satin/.claude-host"))

    # 処理の情報を先に読む（なければ何も起動しない）
    try:
        cma_work = read_cma_work(run_dir) if mode == "cma" else None
        work_order = run_dir / "work_order"
        if mode == "cc" and not work_order.is_file():
            raise RunInfoError(f"{work_order} がありません")
    except RunInfoError as e:
        _log(str(e))
        return 2

    startup_script: Path | None = None
    if url := env.get("SATIN_ASSETS_URL"):
        if store_factory is None:
            from ..assets.adapters import GcsStore

            store_factory = GcsStore
        try:
            fetched = fetch_assets(store_factory(url), workspace / ".satin" / "assets")
        except AssetBundleError as e:
            _log(str(e))
            return 3
        startup_script = fetched.startup_sh
        if mode == "cc":
            place_for_claude_code(fetched.root, host_config)
    if startup_script is None:
        image_default = Path("/opt/satin/startup.sh")
        startup_script = image_default if image_default.is_file() else None

    try:
        await run_startup(
            startup_script,
            workspace=workspace,
            mode=mode,
            session_id=env.get("SATIN_SESSION_ID", ""),
            user_email=env.get("AGENT_USER_EMAIL", ""),
            timeout=float(env.get("SATIN_STARTUP_TIMEOUT", "300")),
            continue_on_failure=env.get("SATIN_STARTUP_CONTINUE_ON_FAILURE") == "1",
        )
    except StartupFailed as e:
        _log(str(e))
        return 4

    if mode == "cma":
        assert cma_work is not None
        apply_cma_timeouts(env)  # 1回のツール呼び出しの上限（SDKの既定は150秒）を上書きする
        return await run_cma(
            cma_work,
            workdir=str(workspace),
            max_idle=float(env.get("SATIN_CMA_MAX_IDLE", "10")),
            handler=cma_handler,
        )
    return await run_cc(
        CcConfig(
            work_order_file=work_order,
            base_dir=str(workspace / "runner"),
            host_config_dir=str(host_config),
            release_idle_min=int(env.get("SATIN_CC_RELEASE_IDLE_MIN", "10")),
            drain_wait_sec=int(env.get("SATIN_CC_DRAIN_WAIT_SEC", "60")),
            extra_env=claude_code_env(env),
        )
    )


def main(argv: list[str] | None = None) -> int:
    """`satin-sandbox [mode]`。引数のモードは環境変数SATIN_MODEより優先する。"""
    args = sys.argv[1:] if argv is None else argv
    env = dict(os.environ)
    if args:
        env["SATIN_MODE"] = args[0]
    return asyncio.run(run(env))


if __name__ == "__main__":
    raise SystemExit(main())
