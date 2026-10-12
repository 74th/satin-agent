from typing import Any

from satin_agent.slack.channel import (
    SlackChat,
    SlackEventRouter,
    SlackUserDirectory,
    neutralize_mentions,
    slack_unescape,
)
from satin_agent.slack.ports import ConvRef, Inbound
from tests.fakes import FakeClock


class FakeSlackClient:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.users = {"U1": {"profile": {"email": "a@b.c"}, "team_id": "T1"}}

    async def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        return {"ts": f"{len(self.calls)}.0"}

    async def chat_update(self, **kw):
        self.calls.append(("update", kw))

    async def files_upload_v2(self, **kw):
        self.calls.append(("file", kw))

    async def users_info(self, user):
        self.calls.append(("users_info", {"user": user}))
        if user not in self.users:
            raise RuntimeError("not found")
        return {"user": self.users[user]}


def body(event, event_id="Ev1", team="T1"):
    return {"event_id": event_id, "team_id": team, "event": event}


def make_router(participating=lambda c: False, client=None):
    got: list[Inbound] = []

    async def handle(m):
        got.append(m)

    users = SlackUserDirectory(client or FakeSlackClient(), FakeClock(), workspace_team="T1")
    return SlackEventRouter(
        handle, bot_user_id="UBOT", is_participating=participating, users=users
    ), got


def test_unescape_and_neutralize():
    assert slack_unescape("sleep 1 &amp;&amp; echo &lt;a&gt;") == "sleep 1 && echo <a>"
    assert neutralize_mentions("<!channel> <@U1> <#C1> <https://x>") == (
        "&lt;!channel> &lt;@U1> &lt;#C1> <https://x>"
    )


async def test_mention_starts_conversation_strips_mention_and_unescapes():
    r, got = make_router()
    await r.on_app_mention(
        body({"channel": "C1", "ts": "1.1", "user": "U1", "text": "<@UBOT> echo a &amp;&amp; b"})
    )
    assert got[0].conv == ConvRef("C1", "1.1") and got[0].text == "echo a && b"
    assert got[0].email == "a@b.c" and got[0].user == "U1"


async def test_duplicate_delivery_and_mention_plus_message_are_deduped():
    r, got = make_router(participating=lambda c: True)
    ev = {"channel": "C1", "ts": "2.2", "thread_ts": "1.1", "user": "U1", "text": "hi"}
    await r.on_app_mention(body(ev, "EvA"))
    await r.on_app_mention(body(ev, "EvA"))  # 再配信
    await r.on_message(body(ev, "EvB"))  # 同じ投稿がmessageでも届く
    assert len(got) == 1


async def test_thread_reply_only_when_participating_and_toplevel_ignored():
    r, got = make_router(participating=lambda c: c == ConvRef("C1", "1.1"))
    await r.on_message(
        body({"channel": "C1", "ts": "2.0", "thread_ts": "1.1", "user": "U1", "text": "a"}, "E1")
    )
    await r.on_message(
        body({"channel": "C1", "ts": "3.0", "thread_ts": "9.9", "user": "U1", "text": "b"}, "E2")
    )
    await r.on_message(body({"channel": "C1", "ts": "4.0", "user": "U1", "text": "c"}, "E3"))
    assert [m.text for m in got] == ["a"]


async def test_dm_and_own_posts_and_bot_posts():
    r, got = make_router()
    await r.on_message(
        body({"channel": "D1", "channel_type": "im", "ts": "1.0", "user": "U1", "text": "hi"}, "E1")
    )
    await r.on_message(
        body(
            {"channel": "D1", "channel_type": "im", "ts": "2.0", "user": "UBOT", "text": "x"}, "E2"
        )
    )
    await r.on_message(
        body(
            {"channel": "D1", "channel_type": "im", "ts": "3.0", "bot_id": "B9", "text": "y"}, "E3"
        )
    )
    assert [m.text for m in got] == ["hi", "y"]  # 他のBotはフィルタ側で除外する
    assert got[0].is_dm and got[0].conv == ConvRef("D1", "-") and got[1].bot_id == "B9"


async def test_long_reply_becomes_file_and_progress_update():
    c = FakeSlackClient()
    chat = SlackChat(c, max_chars=100)
    conv = ConvRef("C1", "1.1")
    ref = await chat.post_progress(conv, "進捗")
    await chat.update_progress(conv, ref, "進捗2")
    await chat.post(conv, "短い <!here>")
    await chat.post(conv, "x" * 101)
    kinds = [k for k, _ in c.calls]
    assert kinds == ["post", "update", "post", "file"]
    assert c.calls[2][1]["text"] == "短い &lt;!here>" and c.calls[2][1]["thread_ts"] == "1.1"
    assert c.calls[3][1]["filename"] == "response.md" and c.calls[3][1]["thread_ts"] == "1.1"
    dm = ConvRef("D1", "-")
    await chat.post(dm, "hi")
    assert "thread_ts" not in c.calls[-1][1]


async def test_user_directory_caches_and_failure_not_cached():
    c = FakeSlackClient()
    clock = FakeClock()
    d = SlackUserDirectory(c, clock, workspace_team="T1")
    first = await d.info("U1")
    assert first is not None and first.email == "a@b.c"
    await d.info("U1")
    assert [k for k, _ in c.calls].count("users_info") == 1
    assert await d.info("U404") is None and await d.info("U404") is None
    assert [k for k, _ in c.calls].count("users_info") == 3  # 失敗は毎回取り直す
    clock.advance(601)
    await d.info("U1")
    assert [k for k, _ in c.calls].count("users_info") == 4


async def test_external_user_detection():
    c = FakeSlackClient()
    c.users["UX"] = {"profile": {}, "team_id": "TOTHER"}
    d = SlackUserDirectory(c, FakeClock(), workspace_team="T1")
    ext, own = await d.info("UX"), await d.info("U1")
    assert ext is not None and ext.is_external is True
    assert own is not None and own.is_external is False
