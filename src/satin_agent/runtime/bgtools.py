"""`bgtasks`のカスタムツール（`run`・`task_*`）と、終了の知らせをツール結果に付ける包み。"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import idle
from .bgtasks import TOOL_DEFS, BgConfig, TaskError, TaskManager

if TYPE_CHECKING:
    from anthropic.lib.tools.agent_toolset import AgentToolContext

_manager: TaskManager | None = None


def manager() -> TaskManager | None:
    """ワーカーのプロセスのタスク管理（ツールを作った後に取得できる）。"""
    return _manager


def reset() -> None:
    global _manager
    _manager = None


def create_manager(
    ctx: AgentToolContext,
    env: Mapping[str, str] | None = None,
    *,
    hold: Callable[[], None] = idle.hold,
    release: Callable[[], None] = idle.release,
) -> TaskManager:
    from anthropic.lib.tools.agent_toolset import resolve_path

    global _manager
    workdir = Path(ctx.workdir)
    _manager = TaskManager(
        root=workdir / ".satin" / "tasks",
        resolve_cwd=lambda p: resolve_path(ctx, p if p != "/workspace" else str(workdir)),
        config=BgConfig.from_env(os.environ if env is None else env),
        hold=hold,
        release=release,
    )
    return _manager


def bg_tools(mgr: TaskManager) -> list[Any]:
    """`TOOL_DEFS`の名前・説明・入力の形で、`@beta_async_tool`のツールを作る。

    TaskErrorはエラーの結果（`is_error`）にする。
    """
    from anthropic.lib.tools import ToolError, beta_async_tool

    async def guard(coro: Awaitable[str]) -> str:
        try:
            return await coro
        except (TaskError, idle.IdleClockMissing) as e:
            raise ToolError(str(e)) from e

    async def run(command: str, cwd: str | None = None, wait_seconds: float | None = None) -> str:
        return await guard(mgr.run_tool(command, cwd, wait_seconds))

    async def task_status(task_id: str | None = None) -> str:
        return await guard(mgr.status_tool(task_id))

    async def task_wait(task_id: str, seconds: float | None = None) -> str:
        return await guard(mgr.wait_tool(task_id, seconds))

    async def task_stop(task_id: str) -> str:
        return await guard(mgr.stop_tool(task_id))

    funcs = {f.__name__: f for f in (run, task_status, task_wait, task_stop)}
    return [
        beta_async_tool(
            funcs[d["name"]],
            name=d["name"],
            description=d["description"],
            input_schema=d["input_schema"],
        )
        for d in TOOL_DEFS
    ]


def _with_notices(content: Any, notices: list[str]) -> Any:
    text = "\n".join(notices)
    if isinstance(content, str):
        return f"{content}\n\n{text}" if content else text
    return [*content, {"type": "text", "text": text}]


def wrap_notices(tool: Any, mgr: TaskManager) -> Any:
    """ツールの結果（エラーも）の末尾に、伝えていないタスクの終了を1回だけ付ける。"""
    from anthropic.lib.tools import ToolError

    orig = tool.call

    async def call(input: object) -> Any:
        try:
            result = await orig(input)
        except ToolError as e:
            if notices := mgr.take_notices():
                raise ToolError(_with_notices(e.content, notices)) from e
            raise
        if notices := mgr.take_notices():
            return _with_notices(result, notices)
        return result

    tool.call = call
    return tool


def worker_tools(ctx: AgentToolContext, env: Mapping[str, str] | None = None) -> list[Any]:
    """`EnvironmentWorker(tools=...)`に渡す、組み込みのツールと`run`・`task_*`。"""
    from anthropic.lib.tools.agent_toolset import beta_agent_toolset_20260401

    mgr = create_manager(ctx, env)
    tools = [*beta_agent_toolset_20260401(ctx), *bg_tools(mgr)]
    return [wrap_notices(t, mgr) for t in tools]
