#!/usr/bin/env python3
"""BigQuery分析ユースケースを、Slackなしで、Managed Agentsのセッションを直接作って検証する。

  uv run python usecases/bigquery-analysis/scripts/verify_cma.py --context <ctx> \
      [--state .satin-state-bigquery.json]

Agentはbashの確認が必要（bash_always_ask）なので、このスクリプトが確認に応答して許可する。
確認すること（結果はresults/に記録。秘密の値は出さない）:
  - Sandbox PodがKSA satin-bq-sandboxで動く
  - エージェントがSkillのスクリプトを実行し、その出力に基づいて答える（日付・順位・検索語・条件）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from satin_agent.config import anthropic_client, load_dotenv  # noqa: E402
from verification.common import (  # noqa: E402
    TOOL_RESULT_TYPES,
    Kube,
    Recorder,
    ScenarioResult,
    Ses,
    agent_text,
    event_ids,
    tool_output,
)

PROMPT = "日本の急上昇検索語を分析して、結果を教えてください。"


def wait_turn_allowing_bash(ses: Ses, timeout: float = 420.0) -> list[dict]:
    """turnが終わるまで待つ。確認待ち（requires_action）のツールには許可を返す。"""
    deadline = time.time() + timeout
    decided: set[str] = set()
    while time.time() < deadline:
        evs = ses.events()[ses._mark :]
        last = None
        for e in evs:
            if e["type"] == "session.status_idle":
                last = e
            elif e["type"] in ("session.status_running", "user.message"):
                last = None
        if last:
            sr = last.get("stop_reason") or {}
            if sr.get("type") != "requires_action":
                return evs
            by_id = {e["id"]: e for e in evs}
            for eid in sr.get("event_ids", []):
                if eid in decided or eid not in by_id:
                    continue
                decided.add(eid)
                ses.c.beta.sessions.events.send(
                    ses.id,
                    events=[
                        {"type": "user.tool_confirmation", "tool_use_id": eid, "result": "allow"}
                    ],
                )
        time.sleep(3)
    raise TimeoutError(f"session {ses.id}: turnが{timeout}秒以内に終わりませんでした")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--context", required=True)
    p.add_argument("--namespace", default="satin-sandboxes")
    p.add_argument("--state", default=".satin-state-bigquery.json")
    p.add_argument("--label", default="bigquery-cma")
    args = p.parse_args(argv)

    load_dotenv(".env")
    agent_id = json.loads(Path(args.state).read_text())["agent_id"]
    client = anthropic_client()
    kube = Kube(args.context, args.namespace)
    rec = Recorder(args.label, redact=[os.environ.get("CLAUDE_API_KEY", "")])

    r = ScenarioResult(name="bigquery_analysis", model="agent")
    ses = Ses(client, agent_id, os.environ["CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID"])
    ses.send(PROMPT)
    evs = wait_turn_allowing_bash(ses)
    text, tools = agent_text(evs), tool_output(evs)

    uses = [e for e in evs if e["type"] == "agent.tool_use"]
    ran_script = any("run_query.py" in json.dumps(e.get("input", {})) for e in uses)
    data = None
    for line in tools.splitlines():
        line = line.strip()
        if line.startswith("{") and '"rows"' in line:
            data = json.loads(line)
    rows = (data or {}).get("rows", [])
    terms_in_answer = [row["term"] for row in rows if row["term"] in text]
    dates_in_answer = sorted({row["refresh_date"] for row in rows if row["refresh_date"] in text})
    pods = kube.pods(ses.id)
    ksa = (
        kube.run(
            "-n",
            args.namespace,
            "get",
            *pods,
            "-o",
            "jsonpath={.spec.serviceAccountName}",
            check=False,
        )
        if pods
        else ""
    )

    r.passed = bool(
        ran_script
        and data
        and 0 < len(rows) <= 10
        and terms_in_answer
        and dates_in_answer
        and ksa == "satin-bq-sandbox"
    )
    r.observed = (
        f"スクリプト実行={ran_script}、行数={len(rows)}、回答に含まれた検索語={len(terms_in_answer)}件、"
        f"回答に含まれた日付={dates_in_answer}、PodのKSA={ksa!r}"
    )
    r.evidence = {
        "session": ses.id,
        "tool_use_events": event_ids(evs, "agent.tool_use"),
        "tool_result_events": event_ids(evs, *TOOL_RESULT_TYPES),
        "answer": text,
    }
    rec.add(r)
    print(f"saved {rec.save()}")
    return 0 if r.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
