"""(A) work queueの実装（environment keyだけで`work.poller`を使う）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from .ports import WorkItem


class AnthropicWorkSource:
    def __init__(
        self, *, environment_id: str, environment_key: str, reclaim_older_than_ms: int = 300_000
    ) -> None:
        from anthropic import AsyncAnthropic

        self._env_id = environment_id
        self._key = environment_key
        self._reclaim = reclaim_older_than_ms
        # 組織スコープのAPIキーは使わない。environment keyだけを認証に使う。
        self._client = AsyncAnthropic(auth_token=environment_key)

    async def items(self) -> AsyncIterator[WorkItem]:
        async for work in self._client.beta.environments.work.poller(
            environment_id=self._env_id,
            environment_key=self._key,
            block_ms=500,
            reclaim_older_than_ms=self._reclaim,
            drain=False,
            auto_stop=False,
        ):
            yield WorkItem(
                id=work.id,
                type=work.data.type,
                session_id=getattr(work.data, "id", ""),
                secret=work.secret or "",
            )

    async def stop(self, work_id: str) -> None:
        await self._client.beta.environments.work.stop(
            work_id, environment_id=self._env_id, force=True
        )

    async def is_stopped(self, work_id: str) -> bool:
        seen = 0
        async for w in self._client.beta.environments.work.list(
            environment_id=self._env_id, limit=50
        ):
            if w.id == work_id:
                return str(w.state) == "stopped"
            seen += 1
            if seen >= 100:
                break
        return False
