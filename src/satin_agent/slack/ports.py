"""Bot（チャネル非依存の中核）のport。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ConvRef:
    """会話（Slackならスレッド）の場所。"""

    channel: str
    thread_ts: str

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.thread_ts}"

    @staticmethod
    def parse(key: str) -> ConvRef:
        channel, _, ts = key.partition(":")
        return ConvRef(channel, ts)


@dataclass(frozen=True)
class Inbound:
    """チャネルから届いたユーザの投稿（フィルタ前）。"""

    conv: ConvRef
    user: str
    text: str
    ts: str
    team: str = ""
    is_dm: bool = False
    bot_id: str | None = None
    subtype: str | None = None
    email: str = ""  # 判明している場合（セッションを始めたユーザのメール）


class ChatChannel(Protocol):
    async def post_progress(self, conv: ConvRef, text: str) -> str: ...
    async def update_progress(self, conv: ConvRef, ref: str, text: str) -> None: ...
    async def post(self, conv: ConvRef, text: str) -> None: ...
    async def post_file(self, conv: ConvRef, filename: str, text: str) -> None: ...


class SessionBusy(Exception):
    """ツール実行・承認待ちの間は`user.message`を送れない（API側が400で拒否する）。"""


class SessionsPort(Protocol):
    async def create(self, metadata: dict[str, str]) -> str: ...
    async def send_message(self, session_id: str, text: str) -> None: ...
    async def interrupt(self, session_id: str) -> None: ...
    async def confirm_tool(
        self, session_id: str, tool_use_id: str, allow: bool, reason: str | None = None
    ) -> None: ...
    async def open_stream(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """接続を確立してから返す（返った後に送ったメッセージのイベントを取りこぼさない）。"""
        ...

    async def list_events(self, session_id: str) -> list[dict[str, Any]]: ...
    async def status(self, session_id: str) -> dict[str, Any]:
        """{"status": "idle"|"running"|..., "stop_reason": {...}|None}"""
        ...
