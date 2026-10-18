"""会話の中核（チャネル非依存）。

スレッド = Managed Agentsのセッション1つ。Botはセッションのイベントを送受信するだけ。
エージェントループはAnthropic側にあり、turnの途中でBotが落ちてもturnは続く。
復帰時は`events.list`で取りこぼしを読み、購読を再開する。

Managed Agentsの制約（スパイク1.3）:
- ツール実行・承認待ちの間は`user.message`が拒否される
  → 追加指示は`user.interrupt`でツールを中断してから送る（ユーザが決定）。
  ただし、ワーカーが提供するバックグラウンド実行のツール（`run`・`task_*`）は最長60秒で
  返るので、中断せずに結果を待ってから送る。
- モデル生成中の`user.message`はキューされ、現在のturnの後に別のturnとして処理される。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..ports import Clock
from ..runtime.bgtasks import BACKGROUND_RE, FINISHED_RES, STATE_LABEL
from .ports import ChatChannel, ConvRef, Inbound, SessionBusy, SessionsPort
from .progress import ProgressView, ToolView, load_timezone
from .store import RUNNING, BotStore, TurnRecord

log = logging.getLogger("satin.slack")

NOTHING_RUNNING = "実行中の処理はありません。"
INPUT_CHECK_FAILED = (
    "入力を確認できなかったため、処理しませんでした。しばらくしてからもう一度お試しください。"
)
OUTPUT_HIDDEN = "（ポリシーにより表示できません）"
ACK_TEXT = "⏳ 受け付けました。処理を開始しています…"
ACK_REJECTED = "処理しませんでした"
SANDBOX_FAILED = (
    "作業環境を起動できませんでした。しばらくしてからもう一度メッセージを送ってください。"
    "続く場合は管理者に連絡してください。"
)
GENERIC_FAILED = (
    "処理中にエラーが発生しました。もう一度メッセージを送ってください。"
    "続く場合は管理者に連絡してください。"
)
# ワーカーがSandbox内で実行するカスタムツール。最長60秒で返るので、追加指示は中断せずに待つ。
BG_TOOL_NAMES = frozenset({"run", "task_status", "task_wait", "task_stop"})
BG_NOTICE_POLL_SECONDS = 10.0
BG_NOTICE_PREFIX = "[自動通知]"
# Sandboxの状態が読めないまま、これだけ経った監視は捨てる（読めている間は時間で捨てない）
BG_WATCH_TTL = 3 * 3600.0
TOOL_NAMES = ("agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use")
# Sandbox内のワーカー（client側）は`user.tool_result`で結果を返す。`agent.*_result`はサーバ側が出す
# （MCP・中断・拒否など）。
RESULT_NAMES = (
    "user.tool_result",
    "agent.tool_result",
    "agent.mcp_tool_result",
    "user.custom_tool_result",
)


# --- ガード類（実体は guards/ にある。ここでは呼び出しの形だけ決める） ---
class VerdictLike(Protocol):
    @property
    def decision(self) -> str: ...
    @property
    def reason(self) -> str: ...


class InspectLike(Protocol):
    @property
    def action(self) -> str: ...
    @property
    def text(self) -> str | None: ...
    @property
    def message(self) -> str | None: ...


class BashVerdictLike(Protocol):
    @property
    def allow(self) -> bool: ...
    @property
    def reason(self) -> str: ...


RuleCheck = Callable[[Inbound], Awaitable[tuple[bool, str | None]]]
InputGuardFn = Callable[[str, list[str]], Awaitable[VerdictLike]]
InspectFn = Callable[[str], Awaitable[InspectLike]]
BashCheckFn = Callable[[str], BashVerdictLike]
HookEmit = Callable[[str, str | None, dict[str, Any]], None]


@dataclass(frozen=True)
class SandboxStatus:
    """オーケストレータが公開したSandboxの状態。`at`はその状態になった時刻。"""

    state: str  # "starting" | "running" | "stopped" | "failed"
    at: float = 0.0
    error: str = ""
    # ワーカーの終了時に報告された、伝えていないまま終わったバックグラウンドのタスク
    finished_tasks: tuple[dict[str, Any], ...] = ()
    finished_count: int = 0  # 報告が4KBを超えたときの、件数だけ
    finished_count_at: float = 0.0  # 件数だけの報告を公開した時刻（通知済みの判定に使う）


SandboxState = Callable[[str], Awaitable["SandboxStatus | str | None"]]


def _status(raw: SandboxStatus | str | None) -> SandboxStatus | None:
    if raw is None:
        return None
    return raw if isinstance(raw, SandboxStatus) else SandboxStatus(raw)


RegisterUser = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class Settings:
    stop_words: tuple[str, ...] = ("停止", "中止", "中断", "STOP")
    min_update_interval: float = 1.0
    heartbeat_seconds: float = 60.0
    sandbox_poll_seconds: float = 3.0
    bg_poll_seconds: float = BG_NOTICE_POLL_SECONDS
    tick_seconds: float = 1.0
    history_for_guard: int = 5
    timezone: str = "Asia/Tokyo"  # 進捗メッセージの「最終更新」の時刻（IANA名）


def bg_notice_text(tasks: list[dict[str, Any]], count: int = 0) -> str:
    """終わったバックグラウンドのタスクを、エージェントへ知らせる`user.message`。"""
    if not tasks:
        return (
            f"[自動通知] バックグラウンドの{count}件のタスクが終了しました。"
            "task_statusで結果を確認し、依頼に沿って報告してください。"
        )
    lines = []
    for t in tasks:
        code = f"（終了コード {t['exit_code']}）" if t.get("exit_code") is not None else ""
        # 停止・上限超過・消失を「終了」と伝えると、task_statusを呼ばずに完了として報告されうる
        label = STATE_LABEL.get(str(t.get("state", "")), "終了")
        lines.append(f"- {t['id']}（`{t.get('command', '')}`）が{label}しました{code}")
    return (
        "[自動通知] バックグラウンドのタスクの実行が終わりました。\n"
        + "\n".join(lines)
        + "\ntask_statusで結果を確認し、依頼に沿って報告してください。"
    )


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().upper()


def tool_summary(name: str, inp: dict[str, Any]) -> str:
    """進捗メッセージ用のツールの要約（1行）。"""
    for key in ("command", "file_path", "path", "pattern", "query", "url"):
        if key in inp:
            s = " ".join(str(inp[key]).split())
            return s if len(s) <= 80 else s[:77] + "…"
    return ""


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
    return "".join(out)


@dataclass
class TurnState:
    tools: list[dict[str, Any]] = field(default_factory=list)
    pending_tools: list[str] = field(default_factory=list)
    buffer: list[str] = field(default_factory=list)  # ツール実行中に受けた追加指示
    sent: int = 0
    echoed: int = 0
    appended: int = 0
    interrupting: bool = False
    decided: list[str] = field(default_factory=list)
    error: str = ""
    preparing: bool = False
    first_text: str = ""  # 最初の指示（送信前にBotが落ちたときの再送用）
    boundary: str = ""  # 最初の指示を送る直前の、セッションの最後のイベントID
    boundary_set: bool = False
    first_counted: bool = False  # 最初の指示を、送信件数（sent）に数えた
    bg_open: list[str] = field(
        default_factory=list
    )  # バックグラウンドで動いている（と思われる）タスク


class Turn:
    """1回の依頼（turn）の進行。イベントを処理して進捗メッセージとSlackへの投稿を更新する。"""

    def __init__(self, core: ConversationCore, rec: TurnRecord, conv: ConvRef) -> None:
        self.core, self.rec, self.conv = core, rec, conv
        st = rec.state
        self.st = TurnState(
            tools=st.get("tools", []),
            pending_tools=st.get("pending_tools", []),
            buffer=st.get("buffer", []),
            sent=st.get("sent", 0),
            echoed=st.get("echoed", 0),
            appended=st.get("appended", 0),
            interrupting=st.get("interrupting", False),
            decided=st.get("decided", []),
            first_text=st.get("first_text", ""),
            boundary=st.get("boundary", ""),
            boundary_set=st.get("boundary_set", False),
            # 古い記録（このフィールドがない）は、送信件数に数えてから保存されている
            first_counted=st.get("first_counted", st.get("sent", 0) >= 1),
            bg_open=st["bg_open"] if "bg_open" in st else core.bg_tasks_of(conv.key),
        )
        self.view = ProgressView(status="thinking", tz=load_timezone(core.s.timezone))
        self.done = asyncio.Event()
        self.final: str = ""  # finished | failed | interrupted
        self.seen: set[str] = set()
        self.dirty = False
        self.last_update = 0.0
        self.last_poll = 0.0
        self._ulock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
        self.replaying = False

    # --- 永続化 ---
    def _persist(self) -> None:
        s = self.st
        self.rec.state = {
            "tools": s.tools,
            "pending_tools": s.pending_tools,
            "buffer": s.buffer,
            "sent": s.sent,
            "echoed": s.echoed,
            "appended": s.appended,
            "interrupting": s.interrupting,
            "decided": s.decided,
            "first_text": s.first_text,
            "boundary": s.boundary,
            "boundary_set": s.boundary_set,
            "first_counted": s.first_counted,
            "bg_open": s.bg_open,
        }
        self.core.store.save_turn(self.rec)

    # --- 進捗メッセージ ---
    def _rebuild_view(self) -> None:
        v = self.view
        v.tools = [ToolView(t["id"], t["name"], t["summary"], t["status"]) for t in self.st.tools]
        v.appended = self.st.appended
        n = len(self.st.bg_open)
        v.note = f"バックグラウンドで実行中のタスク: {n}件（終わったらお知らせします）" if n else ""

    async def flush(self, force: bool = False) -> None:
        async with self._ulock:
            now = self.core.clock.now()
            if not force and now - self.last_update < self.core.s.min_update_interval:
                self.dirty = True
                return
            self._rebuild_view()
            self.view.updated_at = now
            self.last_update, self.dirty = now, False
            try:
                if not self.rec.progress_ref:
                    self.rec.progress_ref = await self.core.channel.post_progress(
                        self.conv, self.view.render()
                    )
                    self._persist()
                else:
                    await self.core.channel.update_progress(
                        self.conv, self.rec.progress_ref, self.view.render()
                    )
            except Exception:
                log.exception("進捗メッセージの更新に失敗")

    def _set_status(self, status: str) -> None:
        self.view.status = status

    # --- 起動 ---
    def start(self, first_text: str | None) -> None:
        self._task = asyncio.ensure_future(self._run(first_text))

    def resume(self) -> None:
        self._task = asyncio.ensure_future(self._run(None))

    async def _run(self, first_text: str | None) -> None:
        s = self.core
        self._ticker = asyncio.ensure_future(self._tick())
        try:
            if first_text is not None:
                await self.flush(force=True)
                self.core.emit("turn_start", self.rec.session_id, conv=self.conv.key)
                stream = await s.sessions.open_stream(self.rec.session_id)
                await self._send_first(first_text)
            else:  # 再起動からの復帰
                stream = await self._catch_up()
                if self.done.is_set() or stream is None:
                    return
            await self._consume(stream)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("turnの処理に失敗")
            await self._finish("failed", error=str(e))
        finally:
            if self._ticker:
                self._ticker.cancel()

    async def _catch_up(self):
        """取りこぼしたイベントを`events.list`で処理し、まだ続いていれば購読を再開する。"""
        sid = self.rec.session_id
        events = await self.core.sessions.list_events(sid)
        if not self.rec.last_event_id and self.st.first_text:
            # このturnの最初の指示を送った記録がなければ、送る前に落ちている。同じセッションの
            # 過去のturnのイベントを、このturnのものとして処理しない（過去の返答の再投稿・即完了）。
            start = self._index_of_first_message(events)
            for ev in events[: start if start is not None else len(events)]:
                self.seen.add(ev["id"])
            if start is None:
                return await self._resend_or_fail(sid)  # 履歴はseenにしてあるので処理しない
            events = events[start:]
        skip = self.rec.last_event_id != ""
        self.replaying = True
        for ev in events:
            if skip:
                self.seen.add(ev["id"])
                if ev["id"] == self.rec.last_event_id:
                    skip = False
                continue
            await self._on_event(ev)
            if self.done.is_set():
                break
        self.replaying = False
        if self.done.is_set():
            return None
        if not any(e["type"] == "user.message" for e in events):
            return await self._resend_or_fail(sid)
        # 確認待ち（requires_action）のまま残ったbashに応答する
        await self._answer_pending_confirms(events)
        return await self.core.sessions.open_stream(sid)

    def _index_of_first_message(self, events: list[dict[str, Any]]) -> int | None:
        """このturnの最初の指示のイベント。送る前に記録した境界（送る直前の最後のイベント）より
        後だけを探す。過去に同じ文面の指示があっても、送信済みと誤認しない。境界を記録する前に
        落ちたなら、まだ何も送っていない。"""
        if not self.st.boundary_set:
            return None
        start = 0
        if self.st.boundary:
            ids = [e["id"] for e in events]
            if self.st.boundary not in ids:
                return None
            start = ids.index(self.st.boundary) + 1
        for i in range(start, len(events)):
            ev = events[i]
            if ev["type"] == "user.message" and text_of(ev.get("content")) == self.st.first_text:
                return i
        return None

    async def _send_first(self, text: str) -> None:
        """最初の指示を送る（初回も、復帰時の再送も同じ手順）。

        送信前に、指示・送信件数・境界（送る直前の最後のイベント）を永続化する。落ちても、復帰時に
        再送でき、送信済みなら境界より後のイベントから見つけて二重に送らない。件数は1回だけ数える。
        """
        st, s, sid = self.st, self.core, self.rec.session_id
        st.first_text = text
        if not st.first_counted:
            st.sent += 1
            st.first_counted = True
        st.boundary = await s.sessions.last_event_id(sid)
        st.boundary_set = True
        self._persist()
        await s.sessions.send_message(sid, text)

    async def _resend_or_fail(self, sid: str):
        """セッションの作成後、最初の指示を送る前にBotが落ちた。保存した指示を再送する。
        保存がなければ（古い記録）、続けられないので失敗にして、再送を案内する。"""
        if not self.st.first_text:
            await self._finish("failed", error="最初の指示が送られませんでした（Botの再起動）")
            return None
        stream = await self.core.sessions.open_stream(sid)
        await self._send_first(self.st.first_text)
        return stream

    async def _answer_pending_confirms(self, events: list[dict[str, Any]]) -> None:
        last_idle = None
        for ev in events:
            if ev["type"] == "session.status_idle":
                last_idle = ev
            elif ev["type"] in ("session.status_running", "user.message"):
                last_idle = None
        if not last_idle:
            return
        sr = last_idle.get("stop_reason") or {}
        if sr.get("type") != "requires_action":
            return
        by_id = {e["id"]: e for e in events}
        for eid in sr.get("event_ids", []):
            ev = by_id.get(eid)
            if ev and ev["type"] in TOOL_NAMES and eid not in self.st.decided:
                if ev.get("evaluated_permission") == "ask":
                    await self._decide(ev)

    async def _consume(self, stream) -> None:
        async for ev in stream:
            await self._on_event(ev)
            if self.done.is_set():
                break
        if not self.done.is_set():  # 購読が閉じた: 状態を確認して続ける（切断からの復帰）
            await self._catch_up_after_disconnect()

    async def _catch_up_after_disconnect(self) -> None:
        for _ in range(5):
            events = await self.core.sessions.list_events(self.rec.session_id)
            for ev in events:
                await self._on_event(ev)
            if self.done.is_set():
                return
            stream = await self.core.sessions.open_stream(self.rec.session_id)
            async for ev in stream:
                await self._on_event(ev)
                if self.done.is_set():
                    return
        await self._finish("failed", error="セッションの購読を再開できませんでした")

    # --- イベントの処理 ---
    async def _on_event(self, ev: dict[str, Any]) -> None:
        eid, typ = ev["id"], ev["type"]
        if eid in self.seen or self.done.is_set():
            return  # 確定したturnのイベント（失敗後の中断の結果など）で、表示を上書きしない
        self.seen.add(eid)
        try:
            await self._handle(ev, typ)
        finally:
            self.rec.last_event_id = eid
            self._persist()

    async def _handle(self, ev: dict[str, Any], typ: str) -> None:
        st = self.st
        if typ == "user.message":
            st.echoed += 1
            self.st.preparing = False
            self.core.on_user_message(self.rec.session_id, text_of(ev.get("content")))
        elif typ == "agent.message":
            await self.core.show(self.conv, self.rec.session_id, text_of(ev.get("content")))
        elif typ in TOOL_NAMES:
            await self._on_tool_use(ev)
        elif typ in RESULT_NAMES:
            await self._on_tool_result(ev)
        elif typ == "user.tool_confirmation":
            if ev.get("tool_use_id") not in st.decided:
                st.decided.append(ev["tool_use_id"])
        elif typ == "session.status_running":
            if not st.interrupting and not st.pending_tools:
                self._set_status("thinking")
        elif typ == "session.error":
            err = ev.get("error") or {}
            retry = (err.get("retry_status") or {}).get("type")
            if retry in ("exhausted", "terminal"):
                st.error = err.get("message") or err.get("type", "error")
        elif typ == "session.status_terminated":
            await self._finish("failed", error=st.error or "セッションが終了しました")
        elif typ == "session.status_idle":
            await self._on_idle(ev)
        elif typ == "user.interrupt":
            st.interrupting = True
            self._set_status("interrupting")
        else:
            return
        await self.flush()

    async def _on_tool_use(self, ev: dict[str, Any]) -> None:
        st = self.st
        name = ev.get("name", "tool")
        summary = await self.core.safe_text(tool_summary(name, ev.get("input") or {}))
        bg = ev.get("type") == "agent.custom_tool_use" and name in BG_TOOL_NAMES
        st.tools.append(
            {"id": ev["id"], "name": name, "summary": summary, "status": "running", "bg": bg}
        )
        st.pending_tools.append(ev["id"])
        self._set_status("running_tool")
        self.core.emit("tool_use", self.rec.session_id, name=name, tool_input=ev.get("input"))
        if ev.get("evaluated_permission") == "ask" and not self.replaying:
            await self._decide(ev)

    async def _decide(self, ev: dict[str, Any]) -> None:
        """bashの実行前確認。一括取り出しは拒否し、それ以外は許可する。"""
        st, sid = self.st, self.rec.session_id
        if ev["id"] in st.decided:
            return
        command = str((ev.get("input") or {}).get("command", ""))
        if ev.get("name") == "bash" and self.core.check_bash:
            v = self.core.check_bash(command)
            allow, reason = v.allow, v.reason
        else:
            allow, reason = True, ""
        st.decided.append(ev["id"])
        self.core.emit("bash_approval", sid, allow=allow, reason=reason, command=command)
        if not allow:
            for t in st.tools:
                if t["id"] == ev["id"]:
                    t["status"] = "denied"
                    t["summary"] = (t["summary"] + f"（拒否: {reason}）").strip()
        await self.core.sessions.confirm_tool(sid, ev["id"], allow, None if allow else reason)

    async def _on_tool_result(self, ev: dict[str, Any]) -> None:
        st = self.st
        tid = ev.get("tool_use_id") or ev.get("mcp_tool_use_id") or ev.get("custom_tool_use_id")
        text = text_of(ev.get("content"))
        bg_task = BACKGROUND_RE.search(text) if text else None
        for t in st.tools:
            if t["id"] == tid and t["status"] == "running":
                if ev.get("is_error") and st.interrupting:
                    t["status"] = "interrupted"  # ユーザの操作（停止・追加指示）による中断
                elif bg_task and t.get("bg") and t["name"] == "run":
                    t["status"] = "background"  # 待ち時間を超えて、バックグラウンドへ回った
                    t["task"] = bg_task.group(1)
                else:
                    t["status"] = "failure" if ev.get("is_error") else "success"
        self._track_background(text, bg_task.group(1) if bg_task else None)
        if tid in st.pending_tools:
            st.pending_tools.remove(tid)
        self.core.emit(
            "tool_result", self.rec.session_id, tool_use_id=tid, error=ev.get("is_error")
        )
        if not st.pending_tools:
            self._set_status("thinking" if not st.interrupting else "interrupting")
            await self._flush_buffer()

    def _track_background(self, text: str, started: str | None) -> None:
        """バックグラウンドのタスクの開始と終了を、ツール結果の定型の文から数える。"""
        st = self.st
        if started and started not in st.bg_open:
            st.bg_open.append(started)
        for rx in FINISHED_RES:
            for m in rx.finditer(text):
                tid = m.group(1)
                if tid in st.bg_open:
                    st.bg_open.remove(tid)
                for t in st.tools:
                    if t.get("task") == tid and t["status"] == "background":
                        t["status"] = "success"

    async def _flush_buffer(self) -> None:
        """ツール完了後に、保持していた追加指示を送る（モデル生成中なのでキューされる）。"""
        st = self.st
        while st.buffer and not st.interrupting:
            text = st.buffer[0]
            try:
                await self.core.sessions.send_message(self.rec.session_id, text)
            except SessionBusy:
                return  # 次のツール完了/idleでやり直す
            st.buffer.pop(0)
            st.sent += 1

    async def _on_idle(self, ev: dict[str, Any]) -> None:
        st = self.st
        sr = ev.get("stop_reason") or {}
        t = sr.get("type")
        if t == "requires_action":
            return  # ツール実行中/承認待ち。turnは続いている
        if t == "end_turn" and st.interrupting:
            if st.buffer:  # 追加指示のための中断: 保持していた指示を新しいturnとして送る
                st.interrupting = False
                self._set_status("thinking")
                await self._flush_buffer()
                return
            await self._finish("interrupted")
            return
        if st.buffer and not st.interrupting:
            await self._flush_buffer()
            if st.buffer:
                return
        if t == "end_turn":
            if st.sent > st.echoed:
                self._set_status("thinking")  # キューされた追加指示が続けて処理される
            else:
                await self._finish("finished")
        else:
            await self._finish("failed", error=st.error or f"セッションが停止しました（{t}）")

    # --- 追加指示・停止 ---
    async def append(self, text: str, *, notice: bool = False) -> None:
        """実行中の追加指示（`notice`: ユーザの指示ではなく、Botの自動通知）。

        モデル生成中ならそのまま送る（キューされ、現在のturnの後に処理される）。
        ツール実行・確認待ちの間は送れないので、`user.interrupt`でツールを中断し、
        turnが終わった直後に送る（スパイク1.3。ツールを止めずに指示を届ける方法はない）。
        ただし、実行中のツールがすべてバックグラウンド実行のツール（最長60秒で返る）なら、
        中断せずに保持し、結果の後に送る。
        """
        st = self.st
        if not notice:
            st.appended += 1
        if st.interrupting:  # 中断の処理中: 終わってから送る
            st.buffer.append(text)
        elif st.pending_tools and self._only_bg_tools_pending():
            st.buffer.append(text)  # ツールの結果の後に`_flush_buffer()`で送る
        elif st.pending_tools:
            st.buffer.append(text)
            st.interrupting = True
            self._set_status("interrupting")
            self._persist()
            await self.flush(force=True)
            await self.core.sessions.interrupt(self.rec.session_id)
        else:
            try:
                await self.core.sessions.send_message(self.rec.session_id, text)
                st.sent += 1
            except SessionBusy:
                st.buffer.append(text)
                st.interrupting = True
                self._set_status("interrupting")
                await self.core.sessions.interrupt(self.rec.session_id)
        self._persist()
        await self.flush(force=True)

    def _only_bg_tools_pending(self) -> bool:
        running = {t["id"]: t for t in self.st.tools}
        return all(running.get(i, {}).get("bg") for i in self.st.pending_tools)

    async def stop(self) -> None:
        st = self.st
        st.interrupting, st.buffer = True, []
        self._set_status("interrupting")
        self._persist()
        await self.flush(force=True)
        await self.core.sessions.interrupt(self.rec.session_id)

    # --- 終了 ---
    async def _finish(self, kind: str, error: str = "") -> None:
        if self.done.is_set():
            return
        self.final = kind
        self.rec.status = kind
        self._set_status(kind)
        self.st.tools = [
            {**t, "status": "interrupted" if kind == "interrupted" else "failure"}
            if t["status"] == "running" and kind != "finished"
            else t
            for t in self.st.tools
        ]
        self._persist()
        self.core.sync_bg_watch(self.conv.key, self.rec.session_id, self.st.bg_open)
        await self.flush(force=True)
        self.core.emit("turn_end", self.rec.session_id, result=kind, error=error)
        if kind == "failed":
            await self.core.notify_failure(self.conv, self.rec.session_id, error)
        self.done.set()
        self.core.runners.pop(self.conv.key, None)

    # --- 定期処理（1分ごとの更新、Sandboxの準備中の表示） ---
    async def _tick(self) -> None:
        s = self.core.s
        while not self.done.is_set():
            await self.core.clock.sleep(s.tick_seconds)
            now = self.core.clock.now()
            if self.dirty and now - self.last_update >= s.min_update_interval:
                await self.flush(force=True)
            elif now - self.last_update >= s.heartbeat_seconds:
                await self.flush(force=True)
            if self.core.sandbox_state and now - self.last_poll >= s.sandbox_poll_seconds:
                self.last_poll = now
                await self._poll_sandbox()

    async def _poll_sandbox(self) -> None:
        assert self.core.sandbox_state
        try:
            status = _status(await self.core.sandbox_state(self.rec.session_id))
        except Exception:
            return
        if status is None or self.done.is_set():
            return
        # Sandboxの起動に失敗した（過去のturnの`failed`は時刻で除く）。セッションはツールの
        # 結果待ちのまま残るので、中断して次の依頼を受けられる状態に戻し、turnを失敗にする。
        if status.state == "failed" and status.at >= self.rec.started_at - 1:
            with contextlib.suppress(Exception):
                await self.core.sessions.interrupt(self.rec.session_id)
            await self._finish("failed", error=status.error or "作業環境の起動に失敗しました")
            return
        starting = status.state == "starting"
        if starting and self.view.status in ("thinking", "running_tool", "preparing_sandbox"):
            if self.view.status != "preparing_sandbox":
                self._set_status("preparing_sandbox")
                await self.flush(force=True)
        elif not starting and self.view.status == "preparing_sandbox":
            self._set_status("running_tool" if self.st.pending_tools else "thinking")
            await self.flush(force=True)

    async def wait(self) -> None:
        if self._task:
            await self._task


class ConversationCore:
    def __init__(
        self,
        *,
        sessions: SessionsPort,
        store: BotStore,
        channel: ChatChannel,
        clock: Clock,
        settings: Settings | None = None,
        rules: RuleCheck | None = None,
        input_guard: InputGuardFn | None = None,
        inspect: InspectFn | None = None,
        check_bash: BashCheckFn | None = None,
        emit_hook: HookEmit | None = None,
        sandbox_state: SandboxState | None = None,
        register_user: RegisterUser | None = None,
    ) -> None:
        self.sessions, self.store, self.channel, self.clock = sessions, store, channel, clock
        self.s = settings or Settings()
        self.rules, self.input_guard, self.inspect = rules, input_guard, inspect
        self.check_bash, self._emit_hook = check_bash, emit_hook
        self.sandbox_state, self.register_user = sandbox_state, register_user
        self.runners: dict[str, Turn] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._history: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.s.history_for_guard)
        )
        self._stop_words = frozenset(normalize(w) for w in self.s.stop_words)

    # --- フック ---
    def emit(self, kind: str, session_id: str | None, **data: Any) -> None:
        if self._emit_hook:
            try:
                self._emit_hook(kind, session_id, data)
            except Exception:
                log.exception("フックへの引き渡しに失敗")

    # --- 表示（出力の検査を通す） ---
    async def safe_text(self, text: str) -> str:
        """進捗メッセージに出すテキスト。検査で表示できなければ伏せる。"""
        if not text or not self.inspect:
            return text
        try:
            r = await self.inspect(text)
        except Exception:
            return OUTPUT_HIDDEN
        if r.action == "show":
            return text
        if r.action == "masked" and r.text:
            return r.text
        return OUTPUT_HIDDEN

    async def show(self, conv: ConvRef, session_id: str, text: str) -> None:
        """エージェントの返答を、検査してからチャネルへ投稿する。セッションは書き換えない。"""
        if not text.strip():
            return
        shown, result = text, "show"
        if self.inspect:
            try:
                r = await self.inspect(text)
            except Exception:
                r = None
            if r is None:
                shown, result = "出力を検査できなかったため、表示できません。", "failed"
            elif r.action == "show":
                pass
            elif r.action == "masked" and r.text:
                shown, result = r.text, "masked"
            else:
                shown = r.message or "ポリシーにより、この出力は表示できません。"
                result = r.action
        self.emit("agent_message", session_id, text=text, inspection=result)
        await self.channel.post(conv, shown)

    async def notify_failure(self, conv: ConvRef, session_id: str, error: str) -> None:
        status = None
        if self.sandbox_state:
            with contextlib.suppress(Exception):
                status = _status(await self.sandbox_state(session_id))
        text = SANDBOX_FAILED if status and status.state == "failed" else GENERIC_FAILED
        self.emit("notice", session_id, text=text, error=error)
        await self.channel.post(conv, text)

    # --- 入力 ---
    async def handle(self, msg: Inbound) -> None:
        key = msg.conv.key
        # フィルタ（応答範囲の制限）。除外した投稿には返信しない。
        if self.rules:
            allowed, reply = await self.rules(msg)
            if not allowed:
                self.emit("user_input", None, user=msg.user, decision="excluded", text=msg.text)
                if reply:
                    await self.channel.post(msg.conv, reply)
                return
        async with self._locks[key]:
            await self._handle_locked(msg)

    async def _handle_locked(self, msg: Inbound) -> None:
        key, text = msg.conv.key, msg.text.strip()
        if not text:
            return
        if normalize(text) in self._stop_words:  # 停止語は入力ガードの対象にしない
            self.emit("user_input", self.store.get_session(key), user=msg.user, decision="stop")
            await self._stop(msg.conv)
            return
        run = self.runners.get(key)
        # 新しいturnを始めるときは、ガードや セッション作成の前に、すぐ受け付けたことを返す
        ack_ref = ""
        if run is None or run.done.is_set():
            ack_ref = await self._ack(msg.conv)
        if self.input_guard:
            try:
                v = await self.input_guard(text, list(self._history[key]))
            except Exception:
                log.exception("入力ガードの呼び出しに失敗")
                self.emit("user_input", None, user=msg.user, text=text, decision="error")
                await self._reject(msg.conv, ack_ref, INPUT_CHECK_FAILED)
                return
            if v.decision != "allow":
                self.emit(
                    "user_input",
                    None,
                    user=msg.user,
                    text=text,
                    decision=v.decision,
                    reason=v.reason,
                )
                await self._reject(msg.conv, ack_ref, v.reason)
                return
        self._history[key].append(text)
        if run is not None and not run.done.is_set():
            self.emit("user_input", run.rec.session_id, user=msg.user, text=text, decision="append")
            await run.append(text)
            return
        sid = self.store.get_session(key)
        if sid is None:
            sid = await self.sessions.create({"slack_conv": key, "slack_user": msg.user})
            if self.register_user and msg.email:
                await self.register_user(sid, msg.email)  # Sandboxの作成時のenvになる
            self.store.save_thread(key, sid, msg.email, self.clock.now())
        self.emit("user_input", sid, user=msg.user, text=text, decision="allow")
        self._start_turn(msg.conv, sid, text, ack_ref)

    def _start_turn(self, conv: ConvRef, sid: str, text: str, ack_ref: str = "") -> Turn:
        rec = TurnRecord(
            turn_id=uuid.uuid4().hex,
            conv_key=conv.key,
            session_id=sid,
            status=RUNNING,
            progress_ref=ack_ref,  # 受付のメッセージを、そのまま進捗メッセージとして更新する
            started_at=self.clock.now(),
            state={"first_text": text},  # 送信前に落ちても、復帰時に再送できる
        )
        turn = Turn(self, rec, conv)
        self.runners[conv.key] = turn
        self.store.save_turn(rec)
        turn.start(text)
        return turn

    async def _ack(self, conv: ConvRef) -> str:
        try:
            return await self.channel.post_progress(conv, ACK_TEXT)
        except Exception:
            log.exception("受付のメッセージを投稿できません")
            return ""

    async def _reject(self, conv: ConvRef, ack_ref: str, reason: str) -> None:
        """処理しなかった入力: 受付のメッセージを更新し、理由は通常の返信で伝える。"""
        if ack_ref:
            with contextlib.suppress(Exception):
                await self.channel.update_progress(conv, ack_ref, ACK_REJECTED)
        await self.channel.post(conv, reason)

    async def _stop(self, conv: ConvRef) -> None:
        run = self.runners.get(conv.key)
        if run is None or run.done.is_set():
            await self.channel.post(conv, NOTHING_RUNNING)
            return
        await run.stop()

    # --- バックグラウンドのタスクの終了の通知 ---
    def bg_tasks_of(self, conv_key: str) -> list[str]:
        w = self.store.get_watch(conv_key)
        return list(w["tasks"]) if w else []

    def sync_bg_watch(self, conv_key: str, session_id: str, open_tasks: list[str]) -> None:
        """turnの終了時: バックグラウンドで動いているタスクが残っていれば、終了の通知を待つ。"""
        if not open_tasks:
            self.store.delete_watch(conv_key)
            return
        current = self.store.get_watch(conv_key)
        since = current["since"] if current else self.clock.now()  # 監視の開始は動かさない
        self.store.set_watch(conv_key, session_id, since, list(open_tasks))

    async def poll_background(self) -> int:
        """ConfigMapを読み、バックグラウンドのタスクの終了をセッションへ知らせる。知らせた数を返す。"""
        if not self.sandbox_state:
            return 0
        n = await self._deliver_pending()  # 前回、送れなかった通知
        try:
            watches = self.store.list_watches()
        except Exception:
            log.exception("バックグラウンドの監視の読み出しに失敗（次のpollで読み直します）")
            return n
        for w in watches:
            try:
                if await self._detect_finished(w):
                    n += await self._deliver_pending()
            except Exception:
                log.exception("バックグラウンドのタスクの通知に失敗")
        return n

    async def _detect_finished(self, w: dict[str, Any]) -> bool:
        """終了の報告を見つけたら、通知（未送信）を永続化する。turnの実行中でも読む。"""
        assert self.sandbox_state
        key, sid = w["conv_key"], w["session_id"]
        status = _status(await self.sandbox_state(sid))
        if status is None:
            if self.clock.now() - w["since"] > BG_WATCH_TTL:
                self.store.delete_watch(key)
            return False
        fresh = [t for t in status.finished_tasks if not self.store.is_notified(sid, t["id"])]
        count_key = f"count@{status.finished_count_at}"
        count = 0
        if status.finished_count and not self.store.is_notified(sid, count_key):
            count = status.finished_count
        if not fresh and not count:
            # ワーカーが終了して報告するものがなければ、監視を終える（伝え済み・消失）
            if status.state in ("stopped", "failed") and status.at >= w["since"]:
                self.store.delete_watch(key)
            return False
        done_ids = {t["id"] for t in fresh}
        remaining = [] if count else [t for t in w["tasks"] if t not in done_ids]
        text = bg_notice_text(fresh, count)
        ids = [t["id"] for t in fresh] + ([count_key] if count else [])
        # 通知の本文・通知済み・監視の更新を一度に記録する。送信に失敗しても、次のpollで再送する
        self.store.add_notice(key, sid, ids, text, remaining, w["since"])
        run = self.runners.get(key)
        if run is not None and not run.done.is_set():
            run.st.bg_open = [t for t in run.st.bg_open if t not in done_ids]
        self.emit("bg_notice", sid, text=text, tasks=sorted(done_ids), count=count)
        return True

    async def _deliver_pending(self) -> int:
        """未送信の通知を送る（turnの実行中は追加の指示、なければ新しいturn）。"""
        sent = 0
        try:
            pending = self.store.list_pending()
        except Exception:
            log.exception("保留中の通知の読み出しに失敗（次のpollで読み直します）")
            return 0
        for p in pending:
            key, sid = p["conv_key"], p["session_id"]
            try:
                async with self._locks[key]:
                    if await self._deliver_one(p, key, sid):
                        sent += 1
            except Exception:
                log.exception("自動通知の送信に失敗（次のpollで再送します）")
        return sent

    async def _deliver_one(self, p: dict[str, Any], key: str, sid: str) -> bool:
        """通知を、セッションに届くまで保留に残す。届いた証拠は、セッションの`user.message`の
        イベントを見たとき（`on_user_message`）。送る役のturnが実行中の間は、そのturnに任せる。
        そのturnが送れずに終わった（失敗・中断・復帰できなかった）なら、もう一度送る。"""
        if p["turn_id"]:
            rec = self.store.get_turn(p["turn_id"])
            if rec is not None and rec.status == RUNNING:
                return False
        run = self.runners.get(key)
        if run is not None and not run.done.is_set():
            # 追加の指示と同じ扱い。先にturnを記録し、保持した後の失敗で二重に送らない
            self.store.set_pending_turn(p["id"], run.rec.turn_id)
            try:
                await run.append(p["text"], notice=True)
            except Exception:
                # 送れず、turnのbufferにも入らなかった。担当を外して、次のpollで再送する
                self.store.set_pending_turn(p["id"], "")
                raise
        else:
            turn = self._start_turn(ConvRef.parse(key), sid, p["text"])
            self.store.set_pending_turn(p["id"], turn.rec.turn_id)
        return True

    def on_user_message(self, session_id: str, text: str) -> None:
        """セッションに届いた`user.message`。自動通知なら、保留を消す（届いた）。"""
        if text.startswith(BG_NOTICE_PREFIX):
            self.store.delete_pending_by_text(session_id, text)

    async def run_background_watch(self) -> None:
        """Botの常駐タスク: 定期的に、バックグラウンドのタスクの終了を確かめる。

        このループが1回の例外で抜けると、Botを再起動するまで通知が一切届かなくなる
        （呼び出し側は`await`しないので、例外はログにも出ない）。失敗は記録して次のpollへ進む。
        """
        while True:
            await self.clock.sleep(self.s.bg_poll_seconds)
            try:
                await self.poll_background()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("バックグラウンドのタスクの監視に失敗（次のpollで続けます）")

    # --- 再起動からの復帰 ---
    async def recover(self) -> int:
        n = 0
        for rec in self.store.running_turns():
            conv = ConvRef.parse(rec.conv_key)
            turn = Turn(self, rec, conv)
            self.runners[rec.conv_key] = turn
            turn.resume()
            n += 1
        return n

    async def shutdown(self) -> None:
        """終了時: turnの追跡を止める（turn自体はAnthropic側で続く。次の起動で復帰する）。"""
        tasks = [t._task for t in self.runners.values() if t._task]
        for t in self.runners.values():
            if t._ticker:
                t._ticker.cancel()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """テスト用: 実行中のturnがすべて終わるのを待つ。"""
        deadline = time.monotonic() + timeout
        while self.runners and time.monotonic() < deadline:
            await asyncio.sleep(0.005)


__all__ = [
    "ConversationCore",
    "Settings",
    "SandboxStatus",
    "Turn",
    "tool_summary",
    "normalize",
    "Iterable",
]
