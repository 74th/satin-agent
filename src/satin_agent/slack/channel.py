"""Slackチャネル: 受信の振り分け、投稿、ユーザ情報（`users.info`）。"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..guards.filters import UserInfo
from ..ports import Clock
from .ports import ConvRef, Inbound

log = logging.getLogger("satin.slack")

DM_THREAD = "-"
MAX_MESSAGE_CHARS = 12_000
USER_CACHE_SECONDS = 600.0
_MENTION = re.compile(r"<@[A-Z0-9]+(\|[^>]*)?>")
_ACTIVE_MARKUP = re.compile(r"<(?=[!@#])")


def slack_unescape(text: str) -> str:
    """Slackは本文の&・<・>をエスケープする。エージェントには元の文字が必要
    （`sleep 1 &amp;&amp; echo` → `sleep 1 && echo`）。`&amp;`は最後に戻す。"""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def neutralize_mentions(text: str) -> str:
    """エージェントの出力が`<!channel>`・`<@U..>`などで一斉通知・メンションを起こさないようにする。"""
    return _ACTIVE_MARKUP.sub("&lt;", text)


class RecentIds:
    """再配信されたイベントの除外用（上限つき）。"""

    def __init__(self, limit: int = 5000) -> None:
        self._limit = limit
        self._ids: OrderedDict[str, None] = OrderedDict()

    def seen(self, value: str) -> bool:
        if value in self._ids:
            return True
        self._ids[value] = None
        if len(self._ids) > self._limit:
            self._ids.popitem(last=False)
        return False


@dataclass(frozen=True)
class SlackUser:
    email: str = ""
    is_guest: bool = False
    team_id: str = ""
    enterprise_id: str = ""


class SlackUserDirectory:
    """`users.info`の結果を10分キャッシュする（失敗はキャッシュしない）。

    `UsersInfo`（フィルタ用）も兼ねる。外部ユーザ（Slackコネクト等）は、
    ユーザのteamがBotのworkspaceと違うかで判定する。判定できなければ`None`。
    """

    def __init__(
        self, client: Any, clock: Clock, *, workspace_team: str = "", workspace_enterprise: str = ""
    ) -> None:
        self._client, self._clock = client, clock
        self._team, self._enterprise = workspace_team, workspace_enterprise
        self._cache: dict[str, tuple[float, SlackUser]] = {}

    async def lookup(self, user_id: str) -> SlackUser | None:
        now = self._clock.now()
        hit = self._cache.get(user_id)
        if hit and now < hit[0]:
            return hit[1]
        try:
            user = (await self._client.users_info(user=user_id))["user"]
            info = SlackUser(
                email=(user.get("profile") or {}).get("email", "") or "",
                is_guest=bool(user.get("is_restricted") or user.get("is_ultra_restricted")),
                team_id=user.get("team_id", "") or "",
                enterprise_id=(user.get("enterprise_user") or {}).get("enterprise_id", "") or "",
            )
        except Exception:
            log.warning("Slackのユーザ情報を取得できません: %s", user_id)
            return None
        self._cache[user_id] = (now + USER_CACHE_SECONDS, info)
        return info

    async def email(self, user_id: str) -> str:
        info = await self.lookup(user_id)
        return info.email if info else ""

    def _external(self, info: SlackUser) -> bool | None:
        if not self._team or not info.team_id:
            return None
        if info.team_id == self._team:
            return False
        if not self._enterprise:
            return True
        return info.enterprise_id != self._enterprise if info.enterprise_id else None

    async def info(self, user_id: str) -> UserInfo | None:
        u = await self.lookup(user_id)
        if u is None:
            return None
        return UserInfo(is_guest=u.is_guest, is_external=bool(self._external(u)), email=u.email)


def make_conv(channel: str, thread_ts: str | None) -> ConvRef:
    return ConvRef(channel, thread_ts or DM_THREAD)


class SlackEventRouter:
    """反応するのは、チャンネルでのメンション、Botが参加しているスレッドの返信、DM。"""

    def __init__(
        self,
        handle: Callable[[Inbound], Awaitable[None]],
        *,
        bot_user_id: str,
        is_participating: Callable[[ConvRef], bool],
        users: SlackUserDirectory | None = None,
    ) -> None:
        self._handle, self._bot, self._participating = handle, bot_user_id, is_participating
        self._users = users
        self._event_ids, self._messages = RecentIds(), RecentIds()

    async def on_app_mention(self, body: dict[str, Any]) -> None:
        event = body.get("event", {})
        if not self._first_delivery(body, event):
            return
        conv = make_conv(event["channel"], event.get("thread_ts") or event["ts"])
        await self._emit(conv, body, event)

    async def on_message(self, body: dict[str, Any]) -> None:
        event = body.get("event", {})
        if event.get("user") == self._bot:
            return  # Bot自身の投稿
        channel = event.get("channel", "")
        if event.get("channel_type") == "im":
            if self._first_delivery(body, event):
                await self._emit(make_conv(channel, event.get("thread_ts")), body, event)
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == event.get("ts"):
            return  # チャンネルの最上位の投稿は、メンション（app_mention）だけが会話を始める
        conv = make_conv(channel, thread_ts)
        if not self._participating(conv):
            return
        if self._first_delivery(body, event):
            await self._emit(conv, body, event)

    def _first_delivery(self, body: dict[str, Any], event: dict[str, Any]) -> bool:
        """再配信（同じevent_id）や、app_mentionとmessageの二重配信を除く。"""
        eid = body.get("event_id")
        if eid and self._event_ids.seen(eid):
            return False
        return not self._messages.seen(f"{event.get('channel', '')}:{event.get('ts', '')}")

    async def _emit(self, conv: ConvRef, body: dict[str, Any], event: dict[str, Any]) -> None:
        user = event.get("user", "")
        email = await self._users.email(user) if (self._users and user) else ""
        text = slack_unescape(_MENTION.sub("", event.get("text", ""))).strip()
        await self._handle(
            Inbound(
                conv=conv,
                user=user,
                text=text,
                ts=event.get("ts", ""),
                team=body.get("team_id") or event.get("team", ""),
                is_dm=event.get("channel_type") == "im",
                bot_id=event.get("bot_id"),
                subtype=event.get("subtype"),
                email=email,
            )
        )


class SlackChat:
    """`ChatChannel`のSlack実装。"""

    def __init__(self, client: Any, max_chars: int = MAX_MESSAGE_CHARS) -> None:
        self._client, self._max = client, max_chars

    @staticmethod
    def _thread(conv: ConvRef) -> str | None:
        return None if conv.thread_ts == DM_THREAD else conv.thread_ts

    async def _post(self, conv: ConvRef, text: str) -> str:
        kw: dict[str, Any] = {"channel": conv.channel, "text": text}
        if t := self._thread(conv):
            kw["thread_ts"] = t
        return (await self._client.chat_postMessage(**kw))["ts"]

    async def post_progress(self, conv: ConvRef, text: str) -> str:
        return await self._post(conv, text)

    async def update_progress(self, conv: ConvRef, ref: str, text: str) -> None:
        await self._client.chat_update(channel=conv.channel, ts=ref, text=text)

    async def post(self, conv: ConvRef, text: str) -> None:
        if not text.strip():
            return
        if len(text) > self._max:  # 長い返答はファイルにする
            await self.post_file(conv, "response.md", text)
            return
        await self._post(conv, neutralize_mentions(text))

    async def post_file(self, conv: ConvRef, filename: str, text: str) -> None:
        kw: dict[str, Any] = {
            "channel": conv.channel,
            "content": text,
            "filename": filename,
            "title": "応答（長いためファイルにしました）",
        }
        if t := self._thread(conv):
            kw["thread_ts"] = t
        await self._client.files_upload_v2(**kw)


class SlackReceiver:
    """Boltのリスナーを登録する。リスナーはすぐ返り（3秒以内のack）、処理は別タスクで行う。"""

    def __init__(self, app: Any, router: SlackEventRouter) -> None:
        self._app, self.router = app, router
        self._tasks: set[asyncio.Task] = set()

    def register(self) -> None:
        @self._app.event("app_mention")
        async def on_mention(body: dict[str, Any]) -> None:
            self._spawn(self.router.on_app_mention(body))

        @self._app.event("message")
        async def on_message(body: dict[str, Any]) -> None:
            self._spawn(self.router.on_message(body))

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception:
                log.exception("Slackイベントの処理に失敗")

        t = asyncio.ensure_future(run())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
