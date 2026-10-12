import asyncio
import io

from satin_agent.cma.orchestrator import CmaOrchestrator
from satin_agent.cma.ports import WorkItem
from satin_agent.cma.states import StatePublisher
from satin_agent.cma.users import UserDirectory
from satin_agent.sandbox.config import SandboxConfig
from satin_agent.sandbox.lifecycle_log import LifecycleLog
from satin_agent.sandbox.manager import RunRequest, SandboxManager, is_terminated
from satin_agent.sandbox.naming import sandbox_name, secret_name
from tests.fakes import FakeClock, FakeKube, FakeWorkSource


class Env:
    def __init__(self, max_concurrency=5, **cfgkw):
        self.kube, self.clock = FakeKube(), FakeClock()
        cfgkw.setdefault("ready_timeout", 1e12)  # 偽の時計は即座に進むので、待つテストでは長くする
        self.cfg = SandboxConfig(**cfgkw)
        self.mgr = SandboxManager(
            self.kube, self.clock, self.cfg, LifecycleLog(self.clock.now, io.StringIO())
        )
        self.src = FakeWorkSource()
        self.users = UserDirectory(self.kube, self.cfg.namespace, b"k")
        self.states = StatePublisher(self.kube, self.clock, self.cfg.namespace)
        self.orc = CmaOrchestrator(
            manager=self.mgr,
            kube=self.kube,
            source=self.src,
            users=self.users,
            states=self.states,
            environment_id="env_1",
            max_concurrency=max_concurrency,
        )
        self.task: asyncio.Task | None = None

    def start(self):
        self.task = asyncio.create_task(self.orc.run())

    async def until(self, cond, n=200):
        for _ in range(n):
            if await cond() if asyncio.iscoroutinefunction(cond) else cond():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("条件が満たされませんでした")

    async def finish(self):
        self.orc.stop()
        if self.task:
            await asyncio.wait_for(self.task, 5)


def item(sid, wid="w1", typ="session", secret="sec"):
    return WorkItem(id=wid, type=typ, session_id=sid, secret=secret)


async def sandbox_running(env: Env, sid):
    sb = await env.kube.get("sandbox", sandbox_name(sid, 1))
    return sb["spec"]["operatingMode"]


async def test_session_item_runs_in_sandbox_and_stops_after_exit():
    env = Env()
    await env.users.register("S1", "me@example.com")
    env.start()
    await env.src.q.put(item("S1", secret="WORKSECRET"))
    name = sandbox_name("S1", 1)
    await env.until(lambda: ("sandbox", name) in env.kube.objects)
    sb = await env.kube.get("sandbox", name)
    envs = {
        e["name"]: e["value"] for e in sb["spec"]["podTemplate"]["spec"]["containers"][0]["env"]
    }
    assert envs["AGENT_USER_EMAIL"] == "me@example.com" and envs["SATIN_MODE"] == "cma"
    sec = (await env.kube.get("secret", secret_name("S1")))["stringData"]
    assert sec == {
        "work_id": "w1",
        "session_id": "S1",
        "environment_id": "env_1",
        "work_secret": "WORKSECRET",
    }
    await env.kube.pod_ready(name)
    await env.until(lambda: True)
    st = await env.states.get("S1")
    assert st is not None and st["state"] in ("starting", "running")
    await env.kube.pod_exit(name, 0)
    await env.until(
        lambda: env.kube.objects[("sandbox", name)]["spec"]["operatingMode"] == "Suspended"
    )
    await env.finish()


async def test_non_session_items_are_ignored():
    env = Env()
    env.start()
    await env.src.q.put(item("X", typ="other"))
    await env.until(lambda: len(env.src.pulled) == 1)
    await asyncio.sleep(0.05)
    assert env.kube.objects.keys().isdisjoint({("sandbox", sandbox_name("X", 1))})
    assert not [k for k in env.kube.objects if k[0] == "sandbox"]
    await env.finish()


async def test_two_sessions_run_concurrently():
    env = Env()
    env.start()
    for s in ("A", "B"):
        await env.src.q.put(item(s, wid=f"w-{s}"))
    await env.until(lambda: len([k for k in env.kube.objects if k[0] == "sandbox"]) == 2)
    for s in ("A", "B"):
        await env.kube.pod_ready(sandbox_name(s, 1))
    modes = [
        env.kube.objects[("sandbox", sandbox_name(s, 1))]["spec"]["operatingMode"] for s in "AB"
    ]
    assert modes == ["Running", "Running"]
    await env.finish()


