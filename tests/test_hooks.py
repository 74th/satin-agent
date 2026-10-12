import asyncio
import io
import json
import logging

import pytest

from satin_agent.hooks import (
    EVENT_KINDS,
    HookDispatcher,
    HookEvent,
    HookLoadError,
    JsonLinesHook,
    load_hooks,
)


def ev(kind="user_input", n=0, **data) -> HookEvent:
    return HookEvent(kind, "s1", 1000.0 + n, dict(data))


class Recorder:
    def __init__(self):
        self.events = []
        self.closed = False

    async def on_event(self, event):
        self.events.append(event)

    async def aclose(self):
        self.closed = True


class Raiser:
    def __init__(self):
        self.calls = 0

    async def on_event(self, event):
        self.calls += 1
        raise RuntimeError("boom")


class Slow:
    async def on_event(self, event):
        await asyncio.sleep(3600)


def test_event_kinds_include_bash_approval():
    assert "bash_approval" in EVENT_KINDS
    assert {"user_input", "turn_start", "turn_end", "tool_use", "tool_result"} <= set(EVENT_KINDS)
    assert {"agent_message", "notice"} <= set(EVENT_KINDS)


async def test_events_are_delivered_in_order_to_every_hook():
    a, b = Recorder(), Recorder()
    d = HookDispatcher([a, b])
    await d.start()
    events = [ev(n=i) for i in range(5)]
    for e in events:
        d.emit(e)
    await d.aclose()
    assert a.events == events and b.events == events
    assert a.closed and b.closed


async def test_emit_is_non_blocking_and_queues_before_start():
    r = Recorder()
    d = HookDispatcher([r])
    d.emit(ev())
    assert r.events == []  # 同期的には配信されない
    await d.start()
    await d.aclose()
    assert len(r.events) == 1


async def test_oldest_dropped_when_over_limit_and_logged(caplog):
    r = Recorder()
    d = HookDispatcher([r], queue_limit=3)
    for i in range(5):
        d.emit(ev(n=i))
    assert d.dropped == 2
    with caplog.at_level(logging.WARNING):
        await d.start()
        await d.aclose()
    assert [e.ts for e in r.events] == [1002.0, 1003.0, 1004.0]


async def test_dropped_is_logged(caplog):
    d = HookDispatcher([Recorder()], queue_limit=1)
    with caplog.at_level(logging.WARNING):
        d.emit(ev())
        d.emit(ev())
    assert "dropped the oldest" in caplog.text


async def test_raising_hook_does_not_break_other_hooks_or_delivery(caplog):
    bad, good = Raiser(), Recorder()
    d = HookDispatcher([bad, good])
    await d.start()
    with caplog.at_level(logging.ERROR):
        for i in range(50):
            d.emit(ev(n=i))
        await d.aclose()
    assert bad.calls == 50 and len(good.events) == 50
    assert "hook Raiser failed" in caplog.text


async def test_emit_with_raising_hook_does_not_block_the_caller():
    d = HookDispatcher([Raiser()])
    await d.start()
    for i in range(1000):
        d.emit(ev(n=i))  # 同期でただちに戻る
    await asyncio.sleep(0)
    await d.aclose()


async def test_slow_hook_times_out_and_delivery_continues():
    good = Recorder()
    d = HookDispatcher([Slow(), good], hook_timeout_seconds=0.01)
    await d.start()
    d.emit(ev())
    d.emit(ev(n=1))
    await d.aclose(timeout_seconds=5)
    assert len(good.events) == 2


async def test_emit_after_close_is_ignored():
    r = Recorder()
    d = HookDispatcher([r])
    await d.start()
    await d.aclose()
    d.emit(ev())
    assert r.events == []


async def test_failing_aclose_is_isolated(caplog):
    class BadClose(Recorder):
        async def aclose(self):
            raise RuntimeError("x")

    other = Recorder()
    d = HookDispatcher([BadClose(), other])
    await d.start()
    await d.aclose()
    assert other.closed


# ---- load_hooks
def test_load_json_lines():
    (hook,) = load_hooks([{"factory": "json_lines", "options": {"include_body": True}}])
    assert isinstance(hook, JsonLinesHook)


def test_load_custom_factory():
    (hook,) = load_hooks([{"factory": "tests.test_hooks:make_recorder"}])
    assert isinstance(hook, Recorder)


def make_recorder():
    return Recorder()


@pytest.mark.parametrize(
    "spec",
    [
        {"factory": "no_such_module_xyz:f"},
        {"factory": "tests.test_hooks:nope"},
        {"factory": "bad-format"},
        {"factory": "tests.test_hooks:not_a_hook"},
        {"factory": "json_lines", "options": {"bogus": 1}},
        {},
    ],
)
def test_unloadable_hook_raises(spec):
    with pytest.raises(HookLoadError):
        load_hooks([spec])


def not_a_hook():
    return object()


# ---- json_lines
async def test_json_lines_omits_body_by_default():
    out = io.StringIO()
    hook = JsonLinesHook(out=out)
    await hook.on_event(ev(text="秘密の本文", user="U1", decision="accepted", tool_input={"a": 1}))
    line = out.getvalue()
    assert line.endswith("\n") and line.count("\n") == 1
    obj = json.loads(line)
    assert obj["kind"] == "user_input" and obj["session_id"] == "s1" and obj["ts"] == 1000.0
    assert obj["data"] == {"user": "U1", "decision": "accepted"}
    assert "秘密" not in line


async def test_json_lines_includes_body_when_enabled():
    out = io.StringIO()
    await JsonLinesHook(include_body=True, out=out).on_event(ev(text="本文"))
    assert json.loads(out.getvalue())["data"]["text"] == "本文"


async def test_json_lines_defaults_to_stdout(capsys):
    await JsonLinesHook().on_event(ev(decision="blocked"))
    assert json.loads(capsys.readouterr().out)["data"]["decision"] == "blocked"
