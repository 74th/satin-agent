import pytest

from satin_agent.ports import ConflictError, NotFoundError
from tests.fakes import FakeClock, FakeKube, FakeObjectStore, FakeSlack


async def test_kube_optimistic_lock_and_watch():
    kube = FakeKube()
    obj = await kube.create("sandbox", {"metadata": {"name": "a", "labels": {"x": "1"}}})
    stale = dict(obj)
    await kube.patch("sandbox", "a", {"spec": {"operatingMode": "Running"}})
    with pytest.raises(ConflictError):
        await kube.replace("sandbox", stale)
    with pytest.raises(ConflictError):
        await kube.create("sandbox", {"metadata": {"name": "a"}})
    assert [o["metadata"]["name"] for o in await kube.list("sandbox", "x=1")] == ["a"]
    await kube.delete("sandbox", "a")
    with pytest.raises(NotFoundError):
        await kube.get("sandbox", "a")


async def test_clock_store_slack_compose():
    clock, store, slack = FakeClock(), FakeObjectStore(), FakeSlack()
    store.write("manifest.json", b"{}")
    await clock.sleep(5)
    ts = await slack.post("C1", None, f"t={clock.now()}")
    await slack.update("C1", ts, "done")
    assert store.read("manifest.json") == b"{}"
    assert slack.messages[0]["text"] == "done"
