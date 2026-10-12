"""bashの一括取り出しの検出（silk-agentの`guards/bulk_files.py`の移植）。

Model Armorの`pi_and_jailbreak`は通常のシェルコマンドも誤検知するため、出力には使わない。
代わりに、ディレクトリ全体を取り出す・固めるコマンド（アーカイブ、`cat *`、`find -exec cat`、
`xargs cat`、全行に一致する再帰検索）だけを、bashのツール承認で拒否する。
意図的に粗い判定で、多数のファイルを読む手段をすべて捕まえようとはしない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_READERS = r"(?:cat|head|tail|less|more|base64|xxd|od|strings|hexdump|nl|tac)"
_SEGMENT = r"[^|;&\n]*"  # stays within one simple command

_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "ディレクトリをアーカイブにまとめる",
        re.compile(
            rf"\btar\b{_SEGMENT}\s(?:--create\b|-?[a-zA-Z]*c[a-zA-Z]*(?=\s|$))"
            rf"|\b(?:zip|jar)\b{_SEGMENT}\s-[a-zA-Z]*r"
            rf"|\b7za?\s+a\b|\brar\s+a\b|\bcpio\b{_SEGMENT}\s-[a-zA-Z]*o"
        ),
    ),
    (
        "ワイルドカードで全ファイルの中身を出力する",
        re.compile(rf"\b{_READERS}\b{_SEGMENT}(?:^|[\s/'\"])\*"),
    ),
    (
        "find で見つけた全ファイルの中身を出力する",
        re.compile(rf"\bfind\b{_SEGMENT}-exec(?:dir)?\s+{_READERS}\b"),
    ),
    (
        "xargs で全ファイルの中身を出力する",
        re.compile(rf"\bxargs\b(?:\s+-\S+)*\s+{_READERS}\b"),
    ),
    (
        "ループで全ファイルの中身を出力する",
        re.compile(rf"\bfor\s+\w+\s+in\b[^;\n]*[*`$][^\n]*\bdo\b[^\n]*\b{_READERS}\b"),
    ),
    (
        "再帰検索で全ファイルの全行を出力する",
        # The search pattern (first argument after the options) matches every line.
        re.compile(
            rf"(?:\bgrep\b(?={_SEGMENT}\s-[a-zA-Z]*[rR])|\brg\b)(?:\s+-\S+)*\s+"
            r"(?:''|\"\"|'\.'|\"\.\"|'\^'|\"\^\"|\^|'\.\*'|\"\.\*\"|\.\*|\.)(?:\s|$)"
        ),
    ),
]


@dataclass(frozen=True)
class BashVerdict:
    allow: bool
    reason: str = ""


def find_bulk_file_access(command: str) -> str | None:
    """拒否する理由。実行してよいならNone。"""
    for reason, pattern in _RULES:
        if pattern.search(command):
            return reason
    return None


def check_bash(command: str) -> BashVerdict:
    """bashコマンドの実行前判定。拒否のreasonはエージェントへ返す文言になる。"""
    reason = find_bulk_file_access(command)
    if reason is None:
        return BashVerdict(True)
    return BashVerdict(
        False, f"ディレクトリ内の全ファイルを一括で取り出すコマンドは実行できません（{reason}）。"
    )
