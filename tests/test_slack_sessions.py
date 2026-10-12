from types import SimpleNamespace

from satin_agent.slack.sessions import AnthropicSessions


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

        async def create(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(id="sess_1")

        self.beta = SimpleNamespace(sessions=SimpleNamespace(create=create))


async def test_vault_ids_are_attached_when_configured():
    c = _FakeClient()
    s = AnthropicSessions(c, agent_id="a", environment_id="e", vault_ids=["vlt_1"])
    assert await s.create({"k": "v"}) == "sess_1"
    assert c.calls == [
        {"agent": "a", "environment_id": "e", "metadata": {"k": "v"}, "vault_ids": ["vlt_1"]}
    ]


async def test_no_vault_ids_keeps_the_original_call():
    c = _FakeClient()
    s = AnthropicSessions(c, agent_id="a", environment_id="e")
    await s.create({"k": "v"})
    assert c.calls == [{"agent": "a", "environment_id": "e", "metadata": {"k": "v"}}]
