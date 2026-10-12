import asyncio
import io
import json

import pytest

from satin_agent.sandbox.config import SandboxConfig
from satin_agent.sandbox.lifecycle_log import LifecycleLog
from satin_agent.sandbox.manager import (
    RunRequest,
    SandboxEnvMismatch,
    SandboxManager,
    SandboxStartError,
)
from satin_agent.sandbox.naming import pvc_name, sandbox_name, secret_name
from tests.fakes import FakeClock, FakeKube


def make(retention=3600.0, **cfgkw):
    kube, clock, out = FakeKube(), FakeClock(), io.StringIO()
    cfg = SandboxConfig(retention_seconds=retention, **cfgkw)
    mgr = SandboxManager(kube, clock, cfg, LifecycleLog(clock.now, out))
    return mgr, kube, clock, out


def req(sid="S1", secret="v1", email="a@b.c", mode="cma"):
    return RunRequest(sid, mode, {"work_secret": secret}, email)


async def test_create_makes_sandbox_secret_policies_without_leaking_secret():
    mgr, kube, _, _ = make()
    res = await mgr.ensure_started(req(secret="TOPSECRET"))
    assert res.action == "created" and res.name == sandbox_name("S1", 1)
    sb = await kube.get("sandbox", res.name)
    assert sb["spec"]["operatingMode"] == "Running"
    assert "TOPSECRET" not in json.dumps(sb)
    sec = await kube.get("secret", secret_name("S1"))
    assert sec["stringData"]["work_secret"] == "TOPSECRET"
    assert sec["metadata"]["ownerReferences"][0]["uid"] == sb["metadata"]["uid"]
    kinds = {k for (k, _) in kube.objects}
    assert {"networkpolicy", "fqdnnetworkpolicy"} <= kinds
    assert len(await kube.list("networkpolicy")) == 3  # deny + dns + metadata


async def test_concurrent_start_yields_single_sandbox():
    mgr, kube, _, out = make()
    real_create = kube.create
    raced = []

    async def racing_create(kind, obj):
        if kind == "sandbox" and not raced:
            raced.append(1)
            other = SandboxManager(kube, mgr.clock, mgr.cfg, mgr.log)
            await other.ensure_started(req())  # 別のインスタンスが先に作る
        return await real_create(kind, obj)

    kube.create = racing_create  # type: ignore[method-assign]
    res = await mgr.ensure_started(req())
    assert res.action == "running"
    assert len(await kube.list("sandbox")) == 1
    # 競合で失敗した側は「開始」を記録しない（開始と終了が対になる）
    assert out.getvalue().count('"event": "start"') == 1


async def test_start_twice_while_running_is_noop_and_keeps_secret():
    mgr, kube, _, _ = make()
    await mgr.ensure_started(req(secret="one"))
    res = await mgr.ensure_started(req(secret="two"))
    assert res.action == "running"
    assert (await kube.get("secret", secret_name("S1")))["stringData"]["work_secret"] == "one"


async def test_exit_suspends_records_code_and_resume_reuses_workspace():
    mgr, kube, clock, _ = make()
    r1 = await mgr.ensure_started(req(secret="one"))
    await kube.pod_ready(r1.name)
    assert await mgr.wait_ready("S1", r1.name) == "ready"
    task = asyncio.create_task(mgr.wait_exit("S1", r1.name))
    await asyncio.sleep(0)
    await kube.pod_exit(r1.name, 0)
    info = await asyncio.wait_for(task, 2)
    assert info.exit_code == 0
    sb = await kube.get("sandbox", r1.name)
    assert sb["spec"]["operatingMode"] == "Suspended"
    assert sb["metadata"]["annotations"]["satin.dev/exit-code"] == "0"
    assert ("pod", r1.name) not in kube.objects

    r2 = await mgr.ensure_started(req(secret="two"))
    assert r2.action == "resumed" and r2.name == r1.name  # 同じSandbox = 同じPVC
    assert (await kube.get("secret", secret_name("S1")))["stringData"]["work_secret"] == "two"
    assert (await kube.get("sandbox", r2.name))["spec"]["operatingMode"] == "Running"
    assert len(await kube.list("sandbox")) == 1


