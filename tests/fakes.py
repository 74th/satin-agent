"""外部依存の偽物。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from typing import Any

from satin_agent.ports import ConflictError, NotFoundError


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    async def sleep(self, seconds: float) -> None:
        self._now += seconds
        await asyncio.sleep(0)


class FakeObjectStore:
    def __init__(self, data: dict[str, bytes] | None = None) -> None:
        self.data: dict[str, bytes] = dict(data or {})
        self.write_log: list[str] = []

    def read(self, key: str) -> bytes:
        if key not in self.data:
            raise NotFoundError(key)
        return self.data[key]

    def write(self, key: str, data: bytes) -> None:
        self.data[key] = data
        self.write_log.append(key)

    def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def list(self) -> list[str]:
        return sorted(self.data)


class FakeKube:
    """名前空間1つ分のオブジェクトを保持する。resourceVersionで楽観ロックする。"""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self._rv = 0
        self._watchers: list[tuple[str, asyncio.Queue]] = []
        self.calls: list[tuple[str, str, str]] = []
        self.simulate_controller = True

    def _bump(self, obj: dict[str, Any]) -> None:
        self._rv += 1
        obj.setdefault("metadata", {})["resourceVersion"] = str(self._rv)

    def _emit(self, kind: str, typ: str, obj: dict[str, Any]) -> None:
        for k, q in self._watchers:
            if k == kind:
                q.put_nowait({"type": typ, "object": copy.deepcopy(obj)})

    async def get(self, kind: str, name: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self.objects[(kind, name)])
        except KeyError:
            raise NotFoundError(f"{kind}/{name}") from None

    async def list(self, kind: str, label_selector: str | None = None) -> list[dict[str, Any]]:
        want = dict(p.split("=", 1) for p in label_selector.split(",")) if label_selector else {}
        out = []
        for (k, _), obj in sorted(self.objects.items()):
            labels = obj.get("metadata", {}).get("labels", {})
            if k == kind and all(labels.get(a) == b for a, b in want.items()):
                out.append(copy.deepcopy(obj))
        return out

    async def create(self, kind: str, obj: dict[str, Any]) -> dict[str, Any]:
        name = obj["metadata"]["name"]
        self.calls.append(("create", kind, name))
        if (kind, name) in self.objects:
            raise ConflictError(f"{kind}/{name} exists")
        obj = copy.deepcopy(obj)
        obj["metadata"]["uid"] = f"uid-{len(self.objects) + 1}-{name}"
        self._controller(kind, obj)
        self._bump(obj)
        self.objects[(kind, name)] = obj
        self._emit(kind, "ADDED", obj)
        return copy.deepcopy(obj)

    async def replace(self, kind: str, obj: dict[str, Any]) -> dict[str, Any]:
        name = obj["metadata"]["name"]
        self.calls.append(("replace", kind, name))
        cur = self.objects.get((kind, name))
        if cur is None:
            raise NotFoundError(f"{kind}/{name}")
        rv = obj["metadata"].get("resourceVersion")
        if rv != cur["metadata"]["resourceVersion"]:
            raise ConflictError(f"{kind}/{name} resourceVersion mismatch")
        obj = copy.deepcopy(obj)
        obj["metadata"].setdefault("uid", cur["metadata"].get("uid"))
        self._controller(kind, obj)
        self._bump(obj)
        self.objects[(kind, name)] = obj
        self._emit(kind, "MODIFIED", obj)
        return copy.deepcopy(obj)

    def _controller(self, kind: str, obj: dict[str, Any]) -> None:
        """Agent Sandbox controllerの最小の模擬: operatingModeに応じてstatusを更新する。"""
        if kind != "sandbox" or not self.simulate_controller:
            return
        mode = obj.get("spec", {}).get("operatingMode")
        name = obj["metadata"]["name"]
        sel = f"sandbox={name}"
        obj.setdefault("status", {})["selector"] = sel
        if mode == "Suspended":
            obj["status"]["conditions"] = [{"type": "Suspended", "status": "True"}]
            self.objects.pop(("pod", name), None)
        elif mode == "Running":
            obj["status"]["conditions"] = [
                {"type": "Ready", "status": "False", "reason": "DependenciesNotReady"}
            ]
            self.objects[("pod", name)] = {
                "metadata": {"name": name, "labels": {"sandbox": name}},
                "status": {"phase": "Pending"},
            }

    # --- テスト用: Podの状態を進める ---
    async def pod_ready(self, name: str) -> None:
        await self.patch("pod", name, {"status": {"phase": "Running"}})
        await self.patch(
            "sandbox",
            name,
            {"status": {"conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]}},
        )

    async def pod_exit(self, name: str, code: int, message: str | None = None) -> None:
        state: dict[str, Any] = {"terminated": {"exitCode": code}}
        if message is not None:
            state["terminated"]["message"] = message
        await self.patch(
            "pod",
            name,
            {
                "status": {
                    "phase": "Succeeded" if code == 0 else "Failed",
                    "containerStatuses": [{"name": "satin", "state": state}],
                }
            },
        )
        reason = "PodSucceeded" if code == 0 else "PodFailed"
        await self.patch(
            "sandbox",
            name,
            {"status": {"conditions": [{"type": "Ready", "status": "False", "reason": reason}]}},
        )

    async def pod_waiting(self, name: str, reason: str) -> None:
        state = {"waiting": {"reason": reason}}
        await self.patch(
            "pod", name, {"status": {"containerStatuses": [{"name": "satin", "state": state}]}}
        )

    async def patch(self, kind: str, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("patch", kind, name))
        cur = self.objects.get((kind, name))
        if cur is None:
            raise NotFoundError(f"{kind}/{name}")
        _merge(cur, patch)
        if kind == "sandbox" and "operatingMode" in patch.get("spec", {}):
            self._controller(kind, cur)
        self._bump(cur)
        self._emit(kind, "MODIFIED", cur)
        return copy.deepcopy(cur)

    async def delete(self, kind: str, name: str) -> None:
        self.calls.append(("delete", kind, name))
        obj = self.objects.pop((kind, name), None)
        if obj is None:
            raise NotFoundError(f"{kind}/{name}")
        self._emit(kind, "DELETED", obj)

    async def watch(
        self, kind: str, label_selector: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue()
        self._watchers.append((kind, q))
        try:
            while True:
                ev = await q.get()
                if ev is None:  # 切断
                    return
                yield ev
        finally:
            self._watchers = [w for w in self._watchers if w[1] is not q]

    def disconnect_watchers(self) -> None:
        for _, q in self._watchers:
            q.put_nowait(None)


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


class FakeSlack:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._n = 0

    async def post(self, channel: str, thread_ts: str | None, text: str) -> str:
        self._n += 1
        ts = f"{self._n}.000"
        self.messages.append({"channel": channel, "thread_ts": thread_ts, "ts": ts, "text": text})
        return ts

    async def update(self, channel: str, ts: str, text: str) -> None:
        for m in self.messages:
            if m["ts"] == ts and m["channel"] == channel:
                m["text"] = text
                return
        raise KeyError(ts)

    async def upload(self, channel: str, thread_ts: str | None, filename: str, text: str) -> None:
        self.messages.append(
            {"channel": channel, "thread_ts": thread_ts, "file": filename, "text": text}
        )


class FakeAnthropic:
    def __init__(self) -> None:
        self.skills: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def create_skill(self, display_name: str, files: dict[str, bytes]) -> dict[str, Any]:
        self.calls.append("create_skill")
        sid = f"skill_{len(self.skills) + 1}"
        self.skills[sid] = {"name": display_name, "versions": [files]}
        return {"id": sid, "latest_version_id": "1"}

    def add_skill_version(self, skill_id: str, files: dict[str, bytes]) -> dict[str, Any]:
        self.calls.append("add_skill_version")
        self.skills[skill_id]["versions"].append(files)
        return {"id": str(len(self.skills[skill_id]["versions"]))}

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return {"id": agent_id, "version": self.agents[agent_id]["version"]}

    def create_agent(self, config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_agent")
        aid = f"agent_{len(self.agents) + 1}"
        self.agents[aid] = {"id": aid, "version": 1, "config": config}
        return {"id": aid, "version": 1}

    def update_agent(self, agent_id: str, version: int, config: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("update_agent")
        assert self.agents[agent_id]["version"] == version
        self.agents[agent_id].update(version=version + 1, config=config)
        return {"id": agent_id, "version": version + 1}


class FakeWorkSource:
    """work queueの偽物。putした順に1件ずつ返す。次を要求されるまで取り出さない。"""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.pulled: list[Any] = []
        self.stopped: list[str] = []
        self.completed: set[str] | None = None  # Noneなら、完了として扱う

    async def is_stopped(self, work_id: str) -> bool:
        return True if self.completed is None else work_id in self.completed

    async def items(self):
        while True:
            w = await self.q.get()
            self.pulled.append(w)
            yield w

    async def stop(self, work_id: str) -> None:
        self.stopped.append(work_id)
        if self.completed is not None:
            self.completed.add(work_id)  # 実物と同じく、停止するとwork itemは`stopped`になる


class ManualClock:
    """`advance()`したときだけ時間が進む時計。sleepは時刻に達するまで待つ。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start
        self._waiters: list[tuple[float, asyncio.Future]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append((self._now + seconds, fut))
        await fut

    async def advance(self, seconds: float) -> None:
        end = self._now + seconds
        while True:
            due = sorted((w for w in self._waiters if w[0] <= end), key=lambda w: w[0])
            if not due:
                break
            t, fut = due[0]
            self._waiters.remove((t, fut))
            self._now = max(self._now, t)
            if not fut.done():
                fut.set_result(None)
            for _ in range(5):
                await asyncio.sleep(0)
        self._now = end
        for _ in range(5):
            await asyncio.sleep(0)


class FakeSessions:
    """Managed Agentsのセッションの挙動の模擬（スパイク1.3で確かめた制約を含む）。

    - ツール実行・確認待ちの間は`send_message`が`SessionBusy`
    - モデル生成中の`send_message`はキューされ、end_turn後に別のturnとして処理される
    - ツール実行中のセッションは`idle(requires_action)`に見える
    """

    def __init__(self) -> None:
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.subs: dict[str, list[asyncio.Queue]] = {}
        self.running: dict[str, bool] = {}
        self.queue: dict[str, list[str]] = {}
        self.pending: dict[str, list[str]] = {}
        self.sent: list[tuple[str, str]] = []  # (session_id, text)
        self.confirmations: list[tuple[str, str, bool, str | None]] = []
        self.interrupts: list[str] = []
        self.created: list[dict[str, str]] = []
        self._n = 0

    # --- SessionsPort ---
    async def create(self, metadata: dict[str, str]) -> str:
        sid = f"sesn_{len(self.events) + 1}"
        self.events[sid], self.subs[sid] = [], []
        self.running[sid], self.queue[sid], self.pending[sid] = False, [], []
        self.created.append(metadata)
        return sid

    async def send_message(self, session_id: str, text: str) -> None:
        from satin_agent.slack.ports import SessionBusy

        if self.pending[session_id]:
            raise SessionBusy("waiting on responses to events")
        self.sent.append((session_id, text))
        if self.running[session_id]:
            self.queue[session_id].append(text)
        else:
            self._begin(session_id, text)

    async def interrupt(self, session_id: str) -> None:
        self.interrupts.append(session_id)
        for tid in list(self.pending[session_id]):
            self.pending[session_id].remove(tid)
            self._emit(
                session_id,
                "agent.tool_result",
                tool_use_id=tid,
                is_error=True,
                content=[{"type": "text", "text": "Tool execution was interrupted"}],
            )
        self._emit(session_id, "user.interrupt")
        self.queue[session_id].clear()
        self.running[session_id] = False
        self._emit(session_id, "session.status_idle", stop_reason={"type": "end_turn"})

    async def confirm_tool(self, session_id, tool_use_id, allow, reason=None) -> None:
        self.confirmations.append((session_id, tool_use_id, allow, reason))
        self._emit(
            session_id,
            "user.tool_confirmation",
            tool_use_id=tool_use_id,
            result="allow" if allow else "deny",
        )
        if not allow:
            self.pending[session_id].remove(tool_use_id)
            self._emit(
                session_id,
                "agent.tool_result",
                tool_use_id=tool_use_id,
                is_error=True,
                content=[{"type": "text", "text": reason or "denied"}],
            )
            self._emit(session_id, "session.status_running")

    async def open_stream(self, session_id: str):
        q: asyncio.Queue = asyncio.Queue()
        self.subs[session_id].append(q)

        async def gen():
            while True:
                ev = await q.get()
                if ev is None:
                    return
                yield ev

        return gen()

    async def list_events(self, session_id: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self.events[session_id]]

    async def last_event_id(self, session_id: str) -> str:
        evs = self.events[session_id]
        return evs[-1]["id"] if evs else ""

    async def status(self, session_id: str) -> dict[str, Any]:
        return {"status": "running" if self.running[session_id] else "idle", "stop_reason": None}

    # --- テストが「Anthropic側の動き」を進めるためのメソッド ---
    def _emit(self, sid: str, typ: str, **fields: Any) -> str:
        self._n += 1
        ev = {"id": f"e{self._n}", "type": typ, **fields}
        self.events[sid].append(ev)
        for q in self.subs[sid]:
            q.put_nowait(dict(ev))
        return ev["id"]

    def _begin(self, sid: str, text: str) -> None:
        self.running[sid] = True
        self._emit(sid, "user.message", content=[{"type": "text", "text": text}])
        self._emit(sid, "session.status_running")

    def say(self, sid: str, text: str) -> None:
        self._emit(sid, "agent.message", content=[{"type": "text", "text": text}])

    def tool_use(self, sid: str, name: str, inp: dict, ask: bool = False) -> str:
        tid = self._emit(
            sid,
            "agent.tool_use",
            name=name,
            input=inp,
            evaluated_permission="ask" if ask else "allow",
        )
        self.pending[sid].append(tid)
        self._emit(
            sid, "session.status_idle", stop_reason={"type": "requires_action", "event_ids": [tid]}
        )
        return tid

    def custom_tool_use(self, sid: str, name: str, inp: dict) -> str:
        """カスタムツール（Sandbox内のワーカーが実行する）。権限ポリシーは効かない。"""
        tid = self._emit(sid, "agent.custom_tool_use", name=name, input=inp)
        self.pending[sid].append(tid)
        self._emit(
            sid, "session.status_idle", stop_reason={"type": "requires_action", "event_ids": [tid]}
        )
        return tid

    def custom_tool_result(self, sid: str, tid: str, text: str = "ok", error: bool = False) -> None:
        if tid in self.pending[sid]:
            self.pending[sid].remove(tid)
        self._emit(
            sid,
            "user.custom_tool_result",
            custom_tool_use_id=tid,
            is_error=error,
            content=[{"type": "text", "text": text}],
        )
        self._emit(sid, "session.status_running")

    def tool_result(self, sid: str, tid: str, error: bool = False) -> None:
        if tid in self.pending[sid]:
            self.pending[sid].remove(tid)
        self._emit(
            sid,
            "agent.tool_result",
            tool_use_id=tid,
            is_error=error,
            content=[{"type": "text", "text": "ok"}],
        )
        self._emit(sid, "session.status_running")

    def end_turn(self, sid: str) -> None:
        self._emit(sid, "session.status_idle", stop_reason={"type": "end_turn"})
        if self.queue[sid]:
            self._begin(sid, self.queue[sid].pop(0))
        else:
            self.running[sid] = False

    def fail(self, sid: str, message: str = "boom") -> None:
        self._emit(
            sid,
            "session.error",
            error={
                "type": "model_request_failed",
                "message": message,
                "retry_status": {"type": "exhausted"},
            },
        )
        self._emit(sid, "session.status_idle", stop_reason={"type": "retries_exhausted"})
        self.running[sid] = False


class FakeChannel:
    def __init__(self) -> None:
        self.progress: dict[str, list[str]] = {}  # ref → 更新履歴
        self.posts: list[str] = []
        self.files: list[tuple[str, str]] = []
        self.updates = 0
        self._n = 0

    async def post_progress(self, conv, text: str) -> str:
        self._n += 1
        ref = f"p{self._n}"
        self.progress[ref] = [text]
        return ref

    async def update_progress(self, conv, ref: str, text: str) -> None:
        self.updates += 1
        self.progress[ref].append(text)

    async def post(self, conv, text: str) -> None:
        self.posts.append(text)

    async def post_file(self, conv, filename: str, text: str) -> None:
        self.files.append((filename, text))

    def last_progress(self) -> str:
        return list(self.progress.values())[-1][-1]