async def test_concurrency_limit_stops_polling():
    env = Env(max_concurrency=1)
    env.start()
    await env.src.q.put(item("A", wid="wA"))
    await env.src.q.put(item("B", wid="wB"))
    await env.until(lambda: len(env.src.pulled) == 1)
    await asyncio.sleep(0.1)
    assert len(env.src.pulled) == 1  # 上限中は次を受け取らない
    a = sandbox_name("A", 1)
    await env.kube.pod_ready(a)
    await env.kube.pod_exit(a, 0)
    await env.until(lambda: len(env.src.pulled) == 2)
    await env.finish()


async def test_start_failure_stops_work_item_and_publishes_failed():
    env = Env(ready_timeout=30)
    env.start()
    await env.src.q.put(item("S1", wid="wbad"))
    await env.until(lambda: ("sandbox", sandbox_name("S1", 1)) in env.kube.objects)
    await env.until(lambda: env.src.stopped == ["wbad"])
    st = await env.states.get("S1")
    assert st is not None and st["state"] == "failed" and "30" in st["error"]
    assert (
        env.kube.objects[("sandbox", sandbox_name("S1", 1))]["spec"]["operatingMode"] == "Suspended"
    )
    await env.finish()


async def test_restart_adopts_running_sandbox_and_stops_it_after_exit():
    env = Env()
    r = await env.mgr.ensure_started(RunRequest("S1", "cma", {"work_secret": "x"}))
    await env.kube.pod_ready(r.name)
    env.start()  # 再起動したオーケストレータ（work itemは来ない）
    await asyncio.sleep(0.05)
    assert env.kube.objects[("sandbox", r.name)]["spec"]["operatingMode"] == "Running"
    await env.kube.pod_exit(r.name, 0)
    await env.until(
        lambda: env.kube.objects[("sandbox", r.name)]["spec"]["operatingMode"] == "Suspended"
    )
    await env.finish()


async def test_restart_stops_already_finished_sandbox():
    env = Env()
    r = await env.mgr.ensure_started(RunRequest("S1", "cma", {"work_secret": "x"}))
    await env.kube.pod_exit(r.name, 0)  # 停止されないまま残っている
    env.start()
    await env.until(
        lambda: env.kube.objects[("sandbox", r.name)]["spec"]["operatingMode"] == "Suspended"
    )
    await env.finish()


async def test_sigterm_stops_intake_but_keeps_sandbox_running():
    env = Env()
    env.start()
    await env.src.q.put(item("S1"))
    name = sandbox_name("S1", 1)
    await env.until(lambda: ("sandbox", name) in env.kube.objects)
    await env.kube.pod_ready(name)
    await env.finish()  # SIGTERM相当
    assert env.kube.objects[("sandbox", name)]["spec"]["operatingMode"] == "Running"
    await env.src.q.put(item("S2"))
    await asyncio.sleep(0.05)
    assert ("sandbox", sandbox_name("S2", 1)) not in env.kube.objects  # もう受け取らない


async def test_users_hmac_and_unmapped():
    kube = FakeKube()
    d = UserDirectory(kube, "ns", b"key")
    await d.register("S1", "a@b.c")
    assert await d.lookup("S1") == "a@b.c"
    assert await d.lookup("unknown") == ""
    other = UserDirectory(kube, "ns", b"other-key")
    assert await other.lookup("S1") == ""  # 鍵が違う = 改ざんとみなす
    cm = await kube.get("configmap", "satin-session-users")
    cm["data"]["S1"] = cm["data"]["S1"].replace("a@b.c", "evil@b.c")
    await kube.replace("configmap", cm)
    assert await d.lookup("S1") == ""


async def test_orchestrator_needs_no_org_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    env = Env()
    env.start()
    await env.src.q.put(item("S1"))
    await env.until(lambda: ("sandbox", sandbox_name("S1", 1)) in env.kube.objects)
    await env.finish()