async def test_abnormal_exit_records_nonzero_code():
    mgr, kube, _, out = make()
    r = await mgr.ensure_started(req())
    await kube.pod_ready(r.name)
    await kube.pod_exit(r.name, 3)
    info = await asyncio.wait_for(mgr.wait_exit("S1", r.name), 2)  # 既に終了済み
    assert info.exit_code == 3
    sb = await kube.get("sandbox", r.name)
    assert sb["metadata"]["annotations"]["satin.dev/exit-code"] == "3"
    assert sb["spec"]["operatingMode"] == "Suspended"
    assert '"event": "failed"' in out.getvalue()


async def test_watch_disconnect_is_recovered_by_relist():
    mgr, kube, _, _ = make()
    r = await mgr.ensure_started(req())
    await kube.pod_ready(r.name)
    task = asyncio.create_task(mgr.wait_exit("S1", r.name))
    await asyncio.sleep(0)
    await kube.pod_exit(r.name, 0)  # watchが取りこぼしても…
    kube.disconnect_watchers()
    assert (await asyncio.wait_for(task, 2)).exit_code == 0


async def test_terminated_but_still_running_is_cycled_on_next_start():
    mgr, kube, _, _ = make()
    r = await mgr.ensure_started(req(secret="one"))
    await kube.pod_ready(r.name)
    await kube.pod_exit(r.name, 0)  # reconcileがまだ停止していない
    r2 = await mgr.ensure_started(req(secret="two"))
    assert r2.action == "resumed"
    assert ("pod", r.name) in kube.objects  # 新しいPod


async def test_env_mismatch_on_resume():
    mgr, kube, _, _ = make()
    r = await mgr.ensure_started(req(email="a@b.c"))
    await mgr.suspend(r.name)
    with pytest.raises(SandboxEnvMismatch):
        await mgr.ensure_started(req(email="other@b.c"))


async def test_ready_timeout_fails_and_suspends():
    mgr, kube, clock, out = make(ready_timeout=30)
    r = await mgr.ensure_started(req())
    with pytest.raises(SandboxStartError, match="30"):
        await mgr.wait_ready("S1", r.name)
    assert (await kube.get("sandbox", r.name))["spec"]["operatingMode"] == "Suspended"
    assert '"event": "failed"' in out.getvalue()


async def test_missing_image_fails_within_timeout():
    mgr, kube, clock, _ = make(ready_timeout=180, image="nope:404")
    r = await mgr.ensure_started(req())
    await kube.pod_waiting(r.name, "ImagePullBackOff")
    t0 = clock.now()
    with pytest.raises(SandboxStartError, match="ImagePullBackOff"):
        await mgr.wait_ready("S1", r.name)
    assert clock.now() - t0 <= 180
    assert (await kube.get("sandbox", r.name))["spec"]["operatingMode"] == "Suspended"


async def test_ready_returns_exited_if_worker_already_finished():
    mgr, kube, _, _ = make()
    r = await mgr.ensure_started(req())
    await kube.pod_exit(r.name, 0)
    assert await mgr.wait_ready("S1", r.name) == "exited"


async def test_retention_expiry_creates_new_generation_and_deletes_old():
    mgr, kube, clock, _ = make(retention=10)
    r = await mgr.ensure_started(req())
    await kube.create("pvc", {"metadata": {"name": pvc_name(r.name)}})
    await mgr.suspend(r.name)
    clock.advance(11)
    r2 = await mgr.ensure_started(req())
    assert r2.action == "created" and r2.name == sandbox_name("S1", 2)
    assert ("sandbox", r.name) not in kube.objects
    # Secretは世代ごとに別の名前（古いSandboxのGCに新しいSecretが巻き込まれない）
    assert secret_name("S1", 1) != secret_name("S1", 2)
    assert ("secret", secret_name("S1", 2)) in kube.objects
    sb2 = await kube.get("sandbox", r2.name)
    vol = sb2["spec"]["podTemplate"]["spec"]["volumes"][0]["secret"]["secretName"]
    assert vol == secret_name("S1", 2)
    assert ("pvc", pvc_name(r.name)) not in kube.objects


