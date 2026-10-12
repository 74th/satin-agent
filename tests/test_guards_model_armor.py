from satin_agent.guards.adapters import to_armor_result
from satin_agent.guards.model_armor import (
    BLOCKED_MESSAGE,
    FAILED_MESSAGE,
    ArmorResult,
    OutputInspector,
    split_text,
)

NONE = ArmorResult(False)


def pii(masked="MASKED"):
    return ArmorResult(True, frozenset(), True, masked)


def other(*names):
    return ArmorResult(True, frozenset(names))


class FakeArmor:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.requests = []

    async def sanitize(self, text):
        self.requests.append(text)
        if self.error:
            raise self.error
        return self.results.pop(0)


def make(client, **kw):
    return OutputInspector(client, **kw)


async def test_no_match_shows_text():
    c = FakeArmor([NONE])
    r = await make(c).inspect("テストは3件成功しました")
    assert (r.action, r.text, r.message) == ("show", "テストは3件成功しました", None)


async def test_pii_only_is_masked():
    r = await make(FakeArmor([pii("連絡先は [EMAIL_ADDRESS] です")])).inspect("連絡先は a@b.c です")
    assert (r.action, r.text) == ("masked", "連絡先は [EMAIL_ADDRESS] です")


async def test_pii_without_masked_text_is_not_shown():
    for masked in (None, ""):
        r = await make(FakeArmor([pii(masked)])).inspect("a@b.c")
        assert (r.action, r.text, r.message) == ("blocked", None, BLOCKED_MESSAGE)


async def test_other_filter_blocks_even_with_pii():
    r = await make(FakeArmor([ArmorResult(True, frozenset({"malicious_uri"}), True, "x")])).inspect(
        "x"
    )
    assert r.action == "blocked" and r.message == BLOCKED_MESSAGE and "malicious_uri" in r.detail


async def test_jailbreak_only_is_treated_as_not_matched():
    r = await make(FakeArmor([other("pi_and_jailbreak")])).inspect("echo hi && whoami")
    assert (r.action, r.text) == ("show", "echo hi && whoami")


async def test_jailbreak_with_pii_is_masked():
    r = await make(
        FakeArmor([ArmorResult(True, frozenset({"pi_and_jailbreak"}), True, "[X]")])
    ).inspect("a")
    assert (r.action, r.text) == ("masked", "[X]")


async def test_jailbreak_blocks_when_not_ignored():
    r = await make(FakeArmor([other("pi_and_jailbreak")]), ignored_filters=()).inspect("x")
    assert r.action == "blocked"


async def test_unidentified_match_fails_closed():
    assert (await make(FakeArmor([ArmorResult(True)])).inspect("x")).action == "blocked"


async def test_on_sdp_match_block():
    r = await make(FakeArmor([pii()]), on_sdp_match="block").inspect("a@b.c")
    assert r.action == "blocked"


async def test_failure_is_not_shown():
    r = await make(FakeArmor(error=RuntimeError("503"))).inspect("hello")
    assert (r.action, r.text, r.message) == ("failed", None, FAILED_MESSAGE)


async def test_empty_text_needs_no_call():
    c = FakeArmor()
    assert (await make(c).inspect("  \n")).action == "show"
    assert not c.requests


async def test_long_text_is_split_and_every_part_inspected():
    text = "\n\n".join(f"paragraph {i} " + "x" * 90 for i in range(10))
    parts = split_text(text, 300)
    assert len(parts) > 1 and "".join(parts) == text
    c = FakeArmor([NONE for _ in parts])
    r = await make(c, chunk_chars=300).inspect(text)
    assert (r.action, r.text) == ("show", text)
    assert c.requests == parts


async def test_one_blocked_part_blocks_everything():
    text = "a" * 250 + "\n\n" + "b" * 250
    r = await make(FakeArmor([NONE, other("uri")]), chunk_chars=300).inspect(text)
    assert (r.action, r.text) == ("blocked", None)


async def test_one_failed_part_fails_everything():
    text = "a" * 250 + "\n\n" + "b" * 250
    c = FakeArmor([NONE])  # 2回目でIndexError
    r = await make(c, chunk_chars=300).inspect(text)
    assert r.action == "failed" and r.text is None


async def test_masked_parts_are_reassembled():
    text = "a" * 250 + "\n\n" + "b" * 250
    r = await make(FakeArmor([pii("A"), NONE]), chunk_chars=300).inspect(text)
    assert (r.action, r.text) == ("masked", "A" + "b" * 250)


def test_split_keeps_code_blocks_together():
    code = "```\n" + "\n\n".join(f"line{i}" for i in range(5)) + "\n```\n\n"
    text = "intro\n\n" + code + "outro\n"
    parts = split_text(text, len(code) + 5)
    assert "".join(parts) == text
    assert any(code in p for p in parts)


def test_split_oversized_single_line():
    text = "x" * 1000
    parts = split_text(text, 300)
    assert "".join(parts) == text and all(len(p) <= 300 for p in parts)


# ---- SDKのレスポンスの変換（ネットワークなし）
def test_to_armor_result_sdp_and_jailbreak():
    from google.cloud import modelarmor_v1 as ma

    match = ma.FilterMatchState.MATCH_FOUND
    result = ma.SanitizationResult(
        filter_match_state=match,
        filter_results={
            "sdp": ma.FilterResult(
                sdp_filter_result=ma.SdpFilterResult(
                    deidentify_result=ma.SdpDeidentifyResult(
                        match_state=match, data=ma.DataItem(text="[EMAIL_ADDRESS]")
                    )
                )
            ),
            "pi_and_jailbreak": ma.FilterResult(
                pi_and_jailbreak_filter_result=ma.PiAndJailbreakFilterResult(match_state=match)
            ),
        },
    )
    r = to_armor_result(result)
    assert r == ArmorResult(True, frozenset({"pi_and_jailbreak"}), True, "[EMAIL_ADDRESS]")


def test_to_armor_result_no_match():
    from google.cloud import modelarmor_v1 as ma

    result = ma.SanitizationResult(
        filter_match_state=ma.FilterMatchState.NO_MATCH_FOUND,
        filter_results={
            "pi_and_jailbreak": ma.FilterResult(
                pi_and_jailbreak_filter_result=ma.PiAndJailbreakFilterResult(
                    match_state=ma.FilterMatchState.NO_MATCH_FOUND
                )
            )
        },
    )
    assert to_armor_result(result) == ArmorResult(False)
