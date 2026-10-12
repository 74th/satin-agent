"""Model Armorによる出力検査（silk-agentの`guards/model_armor.py`の移植）。

表示用のテキストだけを検査・加工する。セッションの会話の内容には触れない。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, Protocol

from satin_agent.guards.config import DEFAULT_IGNORED_FILTERS, ModelArmorConfig

log = logging.getLogger(__name__)

DEFAULT_CHUNK_CHARS = 8000
BLOCKED_MESSAGE = "この出力はポリシーにより表示できません。"
FAILED_MESSAGE = "出力の検査に失敗したため、表示できません。"
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class ArmorResult:
    """Model Armorの応答を、SDKの型に依存しない形にしたもの。"""

    match_found: bool
    # 一致した個人情報以外のフィルタ名（無視するものを含む）
    matched_filters: frozenset[str] = frozenset()
    sdp_matched: bool = False
    # 個人情報のマスク後のテキスト（返らなければNone）
    masked_text: str | None = None


class ModelArmorClient(Protocol):
    """`sanitizeModelResponse`相当。失敗は例外。"""

    async def sanitize(self, text: str) -> ArmorResult: ...


@dataclass(frozen=True)
class InspectResult:
    action: Literal["show", "masked", "blocked", "failed"]
    # show: 元のテキスト、masked: マスク後のテキスト。blocked / failed: None
    text: str | None = None
    # blocked / failed: 利用者に伝える文言
    message: str | None = None
    # ログ・フック用の詳細（利用者には見せない）
    detail: str = ""


def _segments(text: str) -> list[str]:
    """段落・コードブロック単位に分ける。連結すると`text`に戻る。"""
    segments: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        current.append(line)
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.strip() == "":
            segments.append("".join(current))
            current = []
    if current:
        segments.append("".join(current))
    return segments


def _hard_split(segment: str, limit: int) -> list[str]:
    parts: list[str] = []
    buf = ""
    for line in segment.splitlines(keepends=True):
        while len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            parts.append(buf)
            buf = ""
        buf += line
    if buf:
        parts.append(buf)
    return parts


def split_text(text: str, limit: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """段落・コードブロックの境界で分ける。`"".join(result) == text`。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for seg in _segments(text):
        pieces = [seg] if len(seg) <= limit else _hard_split(seg, limit)
        for piece in pieces:
            if buf and len(buf) + len(piece) > limit:
                chunks.append(buf)
                buf = ""
            buf += piece
    if buf:
        chunks.append(buf)
    return chunks


class OutputInspector:
    def __init__(
        self,
        client: ModelArmorClient,
        *,
        ignored_filters: Collection[str] = DEFAULT_IGNORED_FILTERS,
        on_sdp_match: str = "mask",
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
    ) -> None:
        self._client = client
        self._ignored = frozenset(ignored_filters)
        self._on_sdp_match = on_sdp_match
        self._chunk_chars = chunk_chars

    @classmethod
    def from_config(cls, client: ModelArmorClient, config: ModelArmorConfig) -> OutputInspector:
        return cls(
            client,
            ignored_filters=config.ignored_filters,
            on_sdp_match=config.on_sdp_match,
            chunk_chars=config.chunk_chars,
        )

    async def inspect(self, text: str) -> InspectResult:
        if not text.strip():
            return InspectResult("show", text)
        parts: list[str] = []
        masked = False
        for chunk in split_text(text, self._chunk_chars):
            try:
                result = await self._client.sanitize(chunk)
            except Exception as e:
                log.exception("Model Armor call failed")
                return InspectResult("failed", None, FAILED_MESSAGE, f"{type(e).__name__}: {e}")
            verdict = self._interpret(result, chunk)
            if verdict.action in ("blocked", "failed"):
                return verdict
            masked = masked or verdict.action == "masked"
            parts.append(verdict.text or "")
        return InspectResult("masked" if masked else "show", "".join(parts))

    def _interpret(self, result: ArmorResult, chunk: str) -> InspectResult:
        if not result.match_found:
            return InspectResult("show", chunk)
        effective = result.matched_filters - self._ignored
        if not effective and not result.sdp_matched:
            if result.matched_filters:  # 無視する設定のフィルタだけ
                return InspectResult("show", chunk)
            # 一致とあるのにどれか分からない: 閉じる側に倒す
            return InspectResult("blocked", None, BLOCKED_MESSAGE, "unidentified filter match")
        if effective:
            return InspectResult(
                "blocked", None, BLOCKED_MESSAGE, "matched: " + ", ".join(sorted(effective))
            )
        if self._on_sdp_match == "block":
            return InspectResult("blocked", None, BLOCKED_MESSAGE, "sensitive data matched")
        if not result.masked_text:
            return InspectResult("blocked", None, BLOCKED_MESSAGE, "no masked text returned")
        return InspectResult("masked", result.masked_text, None, "sensitive data masked")
