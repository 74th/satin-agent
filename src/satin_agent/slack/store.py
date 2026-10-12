"""Botの管理情報（SQLite）。スレッド → セッション、turnの状態、最後に処理したイベントID。

会話の履歴はManaged Agentsのセッション自体が持つので、ここには持たない。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any

RUNNING = "running"


@dataclass
class TurnRecord:
    turn_id: str
    conv_key: str
    session_id: str
    status: str  # running | finished | failed | interrupted
    progress_ref: str = ""
    last_event_id: str = ""
    started_at: float = 0.0
    state: dict[str, Any] = field(default_factory=dict)  # ツール一覧・件数・保留中の追加指示など


SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  conv_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_email TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS turns (
  turn_id TEXT PRIMARY KEY, conv_key TEXT NOT NULL, session_id TEXT NOT NULL,
  status TEXT NOT NULL, progress_ref TEXT NOT NULL DEFAULT '',
  last_event_id TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL, state TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS turns_status ON turns(status);
"""


class BotStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- threads ---
    def get_session(self, conv_key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT session_id FROM threads WHERE conv_key=?", (conv_key,)
            ).fetchone()
        return row["session_id"] if row else None

    def save_thread(self, conv_key: str, session_id: str, email: str, now: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO threads(conv_key, session_id, user_email, created_at)"
                " VALUES(?,?,?,?)",
                (conv_key, session_id, email, now),
            )
            self._db.commit()

    # --- turns ---
    def save_turn(self, t: TurnRecord) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO turns(turn_id, conv_key, session_id, status, progress_ref,"
                " last_event_id, started_at, state) VALUES(?,?,?,?,?,?,?,?)",
                (
                    t.turn_id,
                    t.conv_key,
                    t.session_id,
                    t.status,
                    t.progress_ref,
                    t.last_event_id,
                    t.started_at,
                    json.dumps(t.state, ensure_ascii=False),
                ),
            )
            self._db.commit()

    def running_turns(self) -> list[TurnRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM turns WHERE status=? ORDER BY started_at", (RUNNING,)
            ).fetchall()
        return [self._turn(r) for r in rows]

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        return self._turn(r) if r else None

    @staticmethod
    def _turn(r: sqlite3.Row) -> TurnRecord:
        return TurnRecord(
            r["turn_id"],
            r["conv_key"],
            r["session_id"],
            r["status"],
            r["progress_ref"],
            r["last_event_id"],
            r["started_at"],
            json.loads(r["state"]),
        )
