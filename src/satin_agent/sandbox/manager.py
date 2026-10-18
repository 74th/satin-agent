"""セッションごとのSandboxのライフサイクル（作成・再開・終了の検出・停止・保持期限）。

状態はSandbox CRのラベル/アノテーションに持つ。1セッション1Sandboxは、
作成の名前衝突と`resourceVersion`の楽観ロックで守る。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..ports import Clock, ConflictError, KubeApi, NotFoundError
from .config import SandboxConfig
from .lifecycle_log import LifecycleLog
from .naming import (
    ANN_EXIT_CODE,
    ANN_GENERATION,
    ANN_LAST_EXIT_AT,
    ANN_STARTED_AT,
    LABEL_SESSION,
    pvc_name,
    sandbox_name,
    secret_name,
    session_hash,
)
from .policies import POLICY_KINDS, render_policies
from .template import render_sandbox

MAX_ATTEMPTS = 6
TERMINATED_REASONS = ("PodSucceeded", "PodFailed")
IMAGE_PULL_REASONS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName")
IMAGE_PULL_GRACE = 20.0


class SandboxError(Exception):
    pass


class SandboxStartError(SandboxError):
    """起動の失敗、または上限時間の超過。"""


class SandboxEnvMismatch(SandboxError):
    """既存のSandboxの作成時のenvと、今回の指定が違う。"""


@dataclass(frozen=True)
class RunRequest:
    session_id: str
    mode: str  # "cma" | "cc"
    secret_data: dict[str, str]  # 処理ごとのSecret（キー = ファイル名）
    user_email: str = ""


@dataclass(frozen=True)
class StartResult:
    name: str
    generation: int
    action: str  # "created" | "resumed" | "running"


@dataclass(frozen=True)
class ExitInfo:
    name: str
    exit_code: int | None
    reason: str
    message: str = ""  # コンテナの終了メッセージ（/dev/termination-log、4KBまで）


def _cond(obj: dict[str, Any], typ: str) -> dict[str, Any] | None:
    for c in obj.get("status", {}).get("conditions", []) or []:
        if c.get("type") == typ:
            return c
    return None


def is_terminated(obj: dict[str, Any]) -> bool:
    ready = _cond(obj, "Ready")
    return bool(
        ready and ready.get("status") != "True" and ready.get("reason") in TERMINATED_REASONS
    )


def is_ready(obj: dict[str, Any]) -> bool:
    ready = _cond(obj, "Ready")
    return bool(ready and ready.get("status") == "True")


def operating_mode(obj: dict[str, Any]) -> str:
    return obj.get("spec", {}).get("operatingMode", "Running")


def generation_of(obj: dict[str, Any]) -> int:
    return int(obj["metadata"].get("annotations", {}).get(ANN_GENERATION, "1"))


class SandboxManager:
    def __init__(
        self, kube: KubeApi, clock: Clock, cfg: SandboxConfig, log: LifecycleLog | None = None
    ) -> None:
        self.kube, self.clock, self.cfg = kube, clock, cfg
        self.log = log or LifecycleLog(clock.now)

    # --- 検索 ---
    async def latest(self, session_id: str) -> dict[str, Any] | None:
        items = await self.kube.list("sandbox", f"{LABEL_SESSION}={session_hash(session_id)}")
        return max(items, key=generation_of) if items else None

    def _env(self, req: RunRequest) -> dict[str, str]:
        env = {
            "SATIN_MODE": req.mode,
            "SATIN_SESSION_ID": req.session_id,
            "AGENT_USER_EMAIL": req.user_email,
            "SATIN_BASH_DEFAULT_TIMEOUT": f"{self.cfg.bash_default_timeout_seconds:g}",
            "SATIN_BASH_MAX_TIMEOUT": f"{self.cfg.bash_max_timeout_seconds:g}",
            **dict(self.cfg.extra_env),
        }
        if self.cfg.assets_url:
            env["SATIN_ASSETS_URL"] = self.cfg.assets_url
        return env

    IDENTITY_ENV = ("SATIN_MODE", "SATIN_SESSION_ID", "AGENT_USER_EMAIL")

    @staticmethod
    def _env_hash(env: dict[str, str]) -> str:
        """記録用（アノテーション）。照合には使わない。"""
        ident = {k: env.get(k, "") for k in SandboxManager.IDENTITY_ENV}
        return hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:16]

    @staticmethod
    def _identity_mismatch(sb: dict[str, Any], env: dict[str, str]) -> list[str]:
        """Sandboxの作成時のenv（CRに書かれたもの）と、今回の指定で、同一性の項目が違うもの。

        アセットのURLやidleの設定などは、再開するPodが元のCRのenvで動くので、変わっても問題ない。
        照合を同一性だけにしないと、設定を変えてデプロイしただけで、既存の会話が再開できなくなる。
        """
        containers = sb["spec"]["podTemplate"]["spec"]["containers"]
        have = {e["name"]: e.get("value", "") for e in containers[0].get("env", [])}
        return [k for k in SandboxManager.IDENTITY_ENV if have.get(k, "") != env.get(k, "")]

    def _expired(self, sb: dict[str, Any]) -> bool:
        if operating_mode(sb) != "Suspended":
            return False
        at = sb["metadata"].get("annotations", {}).get(ANN_LAST_EXIT_AT)
        return at is not None and self.clock.now() - float(at) >= self.cfg.retention_seconds

    # --- 起動 ---
    async def ensure_started(self, req: RunRequest) -> StartResult:
        """Sandboxを作る/再開する。同じセッションへの同時の指示でもSandboxは1つ。"""
        for _ in range(MAX_ATTEMPTS):
            sb = await self.latest(req.session_id)
            gen = 1
            if sb is not None and self._expired(sb):
                await self._delete(sb, reason="expired")
                gen, sb = generation_of(sb) + 1, None
            elif sb is not None:
                gen = generation_of(sb)
            try:
                if sb is None:
                    return await self._create(req, gen)
                return await self._resume(req, sb)
            except ConflictError:
                continue
        raise SandboxError(f"Sandboxの起動が競合し続けました: {req.session_id}")

    async def _put_secret(
        self, req: RunRequest, gen: int, owner: dict[str, Any] | None = None
    ) -> None:
        name = secret_name(req.session_id, gen)
        obj: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": self.cfg.namespace,
                "labels": {LABEL_SESSION: session_hash(req.session_id)},
            },
            "type": "Opaque",
            "stringData": dict(req.secret_data),
        }
        if owner is not None:
            obj["metadata"]["ownerReferences"] = [_owner_ref(owner)]
        try:
            cur = await self.kube.get("secret", name)
        except NotFoundError:
            try:
                await self.kube.create("secret", obj)
            except ConflictError:
                cur = await self.kube.get("secret", name)
                await self._replace_secret(cur, obj)
            return
        await self._replace_secret(cur, obj)

    async def _replace_secret(self, cur: dict[str, Any], obj: dict[str, Any]) -> None:
        obj["metadata"]["resourceVersion"] = cur["metadata"]["resourceVersion"]
        if "ownerReferences" not in obj["metadata"] and cur["metadata"].get("ownerReferences"):
            obj["metadata"]["ownerReferences"] = cur["metadata"]["ownerReferences"]
        await self.kube.replace("secret", obj)

    async def _create(self, req: RunRequest, gen: int) -> StartResult:
        env = self._env(req)
        manifest = render_sandbox(
            self.cfg,
            session_id=req.session_id,
            generation=gen,
            mode=req.mode,
            env=env,
            env_hash=self._env_hash(env),
        )
        name = manifest["metadata"]["name"]
        manifest["spec"]["operatingMode"] = "Suspended"
        await self._put_secret(req, gen)  # Podが参照するSecretを先に用意する
        created = await self.kube.create("sandbox", manifest)  # 衝突なら呼び出し側がやり直す
        # Sandboxと同時に消えるよう、SecretとポリシーのownerをSandboxにする
        await self.kube.patch(
            "secret",
            secret_name(req.session_id, gen),
            {"metadata": {"ownerReferences": [_owner_ref(created)]}},
        )
        for pol in render_policies(self.cfg, sandbox=name):
            pol["metadata"]["ownerReferences"] = [_owner_ref(created)]
            try:
                await self.kube.create(POLICY_KINDS[pol["kind"]], pol)
            except ConflictError:
                pass
        # 作成直後にcontrollerがstatusを更新して`resourceVersion`が変わるので、replaceではなく
        # 冪等なmerge patchで`Running`にする（1セッション1Sandboxは作成の名前衝突で守っている）
        await self.kube.patch(
            "sandbox",
            name,
            {
                "spec": {"operatingMode": "Running"},
                "metadata": {"annotations": {ANN_STARTED_AT: str(self.clock.now())}},
            },
        )
        self.log.event("start", session_id=req.session_id, sandbox=name, action="create")
        return StartResult(name, gen, "created")

    async def _resume(self, req: RunRequest, sb: dict[str, Any]) -> StartResult:
        name, gen = sb["metadata"]["name"], generation_of(sb)
        env = self._env(req)
        differs = self._identity_mismatch(sb, env)
        if differs:
            raise SandboxEnvMismatch(
                f"{name}: 作成時と今回で違う項目: {', '.join(differs)}"
                "（セッションを始めたユーザが変わった等）"
            )
        mode = operating_mode(sb)
        if mode == "Running" and not is_terminated(sb):
            return StartResult(name, gen, "running")  # 動作中のPodのSecretは触らない
        if mode == "Running":  # 終了済みのPodが残っている: 一度止めてから再開する
            sb = await self._set_mode(sb, "Suspended")
            sb = await self._wait_suspended(name)
        await self._put_secret(req, gen, owner=sb)
        started = dict(sb["metadata"].get("annotations", {}))
        started[ANN_STARTED_AT] = str(self.clock.now())
        sb = {**sb, "metadata": {**sb["metadata"], "annotations": started}}
        await self._set_mode(sb, "Running")
        self.log.event("start", session_id=req.session_id, sandbox=name, action="resume")
        return StartResult(name, gen, "resumed")

    async def _set_mode(self, sb: dict[str, Any], mode: str) -> dict[str, Any]:
        sb = dict(sb)
        sb["spec"] = {**sb["spec"], "operatingMode": mode}
        return await self.kube.replace("sandbox", sb)

    async def _wait_suspended(self, name: str, timeout: float = 120.0) -> dict[str, Any]:
        deadline = self.clock.now() + timeout
        while True:
            sb = await self.kube.get("sandbox", name)
            sel = sb.get("status", {}).get("selector")
            pods = await self.kube.list("pod", sel)
            c = _cond(sb, "Suspended")
            if c and c.get("status") == "True" and not pods:
                return sb
            if self.clock.now() >= deadline:
                raise SandboxError(f"{name} が{timeout}秒以内に停止しませんでした")
            await self.clock.sleep(2)

    # --- 準備完了の待機 ---
    async def wait_ready(self, session_id: str, name: str, timeout: float | None = None) -> str:
        """ "ready"か"exited"（待つ間に処理が終わった）を返す。失敗・超過は停止して例外。"""
        timeout = self.cfg.ready_timeout if timeout is None else timeout
        deadline = self.clock.now() + timeout
        pull_since: float | None = None
        while True:
            sb = await self.kube.get("sandbox", name)
            if is_ready(sb):
                self.log.event("ready", session_id=session_id, sandbox=name)
                return "ready"
            if is_terminated(sb):
                return "exited"
            reason = await self._pull_error(sb)
            if reason:
                pull_since = pull_since if pull_since is not None else self.clock.now()
                if self.clock.now() - pull_since >= IMAGE_PULL_GRACE:
                    return await self._fail_start(
                        session_id, name, f"イメージを取得できません: {reason}"
                    )
            else:
                pull_since = None
            if self.clock.now() >= deadline:
                return await self._fail_start(
                    session_id, name, f"{timeout:.0f}秒以内に準備完了になりませんでした"
                )
            await self.clock.sleep(2)

    async def _pull_error(self, sb: dict[str, Any]) -> str | None:
        sel = sb.get("status", {}).get("selector")
        if not sel:
            return None
        for pod in await self.kube.list("pod", sel):
            for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                w = cs.get("state", {}).get("waiting")
                if w and w.get("reason") in IMAGE_PULL_REASONS:
                    return w["reason"]
        return None

    async def _fail_start(self, session_id: str, name: str, error: str) -> str:
        self.log.event("failed", session_id=session_id, sandbox=name, error=error)
        await self.suspend(name)
        raise SandboxStartError(f"{name}: {error}")

    # --- 終了の検出と停止 ---
    async def exit_info(self, sb: dict[str, Any]) -> ExitInfo:
        ready = _cond(sb, "Ready") or {}
        code: int | None = None
        message = ""
        sel = sb.get("status", {}).get("selector")
        if sel:
            for pod in await self.kube.list("pod", sel):
                for cs in pod.get("status", {}).get("containerStatuses", []) or []:
                    t = cs.get("state", {}).get("terminated")
                    if t is not None:
                        code = int(t.get("exitCode", 0))
                        message = t.get("message") or ""
        if code is None:
            code = 0 if ready.get("reason") == "PodSucceeded" else None
        return ExitInfo(sb["metadata"]["name"], code, ready.get("reason", ""), message)

    async def wait_exit(self, session_id: str, name: str) -> ExitInfo:
        """Podの終了を待つ。watchが切れたら再取得してやり直す。終了したらSandboxを停止する。"""
        while True:
            sb = await self.kube.get("sandbox", name)
            if is_terminated(sb):
                return await self.finish(session_id, sb)
            try:
                async for ev in self.kube.watch("sandbox"):
                    obj = ev["object"]
                    if obj["metadata"]["name"] != name:
                        continue
                    if ev["type"] == "DELETED":
                        raise SandboxError(f"{name} が削除されました")
                    if is_terminated(obj):
                        return await self.finish(session_id, obj)
            except NotFoundError:
                raise SandboxError(f"{name} が見つかりません") from None
            # watchの切断: 取りこぼしを防ぐため、再取得して確認する

    async def finish(self, session_id: str, sb: dict[str, Any]) -> ExitInfo:
        info = await self.exit_info(sb)
        name = sb["metadata"]["name"]
        event = "exit" if info.exit_code == 0 else "failed"
        started = sb["metadata"].get("annotations", {}).get(ANN_STARTED_AT)
        self.log.event(
            event,
            session_id=session_id,
            sandbox=name,
            exit_code=info.exit_code,
            error=None if info.exit_code == 0 else info.reason,
            # 別プロセス（reconcile）で終了を検出する場合も所要時間が分かるよう、開始時刻はCRに持つ
            duration_s=round(self.clock.now() - float(started), 3) if started else None,
        )
        await self.suspend(name, exit_code=info.exit_code)
        return info

    async def suspend(self, name: str, exit_code: int | None = None) -> None:
        """停止状態にする（Podを削除し、ワークスペースは残す）。終了時刻と終了コードを記録する。"""
        for _ in range(MAX_ATTEMPTS):
            try:
                sb = await self.kube.get("sandbox", name)
            except NotFoundError:
                return
            ann = dict(sb["metadata"].get("annotations", {}))
            ann[ANN_LAST_EXIT_AT] = str(self.clock.now())
            if exit_code is not None:
                ann[ANN_EXIT_CODE] = str(exit_code)
            sb["metadata"]["annotations"] = ann
            sb["spec"] = {**sb["spec"], "operatingMode": "Suspended"}
            try:
                await self.kube.replace("sandbox", sb)
                return
            except ConflictError:
                continue
        raise SandboxError(f"{name} を停止できませんでした")

    # --- 削除 ---
    async def _delete(self, sb: dict[str, Any], *, reason: str) -> None:
        name = sb["metadata"]["name"]
        sid = sb["metadata"].get("annotations", {}).get("satin.dev/session-id", "")
        for kind, n in (("sandbox", name), ("pvc", pvc_name(name))):
            try:
                await self.kube.delete(kind, n)
            except NotFoundError:
                pass
        self.log.event("deleted", session_id=sid, sandbox=name, reason=reason)

    async def gc(self, *, delete_all: bool = False) -> list[str]:
        """保持期限を過ぎた停止中のSandboxとPVCを削除する。`delete_all`なら全て。"""
        deleted = []
        for sb in await self.kube.list("sandbox", "app.kubernetes.io/managed-by=satin-agent"):
            if delete_all or self._expired(sb):
                await self._delete(sb, reason="all" if delete_all else "expired")
                deleted.append(sb["metadata"]["name"])
        return deleted


def _owner_ref(owner: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": owner.get("apiVersion", "agents.x-k8s.io/v1beta1"),
        "kind": "Sandbox",
        "name": owner["metadata"]["name"],
        "uid": owner["metadata"].get("uid", ""),
        "controller": False,
        "blockOwnerDeletion": False,
    }


__all__ = [
    "SandboxManager",
    "RunRequest",
    "StartResult",
    "ExitInfo",
    "SandboxError",
    "SandboxStartError",
    "SandboxEnvMismatch",
    "sandbox_name",
]
