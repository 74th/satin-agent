"""(A)のワーカーが提供する、待ち時間つきのコマンド実行とバックグラウンドのタスク。

- `run`: コマンドを別プロセスで起動し、待ち時間内に終われば結果を返す。終わらなければ**止めずに**
  タスクIDを返し、バックグラウンドで動かし続ける。
- `task_status` / `task_wait` / `task_stop`: タスクの確認・待機・停止。

タスクの記録は`<workspace>/.satin/tasks/<id>/`（`meta.json`・`output.log`）に置く。PVCにあるので、
Sandboxが停止して再開しても読める。1つのSandboxは1つのセッションだけを処理する。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

WAIT_MAX = 60.0  # `run`の待ち時間と`task_wait`の上限（Botの追加指示の待ちと対）
WAIT_MIN = 1.0
RUN_TAIL_BYTES = 100 * 1024
STATUS_TAIL_BYTES = 8 * 1024
# `task_status`（IDなし）の上限。`_recover()`がPVCに残る過去のタスクも読むので、
# 全件を返すとツールの結果が数万トークンになりうる。
STATUS_ALL_MAX_TASKS = 10
STATUS_ALL_BUDGET = 8 * 1024
KILL_GRACE = 10.0
DEFAULT_MAX_TASKS = 4
DEFAULT_MAX_SECONDS = 7200.0
DEFAULT_WAIT = 30.0

RUNNING, EXITED, STOPPED, TIMEOUT, LOST = "running", "exited", "stopped", "timeout", "lost"
STATE_LABEL = {
    RUNNING: "実行中",
    EXITED: "終了",
    STOPPED: "停止",
    TIMEOUT: "上限超過",
    LOST: "消失",
}

# `run`が待ち時間を超えたときの結果の定型の文。Botがタスクの表示のためにIDを読む。
BACKGROUND_PREFIX = "[satin] バックグラウンドで実行中:"
BACKGROUND_RE = re.compile(r"\[satin\] バックグラウンドで実行中: (t-[0-9]+-[0-9a-f]+)")
# 終わったことを伝える結果の文（Botが、残りのタスクの数を数えるために読む）
_TASK_ID = r"(t-[0-9]+-[0-9a-f]+)"
FINISHED_RES = (
    re.compile(r"バックグラウンドのタスク " + _TASK_ID + r" が"),  # ツール結果の末尾の知らせ
    re.compile(_TASK_ID + r": (?:終了|停止|上限超過|消失)"),  # task_status・task_waitの先頭の行
)

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "run",
        "description": (
            "コマンドを別のプロセスで実行する。待ち時間（既定30秒、最大60秒）以内に終われば、出力と終了コードを返す。"
            "終わらなければ止めずにバックグラウンドへ回し、タスクIDを返す。"
            "`cd`・`export`などシェルの状態を変える操作以外のコマンドは、bashではなくこのツールを使う。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "bashで実行するコマンド"},
                "cwd": {
                    "type": "string",
                    "description": "作業ディレクトリ（ワークスペース内）。既定は/workspace",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "待つ秒数（1〜60、既定30）。超えるとバックグラウンドへ回る",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "task_status",
        "description": (
            "バックグラウンドのタスクの状態（実行中・終了・停止・上限超過・消失）、終了コード、経過時間、"
            "出力の末尾を返す。task_idを省略すると、すべてのタスクを返す。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "タスクID（省略可）"}},
        },
    },
    {
        "name": "task_wait",
        "description": (
            "タスクが終わるか、指定した秒数（最大60）が過ぎるまで待ち、task_statusと同じ内容を返す。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "タスクID"},
                "seconds": {"type": "number", "description": "待つ秒数（1〜60、既定60）"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_stop",
        "description": "タスクのプロセスと、その子プロセスをすべて止める。",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "タスクID"}},
            "required": ["task_id"],
        },
    },
]


class TaskError(Exception):
    """ツールの結果をエラーにする。"""


@dataclass
class Task:
    id: str
    command: str
    cwd: str
    started_at: float
    state: str = RUNNING
    pid: int | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    notified: bool = False
    leftover: bool = False  # シェルの終了時に残っていた子プロセスを止めた


def _num(env: Mapping[str, str], name: str, default: float, low: float, high: float = 0.0) -> float:
    """環境変数の数値。読めない値・範囲外は既定へ寄せる（設定のタイプミスでセッションを失敗させない）。

    `from_env()`はツール一覧を作るたびに呼ばれるので、ここで例外を投げると
    オーケストレータが再実行を使い切って(A)が止まる。
    """
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(
            f"satin-sandbox: {name}={raw!r} は数値として読めません。既定の{default}を使います",
            file=sys.stderr,
            flush=True,
        )
        return default
    bounded = max(value, low) if high <= 0 else min(max(value, low), high)
    if bounded != value:
        print(
            f"satin-sandbox: {name}={raw!r} は範囲外です。{bounded}を使います",
            file=sys.stderr,
            flush=True,
        )
    return bounded


@dataclass
class BgConfig:
    max_tasks: int = DEFAULT_MAX_TASKS
    max_seconds: float = DEFAULT_MAX_SECONDS
    default_wait: float = DEFAULT_WAIT

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BgConfig:
        return cls(
            max_tasks=int(_num(env, "SATIN_BG_MAX_TASKS", DEFAULT_MAX_TASKS, 1.0)),
            max_seconds=_num(env, "SATIN_BG_MAX_SECONDS", DEFAULT_MAX_SECONDS, 1.0),
            default_wait=_num(env, "SATIN_BG_DEFAULT_WAIT", DEFAULT_WAIT, WAIT_MIN, WAIT_MAX),
        )


def _default_env() -> dict[str, str]:
    from anthropic.lib.tools.agent_toolset import _default_bash_env

    return _default_bash_env()


def _tail(path: Path, limit: int) -> tuple[str, bool]:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return "", False
    return data.decode("utf-8", errors="replace"), size > limit


LEFTOVER_NOTE = "（シェルの終了時に残っていた子プロセスを停止しました）"


def _group_members(pgid: int) -> list[int]:
    """プロセスグループの、ゾンビではないプロセス。"""
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for name in entries:
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_text()
            state, _, pgrp = stat[stat.rindex(")") + 2 :].split()[:3]
        except (OSError, ValueError):
            continue
        if int(pgrp) == pgid and state != "Z":
            out.append(int(name))
    return out


def _fmt_seconds(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}分{sec % 60}秒" if sec >= 60 else f"{sec}秒"


@dataclass
class TaskManager:
    root: Path  # <workspace>/.satin/tasks
    resolve_cwd: Callable[[str], Path]
    config: BgConfig = field(default_factory=BgConfig)
    hold: Callable[[], None] = lambda: None
    release: Callable[[], None] = lambda: None
    env_factory: Callable[[], dict[str, str]] = _default_env
    clock: Callable[[], float] = time.time
    _tasks: dict[str, Task] = field(default_factory=dict)
    _procs: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)
    _done: dict[str, asyncio.Event] = field(default_factory=dict)
    _reason: dict[str, str] = field(default_factory=dict)  # 止めた理由（停止・消失）
    _watchers: set[asyncio.Task[None]] = field(default_factory=set)
    _seq: int = 0

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover()

    # --- 記録 ---
    def _dir(self, task_id: str) -> Path:
        return self.root / task_id

    def _save(self, t: Task) -> None:
        d = self._dir(t.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(asdict(t), ensure_ascii=False))
        tmp.replace(d / "meta.json")

    def _recover(self) -> None:
        """前のSandboxの記録を読む。実行中のまま残っているものは消失にする。"""
        for meta in sorted(self.root.glob("t-*/meta.json")):
            try:
                t = Task(**json.loads(meta.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if t.state == RUNNING:
                t.state, t.ended_at = LOST, self.clock()
                self._save(t)
            self._tasks[t.id] = t
            m = re.match(r"t-(\d+)-", t.id)
            if m:
                self._seq = max(self._seq, int(m.group(1)))

    # --- 起動 ---
    def running(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.state == RUNNING]

    async def start(self, command: str, cwd: str | None = None) -> Task:
        if len(self.running()) >= self.config.max_tasks:
            raise TaskError(
                f"同時に実行できるバックグラウンドのタスクは{self.config.max_tasks}件までです。"
                "task_statusで確認し、不要なものはtask_stopで止めてください"
            )
        try:
            workdir = self.resolve_cwd(cwd or "/workspace")
        except ValueError as e:
            raise TaskError(str(e)) from e
        if not workdir.is_dir():
            raise TaskError(f"作業ディレクトリ {cwd!r} がありません")
        self._seq += 1
        t = Task(f"t-{self._seq}-{uuid.uuid4().hex[:4]}", command, str(workdir), self.clock())
        d = self._dir(t.id)
        d.mkdir(parents=True)
        self.hold()  # 時計が登録されていなければ、ここで失敗して起動しない
        try:
            with (d / "output.log").open("wb") as log:
                proc = await asyncio.create_subprocess_exec(
                    "/bin/bash",
                    "-c",
                    command,
                    cwd=str(workdir),
                    env=self.env_factory(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
        except BaseException:
            self.release()
            raise
        t.pid = proc.pid
        self._tasks[t.id] = t
        self._procs[t.id] = proc
        self._done[t.id] = asyncio.Event()
        self._save(t)
        w = asyncio.ensure_future(self._watch(t, proc))
        self._watchers.add(w)
        w.add_done_callback(self._watchers.discard)
        return t

    async def _watch(self, t: Task, proc: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.config.max_seconds)
        except TimeoutError:
            self._reason.setdefault(t.id, TIMEOUT)
            await self._kill(proc)
        else:
            # シェルが先に終わっても、同じプロセスグループの子が残れる。残すと、停止・上限時間・
            # 同時実行数の管理から外れるので、タスクを確定する前に止める。
            if _group_members(proc.pid):
                t.leftover = True
                await self._reap_group(proc.pid)
        if t.state == RUNNING:
            self._finish(t, self._reason.get(t.id, EXITED), proc.returncode)

    async def _reap_group(self, pgid: int) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, sig)
            for _ in range(int(KILL_GRACE * 10)):
                if not _group_members(pgid):
                    return
                await asyncio.sleep(0.1)

    def _finish(self, t: Task, state: str, code: int | None) -> None:
        t.state, t.exit_code, t.ended_at = state, code, self.clock()
        self._save(t)
        self._procs.pop(t.id, None)
        self._done[t.id].set()
        self.release()

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """プロセスグループごと止める（SIGTERM、猶予の後にSIGKILL）。"""
        for sig, grace in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, KILL_GRACE)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
                break
            except TimeoutError:
                continue
        # リーダーが先に終わっても、子が残っていれば止める
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # --- 確認 ---
    def get(self, task_id: str) -> Task:
        t = self._tasks.get(task_id)
        if t is None:
            raise TaskError(f"タスク {task_id} はありません")
        return t

    async def wait(self, task_id: str, seconds: float) -> Task:
        t = self.get(task_id)
        ev = self._done.get(task_id)
        if t.state == RUNNING and ev is not None:
            try:
                await asyncio.wait_for(ev.wait(), timeout=max(0.0, min(seconds, WAIT_MAX)))
            except TimeoutError:
                pass
        return t

    def output(self, t: Task, limit: int) -> str:
        text, truncated = _tail(self._dir(t.id) / "output.log", limit)
        if truncated:
            log = self._dir(t.id) / "output.log"
            text = f"（出力が長いため末尾だけを表示しています。全体: {log}）\n{text}"
        return text

    def describe(self, t: Task, limit: int = STATUS_TAIL_BYTES) -> str:
        end = t.ended_at if t.ended_at is not None else self.clock()
        head = f"{t.id}: {STATE_LABEL[t.state]}"
        if t.exit_code is not None:
            head += f"（終了コード {t.exit_code}）"
        head += f" 経過 {_fmt_seconds(end - t.started_at)} `{t.command[:80]}`"
        if t.leftover:
            head += LEFTOVER_NOTE
        if t.state != RUNNING:
            t.notified = True
            self._save(t)
        out = self.output(t, limit)
        return f"{head}\n{out}" if out else head

    # --- 停止 ---
    async def stop(self, task_id: str) -> Task:
        t = self.get(task_id)
        proc = self._procs.get(task_id)
        if t.state == RUNNING and proc is not None:
            self._reason[t.id] = STOPPED
            await self._kill(proc)
            await self._done[t.id].wait()
        return t

    async def shutdown(self) -> None:
        """SIGTERMの猶予の間に、実行中のタスクを止めて消失として記録する。"""

        async def one(t: Task) -> None:
            proc = self._procs.get(t.id)
            self._reason[t.id] = LOST
            if proc is not None:
                await self._kill(proc)
                await self._done[t.id].wait()
            if t.state == RUNNING:
                self._finish(t, LOST, None)

        await asyncio.gather(*(one(t) for t in self.running()))  # 並列に止める

    # --- 終了の知らせ ---
    def unnotified(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.state != RUNNING and not t.notified]

    def take_notices(self) -> list[str]:
        """伝えていないまま終わったタスクの知らせ。伝えたものとして記録する。"""
        out = []
        for t in self.unnotified():
            t.notified = True
            self._save(t)
            code = f"（終了コード {t.exit_code}）" if t.exit_code is not None else ""
            out.append(
                f"[satin] バックグラウンドのタスク {t.id} が{STATE_LABEL[t.state]}しました{code}。"
                "task_statusで結果を確認できます。"
            )
        return out

    def report(self) -> list[dict[str, Any]]:
        """終了時の報告（伝えていないまま終わったタスク）。秘密は含まない。"""
        return [
            {
                "id": t.id,
                "command": t.command[:80],
                "state": t.state,
                "exit_code": t.exit_code,
            }
            for t in self.unnotified()
        ]

    # --- ツールの本体 ---
    async def run_tool(self, command: str, cwd: str | None, wait_seconds: float | None) -> str:
        wait = self.config.default_wait
        if wait_seconds is not None:
            wait = min(max(wait_seconds, WAIT_MIN), WAIT_MAX)
        t = await self.start(command, cwd)
        await self.wait(t.id, wait)
        if t.state == RUNNING:
            return (
                f"{BACKGROUND_PREFIX} {t.id}（{_fmt_seconds(wait)}以内に終わらなかったため、"
                f"止めずにバックグラウンドへ回しました）\n出力: {self._dir(t.id) / 'output.log'}\n"
                "task_status・task_wait・task_stopで確認・待機・停止できます。"
                "結果をすぐに必要としないなら、turnを終えてかまいません。"
            )
        t.notified = True
        self._save(t)
        out = self.output(t, RUN_TAIL_BYTES)
        if t.leftover:
            out = f"{out}\n{LEFTOVER_NOTE}" if out else LEFTOVER_NOTE
        if t.state == EXITED and t.exit_code == 0:
            return out
        label = f"終了コード {t.exit_code}" if t.state == EXITED else STATE_LABEL[t.state]
        raise TaskError(f"{out}\n[{label}]" if out else f"[{label}]")

    async def status_tool(self, task_id: str | None) -> str:
        if task_id:
            return self.describe(self.get(task_id))
        if not self._tasks:
            return "タスクはありません"
        # 実行中を先に、残りは新しい順。件数と合計の大きさに上限を置く
        tasks = sorted(self._tasks.values(), key=lambda t: (t.state != RUNNING, -t.started_at))
        parts: list[str] = []
        used = 0
        for t in tasks[:STATUS_ALL_MAX_TASKS]:
            if parts and used >= STATUS_ALL_BUDGET:
                break
            text = self.describe(t, STATUS_TAIL_BYTES // 4)
            parts.append(text)
            used += len(text)
        if omitted := len(tasks) - len(parts):
            parts.append(
                f"（ほかに{omitted}件のタスクがあります。"
                "task_statusにtask_idを指定すると、そのタスクを確認できます）"
            )
        return "\n\n".join(parts)

    async def wait_tool(self, task_id: str, seconds: float | None) -> str:
        t = await self.wait(task_id, WAIT_MAX if seconds is None else max(seconds, WAIT_MIN))
        return self.describe(t)

    async def stop_tool(self, task_id: str) -> str:
        return self.describe(await self.stop(task_id))
