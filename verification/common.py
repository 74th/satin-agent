"""検証スクリプトの共通部品: Managed Agentsのセッション操作、kubectl、判定、結果の記録。"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from satin_agent.sandbox.naming import LABEL_SESSION, session_hash

RESULTS_DIR = Path("results")
# Sandbox内のワーカーは`user.tool_result`で結果を返す。サーバ側の結果（中断・MCP）は`agent.*`。
TOOL_RESULT_TYPES = ("user.tool_result", "agent.tool_result", "agent.mcp_tool_result")


# --- 判定 ---
def contains(text: str, value: str) -> bool:
    return value in text


def none_of(text: str, values: list[str]) -> list[str]:
    """textに含まれてしまった値のうち、空でないものを返す（秘密の漏えいの確認）。"""
    return [v for v in values if v and v in text]


def jev_mentions(text: str, value: str) -> float:
    from tests.live import jev

    return jev.mentions_value(text, value)


def jev_explains(text: str, what: str) -> float:
    from tests.live import jev

    return jev.explains(text, what)


def jev_usage() -> list[tuple[int, int]]:
    from tests.live import jev

    return list(jev.usage_log)


# --- 結果の記録 ---
@dataclass
class ScenarioResult:
    name: str
    passed: bool = False
    skipped: str = ""  # 実施しなかった理由
    model: str = ""
    observed: str = ""  # 観察した振る舞い
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "result": "skipped" if self.skipped else ("pass" if self.passed else "fail"),
            "skipped_reason": self.skipped,
            "model": self.model,
            "observed": self.observed,
            "evidence": self.evidence,
        }


class Recorder:
    def __init__(self, label: str, redact: list[str] | None = None) -> None:
        self.label = label
        self.results: list[ScenarioResult] = []
        self._redact = [r for r in (redact or []) if r]

    def add(self, r: ScenarioResult) -> None:
        self.results.append(r)
        status = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
        print(f"[{status}] {r.name}: {r.observed or r.skipped}")

    def save(self) -> Path:
        RESULTS_DIR.mkdir(exist_ok=True)
        path = RESULTS_DIR / f"{self.label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        text = json.dumps(
            {
                "label": self.label,
                "date": time.strftime("%Y-%m-%d"),
                "jev_usage": jev_usage_safe(),
                "results": [r.as_dict() for r in self.results],
            },
            ensure_ascii=False,
            indent=2,
        )
        for secret in self._redact:  # 念のため、秘密の値が記録に入らないようにする
            text = text.replace(secret, "<redacted>")
        path.write_text(text + "\n")
        return path


def jev_usage_safe() -> list[tuple[int, int]]:
    try:
        return jev_usage()
    except Exception:
        return []


# --- Managed Agentsのセッション ---
class Ses:
    def __init__(self, client: Any, agent_id: str, environment_id: str) -> None:
        self.c = client
        self.id = client.beta.sessions.create(agent=agent_id, environment_id=environment_id).id
        self._mark = 0  # 次のturnのイベントの開始位置

    def events(self) -> list[dict[str, Any]]:
        return [e.model_dump() for e in self.c.beta.sessions.events.list(self.id)]

    def send(self, text: str) -> None:
        self._mark = len(self.events())
        self.c.beta.sessions.events.send(
            self.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
        )

    def interrupt(self) -> None:
        self.c.beta.sessions.events.send(self.id, events=[{"type": "user.interrupt"}])

    def wait_turn(self, timeout: float = 300.0, poll: float = 3.0) -> list[dict[str, Any]]:
        """送ったメッセージ以降で、end_turnのidleになるまで待つ。そのturnのイベントを返す。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            evs = self.events()[self._mark :]
            if turn_finished(evs):
                return evs
            time.sleep(poll)
        raise TimeoutError(f"session {self.id}: turnが{timeout}秒以内に終わりませんでした")


def turn_finished(evs: list[dict[str, Any]]) -> bool:
    started = any(e["type"] == "user.message" for e in evs)
    for e in reversed(evs):
        if e["type"] == "session.status_idle":
            return started and (e.get("stop_reason") or {}).get("type") != "requires_action"
        if e["type"] == "session.status_running":
            return False
    return False


def agent_text(evs: list[dict[str, Any]]) -> str:
    out = []
    for e in evs:
        if e["type"] == "agent.message":
            out.extend(b.get("text", "") for b in e.get("content", []) if b.get("type") == "text")
    return "\n".join(out)


def tool_output(evs: list[dict[str, Any]]) -> str:
    out = []
    for e in evs:
        if e["type"] in TOOL_RESULT_TYPES:
            c = e.get("content") or []
            out.extend(b.get("text", "") for b in c if isinstance(b, dict))
    return "\n".join(out)


def event_ids(evs: list[dict[str, Any]], *types: str) -> list[str]:
    return [e["id"] for e in evs if e["type"] in types]


# --- kubectl ---
class Kube:
    def __init__(self, context: str, namespace: str) -> None:
        self.ctx, self.ns = context, namespace

    def run(self, *args: str, check: bool = True, timeout: float = 60.0) -> str:
        r = subprocess.run(
            ["kubectl", "--context", self.ctx, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and r.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def sandbox(self, session_id: str) -> dict[str, Any] | None:
        out = self.run(
            "-n",
            self.ns,
            "get",
            "sandbox",
            "-l",
            f"{LABEL_SESSION}={session_hash(session_id)}",
            "-o",
            "json",
        )
        items = json.loads(out).get("items", [])
        return (
            max(
                items,
                key=lambda o: int(o["metadata"]["annotations"].get("satin.dev/generation", "1")),
            )
            if items
            else None
        )

    def mode(self, session_id: str) -> str | None:
        sb = self.sandbox(session_id)
        return sb["spec"].get("operatingMode") if sb else None

    def wait_mode(self, session_id: str, want: str, timeout: float = 180.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.mode(session_id) == want:
                return True
            time.sleep(3)
        return False

    def pods(self, session_id: str) -> list[str]:
        sb = self.sandbox(session_id)
        if not sb:
            return []
        sel = (sb.get("status") or {}).get("selector")
        if not sel:
            return []
        out = self.run("-n", self.ns, "get", "pod", "-l", sel, "-o", "name", check=False)
        return [x for x in out.split() if x]

    def delete_pod(self, name: str) -> None:
        self.run("-n", self.ns, "delete", name, "--wait=false")

    def secret_values(
        self, name: str, keys: tuple[str, ...] = ("work_secret", "work_order")
    ) -> list[str]:
        """Secretの秘密の値（漏えい確認のため、比較にだけ使う。出力・記録はしない）。

        session_idなど、環境変数に出てよい識別子は含めない。
        """
        import base64

        out = self.run("-n", self.ns, "get", "secret", name, "-o", "json", check=False)
        if not out:
            return []
        data = json.loads(out).get("data", {})
        return [base64.b64decode(v).decode(errors="replace") for k, v in data.items() if k in keys]

    def logs(self, deploy: str, since: str = "10m", namespace: str = "satin-system") -> str:
        return self.run(
            "-n",
            namespace,
            "logs",
            f"deploy/{deploy}",
            f"--since={since}",
            "--all-containers=true",
            check=False,
            timeout=120,
        )
