import pytest

from satin_agent.guards.config import FilterConfig
from satin_agent.guards.filters import InboundMessage, RuleFilter, UserInfo
from tests.fakes import FakeClock


class FakeUsers:
    def __init__(self, infos=None, error=None):
        self.infos = infos or {}
        self.error = error
        self.calls = []

    async def info(self, user_id):
        self.calls.append(user_id)
        if self.error:
            raise self.error
        return self.infos.get(user_id)


def msg(**kw) -> InboundMessage:
    base = dict(
        team="T1", channel="C1", user="U1", text="hello", ts="1.0", thread_ts=None, is_dm=False
    )
    base.update(kw)
    return InboundMessage(**base)


def make(cfg=None, users=None, clock=None):
    clock = clock or FakeClock()
    users = users or FakeUsers({"U1": UserInfo(False, False, "a@example.com")})
    return RuleFilter(cfg or FilterConfig(), users, clock, "UBOT"), users, clock


async def test_normal_message_is_allowed():
    f, _, _ = make()
    r = await f.check(msg())
    assert r.allowed and r.reply is None


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"bot_id": "B1"}, "bot"),
        ({"user": "UBOT"}, "bot"),
        ({"subtype": "message_changed"}, "system_event"),
        ({"subtype": "channel_join"}, "system_event"),
        ({"text": "  \n"}, "empty"),
    ],
)
async def test_excluded_silently(kw, reason):
    f, _, _ = make()
    r = await f.check(msg(**kw))
    assert not r.allowed and r.reply is None and r.reason == reason


async def test_file_share_and_thread_broadcast_are_messages():
    f, _, _ = make()
    assert (await f.check(msg(subtype="file_share"))).allowed
    assert (await f.check(msg(subtype="thread_broadcast"))).allowed


async def test_allow_lists():
    f, _, _ = make(FilterConfig(teams=("T1",), channels=("C1",), users=("U1",)))
    assert (await f.check(msg())).allowed
    for kw in ({"team": "T2"}, {"channel": "C2"}, {"user": "U2"}):
        r = await f.check(msg(**kw))
        assert not r.allowed and r.reply is None


async def test_channel_list_does_not_apply_to_dm():
    f, _, _ = make(FilterConfig(channels=("C1",)))
    assert (await f.check(msg(channel="D9", is_dm=True))).allowed


async def test_direct_messages_can_be_denied():
    f, _, _ = make(FilterConfig(allow_direct_messages=False))
    r = await f.check(msg(is_dm=True))
    assert not r.allowed and r.reply is None
    assert (await f.check(msg())).allowed


async def test_guest_and_external_are_denied_by_config():
    users = FakeUsers(
        {
            "G": UserInfo(True, False, None),
            "X": UserInfo(False, True, None),
            "U1": UserInfo(False, False, None),
        }
    )
    f, _, _ = make(FilterConfig(deny_guests=True, deny_external_users=True), users)
    assert (await f.check(msg(user="U1"))).allowed
    assert (await f.check(msg(user="G"))).reason == "guest"
    assert (await f.check(msg(user="X"))).reason == "external_user"


async def test_guests_are_allowed_when_not_denied_and_no_lookup():
    f, users, _ = make(FilterConfig(), FakeUsers({"G": UserInfo(True, True, None)}))
    assert (await f.check(msg(user="G"))).allowed
    assert users.calls == []


async def test_undeterminable_user_is_excluded_when_denying():
    for users in (FakeUsers(), FakeUsers(error=RuntimeError("boom"))):
        f, _, _ = make(FilterConfig(deny_guests=True), users)
        r = await f.check(msg(user="U9"))
        assert not r.allowed and r.reply is None


async def test_users_info_is_cached_for_ten_minutes():
    f, users, clock = make(FilterConfig(deny_guests=True))
    await f.check(msg())
    await f.check(msg())
    assert users.calls == ["U1"]
    clock.advance(599)
    await f.check(msg())
    assert users.calls == ["U1"]
    clock.advance(2)
    await f.check(msg())
    assert users.calls == ["U1", "U1"]


async def test_failed_lookup_is_not_cached():
    users = FakeUsers(error=RuntimeError("boom"))
    f, _, _ = make(FilterConfig(deny_guests=True), users)
    await f.check(msg())
    users.error = None
    users.infos["U1"] = UserInfo(False, False, None)
    assert (await f.check(msg())).allowed


async def test_length_cap_replies():
    f, _, _ = make()
    assert (await f.check(msg(text="a" * 8000))).allowed
    r = await f.check(msg(text="a" * 8001))
    assert not r.allowed and r.reply and "8000" in r.reply and r.reason == "too_long"


async def test_rate_limit_per_user_per_minute():
    f, _, clock = make(users=FakeUsers())
    for _ in range(20):
        assert (await f.check(msg())).allowed
    r = await f.check(msg())
    assert not r.allowed and r.reply and r.reason == "rate_limited"
    assert (await f.check(msg(user="U2"))).allowed  # 別ユーザは別枠
    clock.advance(60)
    assert (await f.check(msg())).allowed


async def test_excluded_messages_do_not_consume_rate_limit():
    f, _, _ = make(FilterConfig(rate_limit_per_minute=1))
    await f.check(msg(text=""))
    assert (await f.check(msg())).allowed