async def test_restart_adopts_never_ready_sandbox_and_fails_it_after_ready_timeout():
    env = Env(ready_timeout=30)  # イメージを取得できない等で、準備完了にならないSandbox
    r = await env.mgr.ensure_started(RunRequest("S1", "cma", {"work_secret": "x"}))
    await env.kube.pod_waiting(r.name, "ImagePullBackOff")
    env.start()
    await env.until(
        lambda: env.kube.objects[("sandbox", r.name)]["spec"]["operatingMode"] == "Suspended"
    )
    st = await env.states.get("S1")
    assert st is not None and st["state"] == "failed"
    await env.finish()


async def test_abnormal_exit_reruns_the_same_work_item_then_succeeds():
    env = Env()
    env.start()
    await env.src.q.put(item("S1", wid="w1", secret="WS"))
    name = sandbox_name("S1", 1)
    await env.until(lambda: ("sandbox", name) in env.kube.objects)
    await env.kube.pod_ready(name)
    await env.kube.pod_exit(name, 137)  # Podが削除された等で、結果を返せずに落ちた
    # 再実行: 同じwork itemのSecretで、同じSandbox（同じワークスペース）が再開される
    await env.until(
        lambda: (
            env.kube.objects[("sandbox", name)]["spec"]["operatingMode"] == "Running"
            and not is_terminated(env.kube.objects[("sandbox", name)])
        )
    )
    sec = (await env.kube.get("secret", secret_name("S1")))["stringData"]
    assert sec["work_id"] == "w1" and sec["work_secret"] == "WS"
    await env.kube.pod_ready(name)
    await env.kube.pod_exit(name, 0)
    await env.until(
        lambda: env.kube.objects[("sandbox", name)]["spec"]["operatingMode"] == "Suspended"
    )
    st = None
    for _ in range(100):
        st = await env.states.get("S1")
        if st and st["state"] == "stopped":
            break
        await asyncio.sleep(0.01)
    assert st is not None and st["state"] == "stopped"
    assert env.src.stopped == []
    await env.finish()


async def test_abnormal_exit_is_retried_only_up_to_the_limit():
    env = Env()
    env.orc.max_restarts = 1
    env.start()
    await env.src.q.put(item("S1"))
    name = sandbox_name("S1", 1)
    await env.until(lambda: ("sandbox", name) in env.kube.objects)
    for _ in range(2):  # 最初の実行と、再実行の1回
        await env.until(lambda: ("pod", name) in env.kube.objects)
        await env.kube.pod_ready(name)
        await env.kube.pod_exit(name, 137)
        await env.until(
            lambda: (
                env.kube.objects[("sandbox", name)]["spec"]["operatingMode"] == "Suspended"
                or not is_terminated(env.kube.objects[("sandbox", name)])
            )
        )
        await asyncio.sleep(0.05)
    st = None
    for _ in range(200):
        st = await env.states.get("S1")
        if st and st["state"] == "failed":
            break
        await asyncio.sleep(0.01)
    assert st is not None and st["state"] == "failed" and "137" in st["error"]
    assert env.src.stopped == ["w1"] or env.src.stopped  # 再実行を使い切った: リースを解放する
    assert env.kube.objects[("sandbox", name)]["spec"]["operatingMode"] == "Suspended"
    await env.finish()


async def test_rerun_worker_that_left_the_lease_alone_is_reported_as_failed():
    """再実行したワーカーが、リースを取れずに手を引いて（work itemが停止されず）終わった場合。"""
    env = Env()
    env.src.completed = set()  # どのwork itemも完了（stopped）になっていない
    env.start()
    await env.src.q.put(item("S1", wid="w1"))
    name = sandbox_name("S1", 1)
    await env.until(lambda: ("sandbox", name) in env.kube.objects)
    await env.kube.pod_ready(name)
    await env.kube.pod_exit(name, 143)  # 最初のワーカーが落ちた
    await env.until(
        lambda: (
            env.kube.objects[("sandbox", name)]["spec"]["operatingMode"] == "Running"
            and not is_terminated(env.kube.objects[("sandbox", name)])
        )
    )
    await env.kube.pod_ready(name)
    await env.kube.pod_exit(name, 0)  # 再実行は、何もせず正常終了（0）
    st = None
    for _ in range(200):
        st = await env.states.get("S1")
        if st and st["state"] == "failed":
            break
        await asyncio.sleep(0.01)
    assert st is not None and st["state"] == "failed" and "完了できません" in st["error"]
    assert env.src.stopped == ["w1"]  # 落ちたワーカーのリースを解放する
    await env.finish()
