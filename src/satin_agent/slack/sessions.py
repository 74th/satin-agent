"""Managed Agentsのセッション操作（`SessionsPort`の実装）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .ports import SessionBusy


class AnthropicSessions:
    def __init__(
        self,
        client: Any,
        *,
        agent_id: str,
        environment_id: str,
        vault_ids: list[str] | None = None,
    ) -> None:
        self._c = client  # AsyncAnthropic
        self._agent_id = agent_id
        self._env_id = environment_id
        self._vault_ids = list(vault_ids or [])

    async def create(self, metadata: dict[str, str]) -> str:
        kwargs: dict[str, Any] = {}
        if self._vault_ids:
            kwargs["vault_ids"] = self._vault_ids
        s = await self._c.beta.sessions.create(
            agent=self._agent_id, environment_id=self._env_id, metadata=metadata, **kwargs
        )
        return s.id

    async def _send(self, session_id: str, event: dict[str, Any]) -> None:
        try:
            await self._c.beta.sessions.events.send(session_id, events=[event])
        except Exception as e:
            # ツール実行・承認の応答待ちの間は、user.message等が400で拒否される
            if getattr(e, "status_code", None) == 400 and "waiting on responses" in str(e):
                raise SessionBusy(str(e)) from e
            raise

    async def send_message(self, session_id: str, text: str) -> None:
        await self._send(
            session_id, {"type": "user.message", "content": [{"type": "text", "text": text}]}
        )

    async def interrupt(self, session_id: str) -> None:
        await self._send(session_id, {"type": "user.interrupt"})

    async def confirm_tool(
        self, session_id: str, tool_use_id: str, allow: bool, reason: str | None = None
    ) -> None:
        ev: dict[str, Any] = {
            "type": "user.tool_confirmation",
            "tool_use_id": tool_use_id,
            "result": "allow" if allow else "deny",
        }
        if not allow and reason:
            ev["deny_message"] = reason
        await self._send(session_id, ev)

    async def open_stream(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        stream = await self._c.beta.sessions.events.stream(session_id)

        async def gen() -> AsyncIterator[dict[str, Any]]:
            async for ev in stream:
                yield ev.model_dump()

        return gen()

    async def list_events(self, session_id: str) -> list[dict[str, Any]]:
        out = []
        async for ev in self._c.beta.sessions.events.list(session_id):
            out.append(ev.model_dump())
        return out

    async def last_event_id(self, session_id: str) -> str:
        async for ev in self._c.beta.sessions.events.list(session_id, limit=1, order="desc"):
            return ev.id
        return ""

    async def status(self, session_id: str) -> dict[str, Any]:
        s = await self._c.beta.sessions.retrieve(session_id)
        d = s.model_dump()
        return {"status": d.get("status"), "stop_reason": d.get("stop_reason")}
