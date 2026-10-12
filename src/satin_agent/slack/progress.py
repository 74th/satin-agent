"""進捗メッセージの組み立て（Slackのmrkdwn）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Tokyo"


def load_timezone(name: str | None) -> tzinfo:
    """設定のタイムゾーン名（IANA）。不正な名前は設定の誤りとして例外にする。"""
    return ZoneInfo(name or DEFAULT_TIMEZONE)


TOOL_MARK = {"running": "⏳", "success": "✅", "failure": "❌", "denied": "🚫", "interrupted": "⏹"}
STATUS_TEXT = {
    "preparing_sandbox": "作業環境を起動中",
    "thinking": "考え中",
    "running_tool": "実行中",
    "interrupting": "中断しています",
    "finished": "完了",
    "failed": "失敗",
    "interrupted": "中断",
}
MAX_TOOLS_SHOWN = 15


@dataclass
class ToolView:
    tool_use_id: str
    name: str
    summary: str
    status: str = "running"  # running | success | failure | denied | interrupted


@dataclass
class ProgressView:
    status: str = "thinking"
    tools: list[ToolView] = field(default_factory=list)
    appended: int = 0  # 実行中に追加した指示の件数
    updated_at: float = 0.0
    note: str = ""
    tz: tzinfo = field(default_factory=lambda: load_timezone(None))

    def render(self) -> str:
        head = STATUS_TEXT.get(self.status, self.status)
        lines = [f"*{head}*"]
        shown = self.tools[-MAX_TOOLS_SHOWN:]
        if len(self.tools) > len(shown):
            lines.append(f"（先頭の{len(self.tools) - len(shown)}件を省略）")
        for t in shown:
            lines.append(f"{TOOL_MARK.get(t.status, '•')} `{t.name}` {t.summary}".rstrip())
        if self.appended:
            lines.append(f"追加の指示: {self.appended}件")
        if self.note:
            lines.append(self.note)
        updated = datetime.fromtimestamp(self.updated_at, self.tz).strftime("%H:%M:%S")
        lines.append(f"_最終更新: {updated}_")
        return "\n".join(lines)