async def test_not_expired_resumes_same_workspace():
    mgr, kube, clock, _ = make(retention=10)
    r = await mgr.ensure_started(req())
    await mgr.suspend(r.name)
    clock.advance(5)
    assert (await mgr.ensure_started(req())).name == r.name


async def test_gc_deletes_only_expired_unless_all():
    mgr, kube, clock, _ = make(retention=10)
    a = await mgr.ensure_started(req("A"))
    b = await mgr.ensure_started(req("B"))
    await mgr.suspend(a.name)
    clock.advance(11)
    await mgr.suspend(b.name)  # Bは今停止したので期限内
    assert await mgr.gc() == [a.name]
    assert await mgr.gc(delete_all=True) == [b.name]


async def test_lifecycle_log_pairs_start_and_end():
    mgr, kube, clock, out = make()
    for sid in ("A", "B", "C"):
        for _ in range(2):
            r = await mgr.ensure_started(req(sid))
            await kube.pod_ready(r.name)
            await kube.pod_exit(r.name, 0)
            await mgr.wait_exit(sid, r.name)
    recs = [json.loads(line) for line in out.getvalue().splitlines()]
    for sid in ("A", "B", "C"):
        mine = [r for r in recs if r["session_id"] == sid]
        starts = [r for r in mine if r["event"] == "start"]
        ends = [r for r in mine if r["event"] in ("exit", "failed")]
        assert len(starts) == len(ends) == 2
        assert all("duration_s" in r for r in ends)


async def test_create_survives_controller_status_update_between_create_and_start():
    """作成直後にcontrollerがstatusを更新しても、新規作成として成功する（実クラスタで起きた競合）。"""
    mgr, kube, _, out = make()
    real = kube.create

    async def create_then_controller_touches(kind, obj):
        created = await real(kind, obj)
        if kind == "sandbox":
            await kube.patch(kind, obj["metadata"]["name"], {"status": {"observedGeneration": 1}})
        return created  # 呼び出し側が持つresourceVersionは古くなる

    kube.create = create_then_controller_touches  # type: ignore[method-assign]
    res = await mgr.ensure_started(req())
    assert res.action == "created"
    assert (await kube.get("sandbox", res.name))["spec"]["operatingMode"] == "Running"
    assert '"action": "create"' in out.getvalue()


async def test_resume_tolerates_changed_non_identity_env_but_not_changed_user():
    mgr, kube, clock, _ = make(assets_url="gs://b/old/")
    r = await mgr.ensure_started(req())
    await mgr.suspend(r.name)
    # アセットのURLやidleの設定が変わっても、既存の会話は再開できる
    mgr2 = SandboxManager(
        kube,
        clock,
        SandboxConfig(assets_url="gs://b/new/", extra_env=(("SATIN_CC_RELEASE_IDLE_MIN", "1"),)),
        mgr.log,
    )
    assert (await mgr2.ensure_started(req())).action == "resumed"
    await mgr2.suspend(r.name)
    with pytest.raises(SandboxEnvMismatch):  # ユーザ（同一性）が変わったときは拒否する
        await mgr2.ensure_started(req(email="other@b.c"))


async def test_resume_matches_identity_from_the_sandbox_env_not_from_a_stored_hash():
    """古いSandbox（保存された照合値が現在のenvと合わない）でも、同一性が同じなら再開できる。"""
    mgr, kube, _, _ = make()
    r = await mgr.ensure_started(req())
    await kube.patch(
        "sandbox", r.name, {"metadata": {"annotations": {"satin.dev/env-hash": "old"}}}
    )
    await mgr.suspend(r.name)
    assert (await mgr.ensure_started(req())).action == "resumed"
