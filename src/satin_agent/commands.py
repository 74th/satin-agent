"""サブコマンドの実体。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .config import Config, ConfigError, anthropic_client, load_dotenv


def dispatch(args: argparse.Namespace, cfg: Config) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    load_dotenv(".env")
    try:
        if args.command == "publish":
            return publish(args, cfg)
        if args.command == "sandbox" and args.sandbox_command == "gc":
            return asyncio.run(sandbox_gc(args, cfg))
        if args.command == "cma-orchestrator":
            return asyncio.run(cma_orchestrator(cfg))
        if args.command == "slack":
            from .slack.app import run_bot

            return asyncio.run(run_bot(cfg))
        if args.command == "cc" and args.cc_command == "spawn":
            return asyncio.run(cc_spawn(cfg))
        if args.command == "cc" and args.cc_command == "reconcile":
            return asyncio.run(cc_reconcile(cfg))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - CLI境界。メッセージだけ出す
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"error: '{args.command}' は未実装です", file=sys.stderr)
    return 1


def publish(args: argparse.Namespace, cfg: Config) -> int:
    from .assets.adapters import AnthropicApi, GcsStore
    from .assets.cma import State, publish_cma
    from .assets.gcs import publish_gcs
    from .assets.load import AssetError, load_assets

    targets = {t.strip() for t in args.target.split(",") if t.strip()}
    unknown = targets - {"cma", "gcs"}
    if unknown:
        raise ConfigError(f"未知の配布先: {sorted(unknown)}")
    try:
        assets = load_assets(args.assets)  # APIを呼ぶ前に検証する
    except AssetError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if "gcs" in targets:
        url = cfg.get("assets.gcs_url")
        if not url:
            raise ConfigError("設定の assets.gcs_url（gs://bucket/prefix/）が必要です")
        result = publish_gcs(assets, GcsStore(url, cfg.get("gcp.project")))
        print(
            f"[gcs] {url}{result['bundle']}: {len(result['files'])} files, {result['bytes']} bytes"
        )
    if "cma" in targets:
        state = State(Path(cfg.get("assets.state_file", ".satin-state.json")))
        agent = publish_cma(
            assets,
            AnthropicApi(anthropic_client()),
            state,
            name=cfg.get("agent.name", "satin-agent"),
            model=cfg.get("agent.model", "claude-haiku-5-5"),
            bash_always_ask=bool(cfg.get("agent.bash_always_ask", False)),
        )
        print(f"[cma] agent {agent['id']} version {agent['version']}")
    return 0


class _SystemClock:
    def now(self) -> float:
        import time

        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise ConfigError(f"環境変数 {name} が設定されていません")
    return v


async def sandbox_gc(args: argparse.Namespace, cfg: Config) -> int:
    from .sandbox.config import SandboxConfig
    from .sandbox.kube import KubeAsyncio
    from .sandbox.manager import SandboxManager

    scfg = SandboxConfig.from_config(cfg)
    async with KubeAsyncio(scfg.namespace, cfg.get("kubernetes.context")) as kube:
        deleted = await SandboxManager(kube, _SystemClock(), scfg).gc(delete_all=args.all)
    print(f"[gc] deleted {len(deleted)}: {', '.join(deleted)}")
    return 0


async def cma_orchestrator(cfg: Config) -> int:
    """(A)のオーケストレータ。environment keyだけを使い、組織のAPIキーは読まない。"""
    from .cma.adapters import AnthropicWorkSource
    from .cma.orchestrator import CmaOrchestrator
    from .cma.states import StatePublisher
    from .cma.users import UserDirectory
    from .sandbox.config import SandboxConfig
    from .sandbox.kube import KubeAsyncio
    from .sandbox.lifecycle_log import LifecycleLog
    from .sandbox.manager import SandboxManager

    env_id = _require_env("CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID")
    env_key = _require_env("CLAUDE_MANAGED_AGENT_ENVIRONMENT_KEY")
    hmac_key = _require_env("SATIN_HMAC_KEY").encode()
    scfg = SandboxConfig.from_config(cfg)
    clock = _SystemClock()
    async with KubeAsyncio(scfg.namespace, cfg.get("kubernetes.context")) as kube:
        orc = CmaOrchestrator(
            manager=SandboxManager(kube, clock, scfg, LifecycleLog(clock.now)),
            kube=kube,
            source=AnthropicWorkSource(
                environment_id=env_id,
                environment_key=env_key,
                reclaim_older_than_ms=int(cfg.get("cma.reclaim_older_than_ms", 300_000)),
            ),
            users=UserDirectory(kube, scfg.namespace, hmac_key),
            states=StatePublisher(kube, clock, scfg.namespace),
            environment_id=env_id,
            max_concurrency=int(cfg.get("cma.max_concurrency", 5)),
            max_restarts=int(cfg.get("cma.max_restarts", 2)),
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, orc.stop)
        await orc.run()
    return 0


async def cc_spawn(cfg: Config) -> int:
    from .cc.spawn import spawn
    from .sandbox.config import SandboxConfig
    from .sandbox.kube import KubeAsyncio
    from .sandbox.lifecycle_log import LifecycleLog
    from .sandbox.manager import SandboxManager

    scfg = SandboxConfig.from_config(cfg)
    clock = _SystemClock()
    async with KubeAsyncio(scfg.namespace, cfg.get("kubernetes.context")) as kube:
        return await spawn(os.environ, SandboxManager(kube, clock, scfg, LifecycleLog(clock.now)))


async def cc_reconcile(cfg: Config) -> int:
    from .cc.reconcile import CcReconciler
    from .sandbox.config import SandboxConfig
    from .sandbox.kube import KubeAsyncio
    from .sandbox.lifecycle_log import LifecycleLog
    from .sandbox.manager import SandboxManager

    scfg = SandboxConfig.from_config(cfg)
    clock = _SystemClock()
    async with KubeAsyncio(scfg.namespace, cfg.get("kubernetes.context")) as kube:
        rec = CcReconciler(
            SandboxManager(kube, clock, scfg, LifecycleLog(clock.now)),
            kube,
            resync_seconds=float(cfg.get("cc.resync_seconds", 30)),
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, rec.stop)
        await rec.run()
    return 0
