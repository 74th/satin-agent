"""ライフサイクルログ（1行1JSON）。開始と終了（終了または失敗）が必ず対になる。"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

START_EVENTS = ("start",)
END_EVENTS = ("exit", "failed")


class LifecycleLog:
    def __init__(self, clock_now: Callable[[], float], out: TextIO | None = None) -> None:
        self._now = clock_now
        self._out = out if out is not None else sys.stdout
        self._open: dict[str, float] = {}  # sandbox名 → 開始時刻

    def event(self, event: str, *, session_id: str, sandbox: str, **fields: Any) -> None:
        now = self._now()
        rec: dict[str, Any] = {
            "ts": now,
            "event": event,
            "session_id": session_id,
            "sandbox": sandbox,
        }
        if event in START_EVENTS:
            self._open[sandbox] = now
        elif event in END_EVENTS and sandbox in self._open:
            rec["duration_s"] = round(now - self._open.pop(sandbox), 3)
        rec.update({k: v for k, v in fields.items() if v is not None})
        self._out.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        self._out.flush()

    def is_open(self, sandbox: str) -> bool:
        return sandbox in self._open
