"""Jev（TypeSafe System One）による応答判定。noul値（0〜1）を返す。liveのテストと検証だけが使う。"""

from __future__ import annotations

from typesafe_sdk import TypeSafeClient

from satin_agent.config import jev_api_key

THRESHOLD = 0.3  # noul がこの値より大きければ「述べている」と判定

_client: TypeSafeClient | None = None
usage_log: list[tuple[int, int]] = []


def client() -> TypeSafeClient:
    global _client
    if _client is None:
        _client = TypeSafeClient(api_key=jev_api_key())
    return _client


def noul(state: str, question: str) -> float:
    """stateに対する真偽質問の確率。usageはusage_logに残す。"""
    res = client().system_one(
        state=state,
        questions={"q": {"type": "noul", "instructions": question}},
    )
    u = getattr(res, "usage", None)
    if u is not None:
        usage_log.append((u.input_tokens, u.output_tokens))
    return float(res.answers["q"].noul)  # pyrefly: ignore[missing-attribute]


def mentions_value(response_text: str, value: str) -> float:
    return noul(response_text, f"この文章は、値「{value}」を(そのまま)述べているか?")


def explains(response_text: str, what: str) -> float:
    return noul(response_text, f"この文章は、{what}を説明しているか?")


def passes(prob: float) -> bool:
    return prob > THRESHOLD
