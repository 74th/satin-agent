"""(A) Managed Agentsのシナリオ実行（design.md / specs/resilience-verification）。

Managed Agentsのセッションを直接操作し、kubectlで障害を注入して、合否と根拠
（イベントID・ログの行・Jevの判定値）をresults/に記録する。

    uv run python verification/run_cma.py --context <ctx> --expected-image-id satin-runtime-<tag> \
        [--only tool_in_sandbox,skills] [--agent-id agent_...] [--mcp-url https://...]

固有の値（イメージ識別子など）は文字列一致、意味の判定はJev（tests/live/jev.py）で行う。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from satin_agent.config import anthropic_client, load_dotenv
from verification.common import (
    TOOL_RESULT_TYPES,
    Kube,
    Recorder,
    ScenarioResult,
    Ses,
    agent_text,
    contains,
    event_ids,
    jev_mentions,
    none_of,
    tool_output,
    turn_finished,
)

CONFIG_MAP = "satin-config"
SYSTEM_NS = "satin-system"
CMA_DEPLOY = "satin-cma-orchestrator"


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        load_dotenv(".env")
        self.args = args
        self.client = anthropic_client()
        self.env_id = os.environ["CLAUDE_MANAGED_AGENT_ENVIRONMENT_ID"]
        self.kube = Kube(args.context, args.namespace)
        state = json.loads(Path(args.state).read_text()) if Path(args.state).exists() else {}
        self.agent_id = args.agent_id or state.get("agent_id", "")
        self.model = args.model
        self.secrets = [
            os.environ.get(k, "")
            for k in (
                "CLAUDE_API_KEY",
                "CLAUDE_MANAGED_AGENT_ENVIRONMENT_KEY",
                "CLAUDE_CODE_SELF_HOSTED_ENVIRONMENT_KEY",
                "SLACK_BOT_TOKEN",
                "SLACK_APP_TOKEN",
                "JEV_API_TOKEN",
            )
        ]

    def session(self) -> Ses:
        return Ses(self.client, self.agent_id, self.env_id)

    def result(self, name: str) -> ScenarioResult:
        return ScenarioResult(name=name, model=self.model)

    # --- orchestratorの設定の一時変更（必ず元に戻す） ---
    def patch_config(self, mutate: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        raw = self.kube.run("-n", SYSTEM_NS, "get", "configmap", CONFIG_MAP, "-o", "json")
        cm = json.loads(raw)
        original = cm["data"]["satin.yaml"]
        cfg = yaml.safe_load(original)
        mutate(cfg)
        self._apply_config(yaml.safe_dump(cfg, allow_unicode=True))
        return lambda: self._apply_config(original)

    def _apply_config(self, text: str) -> None:
        patch = json.dumps({"data": {"satin.yaml": text}})
        self.kube.run(
            "-n", SYSTEM_NS, "patch", "configmap", CONFIG_MAP, "--type=merge", "-p", patch
        )
        self.kube.run("-n", SYSTEM_NS, "rollout", "restart", f"deploy/{CMA_DEPLOY}")
        self.kube.run(
            "-n",
            SYSTEM_NS,
            "rollout",
            "status",
            f"deploy/{CMA_DEPLOY}",
            "--timeout=180s",
            timeout=200,
        )


# ---------------------------------------------------------------- 10.2 基本
def tool_in_sandbox(c: Ctx) -> ScenarioResult:
    r = c.result("tool_in_sandbox")
    s = c.session()
    s.send("bashで `cat /etc/satin-image-id` を実行し、出力された値をそのまま答えてください。")
    evs = s.wait_turn()
    out = tool_output(evs) + "\n" + agent_text(evs)
    r.passed = contains(out, c.args.expected_image_id)
    verb = "含まれた" if r.passed else "含まれない"
    r.observed = f"イメージ識別子 {c.args.expected_image_id!r} が結果に{verb}"
    r.evidence = {
        "session": s.id,
        "tool_result_events": event_ids(evs, *TOOL_RESULT_TYPES),
        "sandbox_mode": c.kube.mode(s.id),
    }
    return r


def skills(c: Ctx) -> ScenarioResult:
    """配布済みSkill `container-probe` を使うよう依頼する。モデルごとにこのシナリオを実行する。"""
    r = c.result("skills")
    s = c.session()
    s.send("container-probe のSkillを使って、このコンテナの識別子を報告してください。")
    evs = s.wait_turn()
    text, tools = agent_text(evs), tool_output(evs)
    read_skill = (
        "SKILL.md" in json.dumps(evs, ensure_ascii=False, default=str) or "container-probe" in tools
    )
    mentions = contains(text + tools, c.args.expected_image_id)
    jev = jev_mentions(text, c.args.expected_image_id)
    r.passed = mentions and read_skill
    r.observed = (
        f"Skillのファイルを読んだ={read_skill}、識別子を答えた={mentions}（Jev noul={jev:.2f}）"
    )
    r.evidence = {
        "session": s.id,
        "jev_noul": jev,
        "tool_use_events": event_ids(evs, "agent.tool_use"),
    }
    return r


def mcp(c: Ctx) -> ScenarioResult:
    r = c.result("mcp")
    if not c.args.mcp_url:
        r.skipped = (
            "HTTPのMCPサーバが設定されていない（--mcp-url と、そのMCPをAgentへ配布する必要がある）"
        )
        return r
    s = c.session()
    s.send("利用できるMCPツールを1つ呼び出して、結果を一言で報告してください。")
    evs = s.wait_turn()
    used = event_ids(evs, "agent.mcp_tool_use")
    r.passed = bool(used)
    r.observed = f"MCPツールの呼び出し {len(used)} 件（Sandboxを経由せず、Anthropic側で実行）"
    r.evidence = {"session": s.id, "mcp_tool_use_events": used}
    return r


def secrets_hidden(c: Ctx) -> ScenarioResult:
    r = c.result("secrets_hidden")
    s = c.session()
    s.send(
        "bashで `env; echo ---; cat /proc/1/environ | tr '\\0' '\\n'; echo ---; ls /run/satin` "
        "を実行し、出力をそのまま貼ってください。"
    )
    evs = s.wait_turn()
    out = tool_output(evs) + "\n" + agent_text(evs)
    sandbox = c.kube.sandbox(s.id)
    sname = sandbox["metadata"]["name"] if sandbox else ""
    from satin_agent.sandbox.naming import secret_name

    work_secrets = c.kube.secret_values(secret_name(s.id))
    leaked = none_of(out, c.secrets + work_secrets)
    r.passed = not leaked
    r.observed = (
        "APIキー・environment key・work secretの値は出力に含まれない"
        if r.passed
        else f"{len(leaked)} 件の秘密の値が出力に含まれた"
    )
    r.evidence = {
        "session": s.id,
        "sandbox": sname,
        "tool_result_events": event_ids(evs, *TOOL_RESULT_TYPES),
        "note": "/run/satin/ のファイル名は見える（内容は環境変数ではなくファイル）",
    }
    return r


def egress_blocked(c: Ctx) -> ScenarioResult:
    r = c.result("egress_blocked")
    s = c.session()
    s.send(
        "bashで `curl -sS -m 10 -o /dev/null -w '%{http_code}' https://example.com; echo; "
        "curl -sS -m 10 -o /dev/null -w '%{http_code}' https://api.anthropic.com; echo` "
        "を実行し、出力をそのまま貼ってください。"
    )
    evs = s.wait_turn()
    out = tool_output(evs)
    blocked = (
        "000" in out.split("\n")[0] or "timed out" in out.lower() or "Could not resolve" in out
    )
    r.passed = blocked
    r.observed = f"example.comへの通信は{'失敗した' if blocked else '成功してしまった'}"
    r.evidence = {"session": s.id, "tool_output_head": out[:300]}
    return r


# ---------------------------------------------------------------- 10.3 ライフサイクル
def resume_workspace(c: Ctx) -> ScenarioResult:
    r = c.result("resume_workspace")
    s = c.session()
    marker = f"note-{int(time.time())}"
    s.send(f"bashで `echo {marker} > /workspace/note.txt` を実行してください。")
    s.wait_turn()
    stopped = c.kube.wait_mode(s.id, "Suspended", timeout=180)
    s.send("bashで `cat /workspace/note.txt` を実行し、出力をそのまま答えてください。")
    evs = s.wait_turn()
    ok = contains(tool_output(evs) + agent_text(evs), marker)
    r.passed = stopped and ok
    r.observed = f"idle後にSandboxは停止={stopped}、再開後にnote.txtを読めた={ok}"
    r.evidence = {"session": s.id, "marker": marker}
    return r


def retention_new_workspace(c: Ctx) -> ScenarioResult:
    r = c.result("retention_new_workspace")
    restore = c.patch_config(lambda cfg: cfg["sandbox"].__setitem__("retention_seconds", 10))
    try:
        s = c.session()
        s.send(
            "bashで `echo old > /workspace/note.txt; cat /workspace/note.txt` を実行してください。"
        )
        s.wait_turn()
        c.kube.wait_mode(s.id, "Suspended", timeout=180)
        time.sleep(15)  # 保持期限（10秒）を過ぎるのを待つ
        s.send(
            "bashで `ls /workspace; cat /workspace/note.txt` を実行し、"
            "出力をそのまま答えてください。"
        )
        try:
            evs = s.wait_turn()
        except TimeoutError:
            logs = c.kube.logs(CMA_DEPLOY, since="15m")
            r.evidence = {
                "session": s.id,
                "orchestrator_log": [
                    ln[:300]
                    for ln in logs.splitlines()
                    if s.id in ln or "Traceback" in ln or "Error" in ln
                ][-15:],
            }
            r.observed = "期限切れ後の2回目のturnが終わらなかった（証拠: オーケストレータのログ）"
            return r
        out = tool_output(evs)
        fresh = "note.txt" not in out.replace("cat:", "") or "No such file" in out
        sb = c.kube.sandbox(s.id)
        gen = sb["metadata"]["annotations"].get("satin.dev/generation") if sb else None
        r.passed = fresh and gen == "2"
        shown = "ない" if fresh else "ある"
        r.observed = f"古いワークスペースは引き継がれず（世代={gen}）、note.txtは{shown}"
        r.evidence = {"session": s.id, "tool_output_head": out[:200]}
    finally:
        restore()
    return r


def parallel_sessions(c: Ctx) -> ScenarioResult:
    r = c.result("parallel_sessions")
    a, b = c.session(), c.session()
    t0 = time.time()
    for s in (a, b):
        s.send("bashで `sleep 30; echo done` を実行して、終わったら「完了」と答えてください。")
    both_running = False
    deadline = time.time() + 120
    while time.time() < deadline:
        if c.kube.mode(a.id) == "Running" and c.kube.mode(b.id) == "Running":
            both_running = True
            break
        time.sleep(2)
    times = {}
    for name, s in (("a", a), ("b", b)):
        s.wait_turn(timeout=240)
        times[name] = round(time.time() - t0, 1)
    r.passed = both_running and max(times.values()) < 120
    r.observed = f"2つのSandboxが同時にRunning={both_running}、応答までの秒数={times}"
    r.evidence = {"sessions": [a.id, b.id], "seconds": times}
    return r


def bad_image(c: Ctx) -> ScenarioResult:
    r = c.result("bad_image")
    restore = c.patch_config(
        lambda cfg: cfg["sandbox"].update(
            image="us-central1-docker.pkg.dev/nnyn-dev/satin-agent/does-not-exist:0",
            ready_timeout=60,
        )
    )
    try:
        s = c.session()
        s.send("bashで `echo hi` を実行してください。")
        t0 = time.time()
        stopped = c.kube.wait_mode(s.id, "Suspended", timeout=150)
        took = round(time.time() - t0, 1)
        logs = c.kube.logs(CMA_DEPLOY, since="5m")
        line = next((ln for ln in logs.splitlines() if "起動に失敗" in ln or "failed" in ln), "")
        r.passed = stopped
        r.observed = f"起動の失敗後、Sandboxは停止={stopped}（{took}秒）"
        r.evidence = {"session": s.id, "orchestrator_log": line[:300]}
        with_interrupt = None
        try:
            s.interrupt()
            with_interrupt = True
        except Exception:
            pass
        r.evidence["interrupted_after"] = with_interrupt
    finally:
        restore()
    return r


# ---------------------------------------------------------------- 10.4 障害
def _published_state(c: Ctx, session_id: str) -> dict[str, Any] | None:
    out = c.kube.run(
        "-n",
        c.args.namespace,
        "get",
        "configmap",
        "satin-sandbox-states",
        "-o",
        "json",
        check=False,
    )
    raw = json.loads(out).get("data", {}).get(session_id) if out else None
    return json.loads(raw) if raw else None


def kill_pod_during_tool(c: Ctx) -> ScenarioResult:
    """ツールが実行されている間にSandboxのPodを削除する。

    合格: セッションが完了する（ツール結果と返答が記録される）か、オーケストレータが失敗を
    公開する（Botがturnを失敗にして中断できる）。黙って放置されないこと。
    """
    r = c.result("kill_pod_during_tool")
    s = c.session()
    s.send("bashで `sleep 60; echo done` を実行して、結果を報告してください。")
    started = False
    for _ in range(120):  # ツールの呼び出しが出て、Podが動いていること
        evs = s.events()
        if any(e["type"] == "agent.tool_use" for e in evs) and c.kube.pods(s.id):
            started = True
            break
        time.sleep(2)
    time.sleep(10)  # ワーカーがツールを実行し始めるのを待つ
    pods = c.kube.pods(s.id)
    for p in pods:
        c.kube.delete_pod(p)
    outcome, evs = "", []
    deadline = time.time() + 420
    while time.time() < deadline:
        evs = s.events()[s._mark :]
        if turn_finished(evs):
            outcome = "completed"
            break
        st = _published_state(c, s.id)
        if st and st.get("state") == "failed":
            outcome = "failure_published"
            break
        time.sleep(5)
    results = [e for e in evs if e["type"] in TOOL_RESULT_TYPES]
    r.passed = started and outcome in ("completed", "failure_published")
    if outcome == "completed":
        r.observed = (
            f"Pod削除後に回復し、ツール結果{len(results)}件・"
            f"返答{'あり' if agent_text(evs) else 'なし'}でturnが完了した"
        )
    elif outcome == "failure_published":
        r.observed = (
            "自動では回復せず、オーケストレータが失敗を公開した（Botがturnを失敗にして中断する）"
        )
    else:
        r.observed = "セッションが黙って放置された（完了も失敗の公開もない）"
    r.evidence = {
        "session": s.id,
        "deleted_pods": pods,
        "tool_started_before_delete": started,
        "outcome": outcome,
        "published_state": _published_state(c, s.id),
        "tool_result_events": [e["id"] for e in results],
    }
    if not r.passed:
        logs = c.kube.logs(CMA_DEPLOY, since="20m")
        keep = ("異常終了", "再実行", "ERROR", "Traceback", "WARNING")
        r.evidence["orchestrator_log"] = [
            ln[:300] for ln in logs.splitlines() if s.id in ln or any(k in ln for k in keep)
        ][-20:]
    return r


def restart_orchestrator_during_tool(c: Ctx) -> ScenarioResult:
    r = c.result("restart_orchestrator_during_tool")
    s = c.session()
    s.send("bashで `sleep 60; echo done` を実行して、結果を報告してください。")
    for _ in range(60):
        if c.kube.pods(s.id):
            break
        time.sleep(2)
    time.sleep(10)
    c.kube.run("-n", SYSTEM_NS, "rollout", "restart", f"deploy/{CMA_DEPLOY}")
    evs = s.wait_turn(timeout=300)
    completed = contains(tool_output(evs) + agent_text(evs), "done")
    stopped = c.kube.wait_mode(s.id, "Suspended", timeout=180)
    r.passed = completed and stopped
    r.observed = (
        f"ツールの実行は完了={completed}、再起動後のオーケストレータがSandboxを停止={stopped}"
    )
    r.evidence = {"session": s.id}
    return r


def _long_tool(c: Ctx, name: str, prompt: str) -> ScenarioResult:
    r = c.result(name)
    s = c.session()
    s.send(prompt)
    evs = s.wait_turn(timeout=600)
    out = tool_output(evs) + "\n" + agent_text(evs)
    calls = [e for e in evs if e["type"] == "agent.tool_use"]
    specified = [(e.get("input") or {}).get("timeout_ms") for e in calls]
    timed_out = "timed out" in out
    r.passed = "long-done" in out and not timed_out
    r.observed = (
        f"150秒を超えるツール（sleep 200）が{'最後まで実行された' if r.passed else '打ち切られた'}"
        f"（エージェントが指定したtimeout_ms={specified}）"
    )
    r.evidence = {
        "session": s.id,
        "timeout_ms_specified": specified,
        "tool_result_events": event_ids(evs, *TOOL_RESULT_TYPES),
    }
    return r


def long_tool_default_timeout(c: Ctx) -> ScenarioResult:
    """`timeout_ms`を指定させずに、旧上限（150秒）を超えるツールを実行する。"""
    return _long_tool(
        c,
        "long_tool_default_timeout",
        "bashで `sleep 200; echo long-done` を実行して、結果を報告してください。"
        "bashのtimeout_msは指定しないでください。",
    )


def long_tool_explicit_timeout(c: Ctx) -> ScenarioResult:
    """`timeout_ms`を長く指定して、旧上限（150秒）を超えるツールを実行する。"""
    return _long_tool(
        c,
        "long_tool_explicit_timeout",
        "bashで `sleep 200; echo long-done` を、timeout_ms に 600000 を指定して実行し、"
        "結果を報告してください。",
    )


def long_tool_25min(c: Ctx) -> ScenarioResult:
    """25分のツール（時間がかかるので、既定の実行には含めない。`--only`で指定する）。"""
    r = c.result("long_tool_25min")
    s = c.session()
    s.send(
        "bashで `sleep 1500; echo long-done` を、timeout_ms に 1800000 を指定して実行し、"
        "結果を報告してください。"
    )
    t0 = time.time()
    evs = s.wait_turn(timeout=2100, poll=15)
    took = round(time.time() - t0)
    out = tool_output(evs) + "\n" + agent_text(evs)
    r.passed = "long-done" in out and "timed out" not in out
    r.observed = f"25分のツールが{'最後まで実行された' if r.passed else '打ち切られた'}（{took}秒）"
    r.evidence = {"session": s.id, "seconds": took}
    return r


SLOW = {"long_tool_25min"}

SCENARIOS: dict[str, Callable[[Ctx], ScenarioResult]] = {
    "tool_in_sandbox": tool_in_sandbox,
    "skills": skills,
    "mcp": mcp,
    "secrets_hidden": secrets_hidden,
    "egress_blocked": egress_blocked,
    "resume_workspace": resume_workspace,
    "retention_new_workspace": retention_new_workspace,
    "parallel_sessions": parallel_sessions,
    "bad_image": bad_image,
    "kill_pod_during_tool": kill_pod_during_tool,
    "restart_orchestrator_during_tool": restart_orchestrator_during_tool,
    "long_tool_default_timeout": long_tool_default_timeout,
    "long_tool_explicit_timeout": long_tool_explicit_timeout,
    "long_tool_25min": long_tool_25min,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--context", required=True, help="kubectlのコンテキスト（必須。誤った環境への実行を避ける）"
    )
    p.add_argument("--namespace", default="satin-sandboxes")
    p.add_argument("--expected-image-id", required=True)
    p.add_argument("--agent-id", default="")
    p.add_argument("--model", default="claude-haiku-5-5", help="記録用（Agentのモデル）")
    p.add_argument(
        "--state", default=".satin-state-verify.json", help="検証用Agent（bashの確認なし）のID"
    )
    p.add_argument("--only", default="", help="カンマ区切りのシナリオ名")
    p.add_argument("--mcp-url", default="")
    p.add_argument("--label", default="cma")
    p.add_argument("--list", action="store_true")
    args = p.parse_args(argv)
    if args.list:
        print("\n".join(SCENARIOS))
        return 0
    names = [n for n in args.only.split(",") if n] or [n for n in SCENARIOS if n not in SLOW]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenario: {unknown}", file=sys.stderr)
        return 2
    ctx = Ctx(args)
    rec = Recorder(args.label, redact=ctx.secrets)
    lock = threading.Lock()
    for n in names:
        try:
            res = SCENARIOS[n](ctx)
        except Exception as e:  # noqa: BLE001 - シナリオの失敗として記録する
            res = ctx.result(n)
            res.observed = f"シナリオの実行に失敗: {type(e).__name__}: {e}"
        with lock:
            rec.add(res)
    path = rec.save()
    print(f"saved {path}")
    return 0 if all(r.passed or r.skipped for r in rec.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
