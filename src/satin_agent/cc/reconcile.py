"""(B) Podの終了の検出と停止、保持期限の削除（オーケストレータのPodのサイドカー）。"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..ports import KubeApi
from ..sandbox.manager import SandboxManager, is_terminated, operating_mode
from ..sandbox.naming import ANN_SESSION_ID, LABEL_MODE

log = logging.getLogger("satin.cc")


class CcReconciler:
    def __init__(
        self,
        mgr: SandboxManager,
        kube: KubeApi,
        *,
        resync_seconds: float = 30.0,
        gc_seconds: float = 300.0,
    ) -> None:
        self.mgr, self.kube = mgr, kube
        self.resync, self.gc_every = resync_seconds, gc_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def reconcile_once(self) -> list[str]:
        """終了したのに`Running`のままのSandboxを停止する。"""
        done = []
        for sb in await self.kube.list("sandbox", f"{LABEL_MODE}=cc"):
            if operating_mode(sb) == "Running" and is_terminated(sb):
                sid = sb["metadata"].get("annotations", {}).get(ANN_SESSION_ID, "")
                await self.mgr.finish(sid, sb)
                done.append(sb["metadata"]["name"])
        return done

    async def run(self) -> None:
        last_gc = self.mgr.clock.now()
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
                if self.mgr.clock.now() - last_gc >= self.gc_every:
                    await self.mgr.gc()
                    last_gc = self.mgr.clock.now()
            except Exception:
                log.exception("reconcileに失敗（次の周期でやり直す）")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.resync)
