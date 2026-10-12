"""(A)のwork queueのport。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WorkItem:
    id: str
    type: str  # "session" 以外は何もしない
    session_id: str
    secret: str


class WorkSource(Protocol):
    def items(self) -> AsyncIterator[WorkItem]:
        """work itemを1件ずつ受け取る。次を要求した時点でpollする（上限中は要求しない）。"""
        ...

    async def stop(self, work_id: str) -> None:
        """work itemのleaseを手放す（起動に失敗した処理）。"""
        ...

    async def is_stopped(self, work_id: str) -> bool:
        """work itemが完了（stopped）になっているか。ワーカーは正常終了時に自分で停止する。
        リースを取れずに手を引いたワーカー（前のワーカーが落ちた後）は停止しない。"""
        ...
