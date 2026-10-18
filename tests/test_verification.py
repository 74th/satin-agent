import json

from verification.common import (
    Recorder,
    ScenarioResult,
    agent_text,
    none_of,
    tool_output,
    turn_finished,
)
from verification.run_cma import SCENARIOS


def ev(i, typ, **kw):
    return {"id": f"e{i}", "type": typ, **kw}


def test_turn_finished_logic():
    base = [ev(1, "user.message"), ev(2, "session.status_running")]
    assert not turn_finished(base)
    tool = [*base, ev(3, "session.status_idle", stop_reason={"type": "requires_action"})]
    assert not turn_finished(tool)  # ツール実行中のidleはturnの終わりではない
    done = [*base, ev(3, "session.status_idle", stop_reason={"type": "end_turn"})]
    assert turn_finished(done)
    resumed = [*done, ev(4, "session.status_running")]
    assert not turn_finished(resumed)
    assert not turn_finished([ev(1, "session.status_idle", stop_reason={"type": "end_turn"})])


def test_text_extraction():
    evs = [
        ev(1, "agent.message", content=[{"type": "text", "text": "こんにちは"}]),
        ev(2, "agent.tool_result", content=[{"type": "text", "text": "out"}]),
    ]
    assert agent_text(evs) == "こんにちは" and tool_output(evs) == "out"


def test_none_of_ignores_empty_values():
    assert none_of("abc secret def", ["secret", "", "zzz"]) == ["secret"]


def test_recorder_redacts_secrets_and_records_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = Recorder("t", redact=["SECRETVALUE"])
    rec.add(ScenarioResult("a", passed=True, observed="ok SECRETVALUE", evidence={"session": "s1"}))
    rec.add(ScenarioResult("b", skipped="MCPなし"))
    path = rec.save()
    data = json.loads(path.read_text())
    assert "SECRETVALUE" not in path.read_text()
    assert [r["result"] for r in data["results"]] == ["pass", "skipped"]
    assert data["results"][0]["evidence"] == {"session": "s1"}


def test_all_scenarios_registered():
    assert {
        "tool_in_sandbox",
        "custom_tool_in_sandbox",
        "skills",
        "mcp",
        "secrets_hidden",
        "egress_blocked",
        "resume_workspace",
        "retention_new_workspace",
        "parallel_sessions",
        "bad_image",
        "kill_pod_during_tool",
        "restart_orchestrator_during_tool",
        "long_tool_default_timeout",
        "long_tool_explicit_timeout",
        "long_tool_25min",
        "bg_run_survives_turn",
        "bg_notice_in_turn",
        "bg_task_stop",
        "bg_max_seconds",
        "bg_lost_on_pod_delete",
        "bg_leftover_children",
    } == set(SCENARIOS)
