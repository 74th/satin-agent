"""10.5 #5: bashの確認待ちのセッションがあるときにBotが再起動しても、再起動後のBotが確認に応答する。

手順（Slackには検証用の最上位メッセージを1つ投稿し、その返信スレッドに結果が出る）:
1. Botを止める（replicas=0）。
2. Botのagent（bashは`always_ask`）のセッションを直接作り、`bash ls`の確認待ちにする。
3. BotのDBに「実行中のturn」を登録する（Botが落ちた時点の状態の再現）。
4. Botを起動する。起動時の復帰が、確認待ちに応答してturnを完了させるのを確認する。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from satin_agent.config import anthropic_client, load_dotenv
from verification.common import Recorder, ScenarioResult

CTX = "gke_nnyn-dev_us-central1-a_silk-agent-gke"
SYS = "satin-system"


def kubectl(*a: str, inp: str | None = None) -> str:
    r = subprocess.run(["kubectl", "--context", CTX, *a], capture_output=True, text=True, input=inp)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def main() -> int:
    load_dotenv(".env")
    channel = sys.argv[1]
    c = anthropic_client()
    agent_id = json.load(open(".satin-state.json"))["agent_id"]  # Botのagent（bashはalways_ask）
    env_id = os.environ["CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID"]

    from slack_sdk import WebClient

    slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    root = slack.chat_postMessage(channel=channel, text="[検証] bashの確認待ちの間のBot再起動")
    ts = root["ts"]
    conv_key = f"{channel}:{ts}"

    res = ScenarioResult("bot_restart_during_bash_confirmation", model="claude-haiku-5-5")
    kubectl("-n", SYS, "scale", "deploy/satin-slack-bot", "--replicas=0")
    kubectl("-n", SYS, "wait", "--for=delete", "pod", "-l", "app=satin-slack-bot", "--timeout=90s")
    try:
        sid = c.beta.sessions.create(agent=agent_id, environment_id=env_id).id
        c.beta.sessions.events.send(
            sid,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": "bashで ls を実行して"}],
                }
            ],
        )
        ask_ids: list[str] = []
        for _ in range(60):
            evs = [e.model_dump() for e in c.beta.sessions.events.list(sid)]
            ask_ids = [
                e["id"]
                for e in evs
                if e["type"] == "agent.tool_use" and e.get("evaluated_permission") == "ask"
            ]
            if ask_ids:
                break
            time.sleep(2)
        waiting = bool(ask_ids)
        print("確認待ち:", waiting, ask_ids)

        # 「前回のBotが実行中のまま残したturn」を、DBへ直接登録する（Botは停止中）
        script = (
            "import sqlite3, json, time\n"
            "db = sqlite3.connect('/data/satin-bot.db')\n"
            "db.execute('INSERT OR REPLACE INTO threads(conv_key, session_id, user_email,"
            " created_at) VALUES(?,?,?,?)',\n"
            f"    ('{conv_key}', '{sid}', '', time.time()))\n"
            "db.execute('INSERT OR REPLACE INTO turns(turn_id, conv_key, session_id, status,"
            " progress_ref, last_event_id, started_at, state) VALUES(?,?,?,?,?,?,?,?)',\n"
            f"    ('verify-confirm-restart', '{conv_key}', '{sid}', 'running', '', '',"
            " time.time(), json.dumps({'sent': 1, 'echoed': 1})))\n"
            "db.commit()\n"
        )
        # Podがないので、DBのPVCを使う使い捨てのPodで書き込む
        spec = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "satin-verify-db", "namespace": SYS},
            "spec": {
                "restartPolicy": "Never",
                "securityContext": {"runAsUser": 1000, "fsGroup": 1000},
                "containers": [
                    {
                        "name": "w",
                        "image": subprocess.run(
                            [
                                "kubectl",
                                "--context",
                                CTX,
                                "-n",
                                SYS,
                                "get",
                                "deploy",
                                "satin-slack-bot",
                                "-o",
                                "jsonpath={.spec.template.spec.containers[0].image}",
                            ],
                            capture_output=True,
                            text=True,
                        ).stdout,
                        "command": ["python3", "-c", script],
                        "volumeMounts": [{"name": "d", "mountPath": "/data"}],
                    }
                ],
                "volumes": [
                    {"name": "d", "persistentVolumeClaim": {"claimName": "satin-slack-bot-data"}}
                ],
            },
        }
        kubectl("apply", "-f", "-", inp=json.dumps(spec))
        for _ in range(60):
            ph = kubectl(
                "-n", SYS, "get", "pod", "satin-verify-db", "-o", "jsonpath={.status.phase}"
            )
            if ph in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        kubectl("-n", SYS, "delete", "pod", "satin-verify-db", "--wait=true")

        kubectl("-n", SYS, "scale", "deploy/satin-slack-bot", "--replicas=1")
        kubectl("-n", SYS, "rollout", "status", "deploy/satin-slack-bot", "--timeout=120s")
        done = False
        for _ in range(60):
            evs = [e.model_dump() for e in c.beta.sessions.events.list(sid)]
            if any(e["type"] == "user.tool_confirmation" for e in evs) and any(
                e["type"] == "session.status_idle"
                and (e.get("stop_reason") or {}).get("type") == "end_turn"
                for e in evs
            ):
                done = True
                break
            time.sleep(3)
        confirm = [e for e in evs if e["type"] == "user.tool_confirmation"]
        logs = kubectl("-n", SYS, "logs", "deploy/satin-slack-bot", "--since=3m")
        recovered = [ln for ln in logs.splitlines() if "recovered" in ln]
        res.passed = waiting and done and bool(confirm)
        res.observed = (
            f"確認待ち={waiting}、再起動後のBotが確認に応答={bool(confirm)}、turnは完了={done}"
        )
        res.evidence = {
            "session": sid,
            "thread": conv_key,
            "confirmation_events": [e["id"] for e in confirm],
            "confirmation": [(e.get("tool_use_id"), e.get("result")) for e in confirm],
            "bot_log": recovered[-2:],
        }
    finally:
        kubectl("-n", SYS, "scale", "deploy/satin-slack-bot", "--replicas=1")
    rec = Recorder("bot-confirm-restart", redact=[os.environ.get("SLACK_BOT_TOKEN", "")])
    rec.add(res)
    print("saved", rec.save())
    return 0 if res.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
