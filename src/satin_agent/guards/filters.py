"""ルールのフィルタ（入力ガードより前に走る）。silk-agentの`core/filters.py`の移植。

除外した投稿は`reply=None`（Botは黙る）、長すぎる・送信が多すぎるときだけ`reply`で理由を返す。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Protocol

from satin_agent.guards.config import FilterConfig
from satin_agent.ports import Clock

log = logging.getLogger(__name__)

# 通常のメッセージとして扱うsubtype。それ以外（編集・参加通知など）はシステムイベント
_MESSAGE_SUBTYPES = frozenset({None, "thread_broadcast", "file_share"})


@dataclass(frozen=True)
class InboundMessage:
    team: str
    channel: str
    user: str
    text: str
    ts: str
    thread_ts: str | None
    is_dm: bool
    bot_id: str | None = None
    subtype: str | None = None


@dataclass(frozen=True)
class FilterResult:
    allowed: bool
    # 設定があるときだけ利用者へ返す。Noneなら黙って無視する
    reply: str | None = None
    # 除外した規則の名前（ログ・フック用）
    reason: str = ""


@dataclass(frozen=True)
class UserInfo:
    is_guest: bool
    is_external: bool
    email: str | None = None


class UsersInfo(Protocol):
    """`users.info`相当。判定できないときはNone（例外でもよい）を返す。"""

    async def info(self, user_id: str) -> UserInfo | None: ...


_ALLOWED = FilterResult(True)


def _excluded(reason: str, reply: str | None = None) -> FilterResult:
    return FilterResult(False, reply, reason)


class RuleFilter:
    def __init__(
        self, config: FilterConfig, users: UsersInfo, clock: Clock, bot_user_id: str
    ) -> None:
        self._cfg = config
        self._users = users
        self._clock = clock
        self._bot_user_id = bot_user_id
        self._cache: dict[str, tuple[float, UserInfo | None]] = {}
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def check(self, msg: InboundMessage) -> FilterResult:
        cfg = self._cfg
        if msg.bot_id or msg.user == self._bot_user_id or not msg.user:
            return _excluded("bot")
        if msg.subtype not in _MESSAGE_SUBTYPES:
            return _excluded("system_event")
        if not msg.text.strip():
            return _excluded("empty")
        if cfg.teams and msg.team not in cfg.teams:
            return _excluded("team_not_allowed")
        if cfg.channels and not msg.is_dm and msg.channel not in cfg.channels:
            return _excluded("channel_not_allowed")
        if cfg.users and msg.user not in cfg.users:
            return _excluded("user_not_allowed")
        if msg.is_dm and not cfg.allow_direct_messages:
            return _excluded("direct_message")
        if cfg.deny_guests or cfg.deny_external_users:
            info = await self._lookup(msg.user)
            # 判定できないときは除外する（拒否したい種別のときだけ）
            if cfg.deny_guests and (info is None or info.is_guest):
                return _excluded("guest")
            if cfg.deny_external_users and (info is None or info.is_external):
                return _excluded("external_user")
        if len(msg.text) > cfg.max_chars:
            return _excluded(
                "too_long",
                f"メッセージが長すぎます（上限 {cfg.max_chars} 文字）。"
                "短くして送り直してください。",
            )
        if not self._rate_ok(msg.user):
            return _excluded("rate_limited", "送信が多すぎます。少し待ってから送ってください。")
        return _ALLOWED

    async def _lookup(self, user_id: str) -> UserInfo | None:
        now = self._clock.now()
        cached = self._cache.get(user_id)
        if cached is not None and now - cached[0] < self._cfg.users_cache_seconds:
            return cached[1]
        try:
            info = await self._users.info(user_id)
        except Exception:
            log.exception("users.info failed for %s", user_id)
            return None  # 失敗はキャッシュしない
        if info is not None:
            self._cache[user_id] = (now, info)
        return info

    def _rate_ok(self, user: str) -> bool:
        limit = self._cfg.rate_limit_per_minute
        if limit <= 0:
            return True
        now = self._clock.now()
        hits = self._hits[user]
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True
