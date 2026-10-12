"""(A) Managed Agentsのオーケストレータ。

work queueのwork itemごとに、そのセッションのSandboxを起動し、Sandbox内のワーカーに処理させる。
同時実行数の上限が埋まっている間は、次のwork itemを受け取らない（pollしない）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..ports import KubeApi
from ..sandbox.manager import (
    RunRequest,
    SandboxError,
    SandboxManager,
    SandboxStartError,
    is_terminated,
    operating_mode,
)
from ..sandbox.naming import ANN_SESSION_ID, LABEL_MODE
from .ports import WorkItem, WorkSource
from .states import StatePublisher
from .users import UserDirectory

log = logging.getLogger("satin.cma")


class CmaOrchestrator:
    def __init__(
        self,
        *,
        manager: SandboxManager,
        kube: KubeApi,
        source: WorkSource,
        users: UserDirectory | None,
        states: StatePublisher | None,
        environment_id: str,
        max_concurrency: int = 5,
        max_restarts: int = 2,
    ) -> None:
        self.mgr, self.kube, self.source = manager, kube, source
        self.users, self.states = users, states
        self.environment_id = environment_id
        self.max_restarts = max_restarts
        self._sem = asyncio.Semaphore(max_concurrency)
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    def stop(self) -> None:
        """SIGTERM: 新しいwork itemの受け取りを止める。起動済みのSandboxは中断しない。"""
        self._stop.set()

    async def run(self) -> None:
        await self.adopt()
        it = self.source.items().__aiter__()
        try:
            while not self._stop.is_set():
                if not await self._acquire_or_stop():
                    break
                nxt = asyncio.ensure_future(it.__anext__())
                stopper = asyncio.ensure_future(self._stop.wait())
                done, _ = await asyncio.wait({nxt, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if nxt not in done:  # 停止が先
                    nxt.cancel()
                    self._sem.release()
                    break
                stopper.cancel()
                try:
                    work = nxt.result()
                except StopAsyncIteration:
                    self._sem.release()
                    break
                if work.type != "session":  # セッション以外には何もしない
                    self._sem.release()
                    continue
                self._spawn(self._handle(work))
        finally:
            await self._drain()

    async def _acquire_or_stop(self) -> bool:
        acq = asyncio.ensure_future(self._sem.acquire())
        stopper = asyncio.ensure_future(self._stop.wait())
        done, _ = await asyncio.wait({acq, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if acq in done:
            stopper.cancel()
            return True
        acq.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await acq
        return False

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _drain(self) -> None:
        """終了時: 監視タスクを止める（Sandboxは動かしたまま。再起動後のadoptで引き継ぐ）。"""
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- 起動時の引き継ぎ ---
    async def adopt(self) -> int:
        """`Running`のまま残っているSandboxの監視を引き継ぐ（終了済みなら停止する）。"""
        n = 0
        for sb in await self.kube.list("sandbox", f"{LABEL_MODE}=cma"):
            if operating_mode(sb) != "Running":
                continue
            sid = sb["metadata"].get("annotations", {}).get(ANN_SESSION_ID, "")
            await self._sem.acquire()  # 動作中のSandboxも上限に数える
            self._spawn(self._adopt_one(sid, sb["metadata"]["name"], sb))
            n += 1
        return n

    async def _adopt_one(self, sid: str, name: str, sb: dict) -> None:
        try:
            if is_terminated(sb):
                await self.mgr.finish(sid, sb)
            else:
                # 準備完了にならないまま残ったSandbox（イメージを取得できない等）は、
                # 再起動後も上限時間で失敗として止める
                await self.mgr.wait_ready(sid, name)
                await self._wait_and_publish(sid, name)
        except asyncio.CancelledError:
            raise
        except SandboxStartError as e:
            log.error("引き継いだSandbox %s は準備完了にならず停止した: %s", name, e)
            await self._publish(sid, "failed", str(e))
        except Exception:
            log.exception("引き継いだSandbox %s の監視に失敗", name)
        finally:
            self._sem.release()

    # --- 1つのwork item ---
    async def _handle(self, work: WorkItem) -> None:
        sid = work.session_id
        try:
            email = await self.users.lookup(sid) if self.users else ""
            request = RunRequest(
                session_id=sid,
                mode="cma",
                secret_data={
                    "work_id": work.id,
                    "session_id": sid,
                    "environment_id": self.environment_id,
                    "work_secret": work.secret,
                },
                user_email=email,
            )
            info = None
            for attempt in range(self.max_restarts + 1):
                await self._publish(sid, "starting")
                res = await self.mgr.ensure_started(request)
                await self.mgr.wait_ready(sid, res.name)
                await self._publish(sid, "running")
                info = await self.mgr.wait_exit(sid, res.name)
                if info.exit_code == 0:
                    break
                # 異常終了（Podの削除・OOM・ノードの入れ替え等）: ワーカーが結果を返せずに落ちると、
                # セッションは結果待ちのまま放置される。同じwork itemで再実行して続きを処理させる。
                log.warning(
                    "Sandbox %s が異常終了（終了コード%s）。再実行 %d/%d",
                    res.name,
                    info.exit_code,
                    attempt + 1,
                    self.max_restarts,
                )
            assert info is not None
            # 再実行したワーカーが、前のワーカーのリースを取れずに手を引くと、work itemは`stopped`に
            # ならない（正常終了のワーカーは自分で停止する）。停止する前に、1回だけ判定する。
            incomplete = (
                info.exit_code == 0 and attempt > 0 and not await self._work_completed(work.id)
            )
            if info.exit_code != 0 or incomplete:
                # 落ちたワーカーのリースが残ると、次の依頼のwork itemが割り当てられない
                # （サーバ側で約5分）。強制停止してリースを解放する。
                with contextlib.suppress(Exception):
                    await self.source.stop(work.id)
            if incomplete:
                # セッションがツール結果待ちのまま放置されるので、黙らずに失敗として公開する
                # （Botはturnを失敗にして、セッションを中断する）。
                await self._publish(
                    sid, "failed", "ワーカーが処理を完了できませんでした（Podの異常終了の後）"
                )
            elif info.exit_code == 0:
                await self._publish(sid, "stopped")
            else:
                await self._publish(sid, "failed", f"exit code {info.exit_code}")
        except asyncio.CancelledError:
            raise
        except (SandboxStartError, SandboxError) as e:
            log.error("work item %s の起動に失敗: %s", work.id, e)
            await self._publish(sid, "failed", str(e))
            with contextlib.suppress(Exception):
                await self.source.stop(work.id)
        except Exception as e:
            log.exception("work item %s の処理に失敗", work.id)
            await self._publish(sid, "failed", str(e))
            with contextlib.suppress(Exception):
                await self.source.stop(work.id)
        finally:
            self._sem.release()

    async def _work_completed(self, work_id: str) -> bool:
        try:
            return await self.source.is_stopped(work_id)
        except Exception:
            log.exception("work item %s の状態を確認できません", work_id)
            return True  # 確認できないときは、失敗にしない

    async def _wait_and_publish(self, sid: str, name: str) -> None:
        """引き継いだSandboxの監視（再起動後）。"""
        await self._publish(sid, "running")
        info = await self.mgr.wait_exit(sid, name)
        await self._publish(
            sid,
            "stopped" if info.exit_code == 0 else "failed",
            None if info.exit_code == 0 else f"exit code {info.exit_code}",
        )

    async def _publish(self, sid: str, state: str, error: str | None = None) -> None:
        if self.states is None:
            return
        with contextlib.suppress(Exception):  # 表示用。失敗しても処理は続ける
            await self.states.set(sid, state, error)
