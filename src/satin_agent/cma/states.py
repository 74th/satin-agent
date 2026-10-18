"""Sandboxの状態（起動中・実行中・停止・失敗）をBot向けのConfigMapへ公開する。"""

from __future__ import annotations

import json

from ..ports import Clock, KubeApi
from .configmap import read_all, update_key

CONFIGMAP = "satin-sandbox-states"
STATES = ("starting", "running", "stopped", "failed")
MAX_CARRIED_TASKS = 20  # 引き継ぐ報告の上限（新しいものを優先）


def parse_finished_tasks(message: str) -> tuple[list[dict], int]:
    """ワーカーの終了メッセージから、伝えていないまま終わったバックグラウンドのタスクを読む。

    (タスクの一覧, 件数だけの報告のときの件数)。読めなければ空。項目は決まったものだけ取り出し、
    それ以外（秘密になりうる文字列）は公開しない。
    """
    try:
        data = json.loads(message) if message else {}
    except ValueError:
        return [], 0
    if not isinstance(data, dict):
        return [], 0
    tasks = []
    for t in data.get("finished_tasks") or []:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str):
            continue
        code = t.get("exit_code")
        tasks.append(
            {
                "id": t["id"],
                "command": str(t.get("command", ""))[:80],
                "state": str(t.get("state", "")),
                "exit_code": code if isinstance(code, int) else None,
            }
        )
    count = data.get("finished_count")
    return tasks, count if isinstance(count, int) and count > 0 else 0


class StatePublisher:
    def __init__(self, kube: KubeApi, clock: Clock, namespace: str) -> None:
        self._kube, self._clock, self._ns = kube, clock, namespace

    async def set(
        self,
        session_id: str,
        state: str,
        error: str | None = None,
        finished_tasks: list[dict] | None = None,
        finished_count: int | None = None,
    ) -> None:
        assert state in STATES
        now = self._clock.now()
        rec: dict[str, object] = {"state": state, "at": now}
        if error:
            rec["error"] = error
        # Botが読む前に次のturnの状態（starting・running）で上書きされると、報告が消える。
        # Botが通知済みのタスクを自分で見分けるので、前の報告を引き継いで残す。
        prev = await self.get(session_id) or {}
        tasks = list(finished_tasks or [])
        known = {t["id"] for t in tasks}
        tasks += [t for t in prev.get("finished_tasks", []) if t.get("id") not in known]
        if tasks:
            rec["finished_tasks"] = tasks[:MAX_CARRIED_TASKS]
        if finished_count:
            rec["finished_count"], rec["finished_count_at"] = finished_count, now
        elif prev.get("finished_count"):
            rec["finished_count"] = prev["finished_count"]
            rec["finished_count_at"] = prev.get("finished_count_at", 0.0)
        await update_key(self._kube, CONFIGMAP, self._ns, session_id, json.dumps(rec))

    async def get(self, session_id: str) -> dict | None:
        raw = (await read_all(self._kube, CONFIGMAP)).get(session_id)
        return json.loads(raw) if raw else None
