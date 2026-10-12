"""Sandboxの状態（起動中・実行中・停止・失敗）をBot向けのConfigMapへ公開する。"""

from __future__ import annotations

import json

from ..ports import Clock, KubeApi
from .configmap import read_all, update_key

CONFIGMAP = "satin-sandbox-states"
STATES = ("starting", "running", "stopped", "failed")


class StatePublisher:
    def __init__(self, kube: KubeApi, clock: Clock, namespace: str) -> None:
        self._kube, self._clock, self._ns = kube, clock, namespace

    async def set(self, session_id: str, state: str, error: str | None = None) -> None:
        assert state in STATES
        rec: dict[str, object] = {"state": state, "at": self._clock.now()}
        if error:
            rec["error"] = error
        await update_key(self._kube, CONFIGMAP, self._ns, session_id, json.dumps(rec))

    async def get(self, session_id: str) -> dict | None:
        raw = (await read_all(self._kube, CONFIGMAP)).get(session_id)
        return json.loads(raw) if raw else None
