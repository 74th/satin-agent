"""監査フック（silk-agentの`hooks.py`の移植）。観測専用のコールバックで、非同期のキューで配信する。

`HookDispatcher.emit`はキューに積むだけで待たない。フックが遅い・例外を出しても、会話の処理も
ほかのフックも止めない。キューが上限を超えたら古いものから捨ててログに残す。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

log = logging.getLogger(__name__)

# イベントの種類
USER_INPUT = "user_input"  # 本文・送信者・フィルタと入力ガードの判定（除外・ブロックも含む）
TURN_START = "turn_start"
TURN_END = "turn_end"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
BASH_APPROVAL = "bash_approval"  # bashの実行前確認の判定（allow / deny と理由）
AGENT_MESSAGE = "agent_message"  # 検査前の本文と検査の結果
NOTICE = "notice"
EVENT_KINDS = (
    USER_INPUT,
    TURN_START,
    TURN_END,
    TOOL_USE,
    TOOL_RESULT,
    BASH_APPROVAL,
    AGENT_MESSAGE,
    NOTICE,
)

# `json_lines`が、本文を出さない設定のときに`data`から取り除くキー
BODY_KEYS = frozenset({"text", "tool_input", "tool_result", "command", "message", "body"})
DEFAULT_QUEUE_LIMIT = 1000
DEFAULT_HOOK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class HookEvent:
    kind: str
    session_id: str | None
    ts: float
    data: dict[str, Any] = field(default_factory=dict)


class Hook(Protocol):
    async def on_event(self, event: HookEvent) -> None: ...


class HookLoadError(ValueError):
    """設定したフックを読み込めない（Botは起動しない）。"""


class JsonLinesHook:
    """1イベント1行のJSONを標準出力へ出す（Cloud Loggingが構造化ログとして集める）。"""

    def __init__(self, include_body: bool = False, out: TextIO | None = None) -> None:
        self._include_body = include_body
        self._out = out

    async def on_event(self, event: HookEvent) -> None:
        data = event.data
        if not self._include_body:
            data = {k: v for k, v in data.items() if k not in BODY_KEYS}
        line = json.dumps(
            {"kind": event.kind, "session_id": event.session_id, "ts": event.ts, "data": data},
            ensure_ascii=False,
            default=str,
        )
        out = self._out if self._out is not None else sys.stdout
        out.write(line + "\n")
        out.flush()


def load_hooks(specs: list[dict]) -> list[Hook]:
    """設定の`hooks`を読み込む。各要素は`{"factory": "json_lines" | "pkg.mod:attr",
    "options": {...}}`。読み込めなければ`HookLoadError`（起動を止める）。"""
    hooks: list[Hook] = []
    for spec in specs:
        if isinstance(spec, str):
            spec = {"factory": spec}
        if not isinstance(spec, dict) or not isinstance(spec.get("factory"), str):
            raise HookLoadError(f"hook spec {spec!r}: 'factory' is required")
        name: str = spec["factory"]
        options = spec.get("options") or {}
        if not isinstance(options, dict):
            raise HookLoadError(f"hook {name!r}: options must be a mapping")
        factory: Callable[..., Any]
        if name == "json_lines":
            factory = JsonLinesHook
        else:
            module_name, _, attr = name.partition(":")
            if not module_name or not attr:
                raise HookLoadError(f"hook {name!r}: expected 'package.module:factory'")
            try:
                factory = getattr(importlib.import_module(module_name), attr)
            except ImportError as e:
                raise HookLoadError(f"hook {name!r}: cannot import {module_name!r}: {e}") from e
            except AttributeError as e:
                raise HookLoadError(f"hook {name!r}: {module_name!r} has no {attr!r}") from e
        try:
            hook = factory(**options)
        except Exception as e:
            raise HookLoadError(f"hook {name!r}: the factory failed: {e}") from e
        if not inspect.iscoroutinefunction(getattr(hook, "on_event", None)):
            raise HookLoadError(f"hook {name!r}: the factory did not return a Hook")
        hooks.append(hook)
    return hooks


class HookDispatcher:
    """イベントをキューに積み、1つのワーカーが順に全フックへ渡す。"""

    def __init__(
        self,
        hooks: list[Hook],
        *,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        hook_timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._hooks = list(hooks)
        self._limit = max(1, queue_limit)
        self._timeout = hook_timeout_seconds
        self._queue: deque[HookEvent] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self.dropped = 0

    def emit(self, event: HookEvent) -> None:
        """キューに積む（待たない・例外を出さない）。上限を超えたら最も古いものを捨てる。"""
        if self._closed or not self._hooks:
            return
        if len(self._queue) >= self._limit:
            dropped = self._queue.popleft()
            self.dropped += 1
            log.warning(
                "hook queue is full (%d): dropped the oldest event (%s); %d dropped so far",
                self._limit,
                dropped.kind,
                self.dropped,
            )
        self._queue.append(event)
        self._idle.clear()
        self._wake.set()

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.get_running_loop().create_task(
                self._run(), name="hook-dispatcher"
            )

    async def _run(self) -> None:
        while True:
            if not self._queue:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            event = self._queue.popleft()
            for hook in self._hooks:
                await self._deliver(hook, event)

    async def _deliver(self, hook: Hook, event: HookEvent) -> None:
        name = type(hook).__name__
        try:
            await asyncio.wait_for(hook.on_event(event), self._timeout)
        except TimeoutError:
            log.error(
                "hook %s took more than %.0f s on %s; skipped", name, self._timeout, event.kind
            )
        except Exception:
            log.exception("hook %s failed on %s", name, event.kind)

    async def aclose(self, timeout_seconds: float = 10.0) -> None:
        """積まれた分を配信（上限時間内）してから、フックを閉じる。"""
        self._closed = True
        worker = self._worker
        if worker is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), timeout_seconds)
            if self._queue:
                log.warning("hook shutdown: %d event(s) were not delivered", len(self._queue))
                self._queue.clear()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
            self._worker = None
        for hook in self._hooks:
            close = getattr(hook, "aclose", None)
            if close is None:
                continue
            try:
                await asyncio.wait_for(close(), self._timeout)
            except Exception:
                log.exception("hook %s failed on close", type(hook).__name__)
        if self.dropped:
            log.warning("hooks dropped %d event(s) because the queue was full", self.dropped)
