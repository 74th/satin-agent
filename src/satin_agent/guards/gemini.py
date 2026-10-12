"""Geminiによる入力ガード（silk-agentの`guards/gemini.py`の移植）。

判定は`allow` / `block` / `confirm`と、利用者へ表示してよい理由。Geminiの呼び出しが失敗したときは
既定でブロックし、`fail_open`のときだけ通す。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from satin_agent.guards.config import GeminiConfig

log = logging.getLogger(__name__)

Decision = Literal["allow", "block", "confirm"]
_DECISIONS: dict[str, Decision] = {
    "allow": "allow",
    "block": "block",
    "confirm": "confirm",
    "review": "confirm",  # silk-agent互換
}
DEFAULT_CRITERIA_FILE = Path(__file__).with_name("input_guard.md")
FAILURE_REASON = (
    "入力を確認できなかったため、送信を見送りました。しばらくしてからもう一度お試しください。"
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "block", "confirm"]},
        "reason": {"type": "string"},
    },
    "required": ["decision"],
}


class GeminiClient(Protocol):
    """プロンプトを渡してJSON（`{"decision": ..., "reason": ...}`）のdictを得る。失敗は例外。"""

    async def generate_json(self, prompt: str) -> dict: ...


@dataclass(frozen=True)
class GuardVerdict:
    decision: Decision
    reason: str = ""
    # Geminiの呼び出しや応答の解釈に失敗した（decisionは設定による代替値）
    failed: bool = False


def load_criteria(path: str | Path | None) -> str:
    return Path(path or DEFAULT_CRITERIA_FILE).read_text(encoding="utf-8")


class InputGuard:
    def __init__(
        self,
        client: GeminiClient,
        criteria: str,
        *,
        history_size: int = 5,
        fail_open: bool = False,
    ) -> None:
        self._client = client
        self._criteria = criteria
        self._history_size = history_size
        self._fail_open = fail_open

    @classmethod
    def from_config(cls, client: GeminiClient, config: GeminiConfig) -> InputGuard:
        return cls(
            client,
            load_criteria(config.criteria_file),
            history_size=config.history_size,
            fail_open=config.fail_open,
        )

    def build_prompt(self, text: str, context: list[str]) -> str:
        lines = [self._criteria, "", "<previous_messages>"]
        if self._history_size > 0:
            lines += [f"<message>{m}</message>" for m in context[-self._history_size :]]
        lines += ["</previous_messages>", "<message_to_review>", text, "</message_to_review>"]
        return "\n".join(lines)

    async def check(self, text: str, context: list[str]) -> GuardVerdict:
        try:
            raw = await self._client.generate_json(self.build_prompt(text, context))
            decision = _DECISIONS[str(raw["decision"]).strip().lower()]
            reason = str(raw.get("reason") or "")
        except Exception:
            log.exception("input guard failed")
            if self._fail_open:
                return GuardVerdict("allow", "", failed=True)
            return GuardVerdict("block", FAILURE_REASON, failed=True)
        return GuardVerdict(decision, reason)
