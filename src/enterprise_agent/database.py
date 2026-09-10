from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import utc_now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
  department TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '', content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
  priority TEXT NOT NULL, status TEXT NOT NULL, creator_id TEXT NOT NULL,
  owner_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, role TEXT NOT NULL,
  status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  pending_approval_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tool_name TEXT NOT NULL,
  arguments_json TEXT NOT NULL, requested_by TEXT NOT NULL,
  status TEXT NOT NULL, reviewer_id TEXT, comment TEXT,
  created_at TEXT NOT NULL, decided_at TEXT,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS traces (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT NOT NULL,
  session_id TEXT NOT NULL, event TEXT NOT NULL, payload_json TEXT NOT NULL,
  duration_ms REAL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind);
CREATE INDEX IF NOT EXISTS idx_tickets_creator ON tickets(creator_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_traces_trace ON traces(trace_id, id);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,department) VALUES(?,?,?,?)",
                [
                    ("u-1001", "张三", "employee", "研发部"),
                    ("u-2001", "李经理", "manager", "研发部"),
                    ("u-9001", "系统管理员", "admin", "IT"),
                ],
            )

    def seed_documents(self, source: Path) -> int:
        items = json.loads(source.read_text(encoding="utf-8"))
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO documents(id,kind,title,tags,content) VALUES(:id,:kind,:title,:tags,:content)
                ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,title=excluded.title,
                tags=excluded.tags,content=excluded.content""",
                items,
            )
        return len(items)

    def create_session(self, user_id: str, role: str) -> str:
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO sessions(id,user_id,role,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (session_id, user_id, role, "active", now, now),
            )
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def update_session(self, session_id: str, **values: Any) -> None:
        allowed = {"status", "summary", "pending_approval_id"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.connect() as connection:
            connection.execute(f"UPDATE sessions SET {assignments} WHERE id=?", (*values.values(), session_id))

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                (session_id, role, content, utc_now()),
            )

    def messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT role,content,created_at FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def create_approval(self, session_id: str, tool_name: str, arguments: dict[str, Any], user_id: str) -> str:
        approval_id = f"apr_{uuid.uuid4().hex[:16]}"
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO approvals(id,session_id,tool_name,arguments_json,requested_by,status,created_at)
                VALUES(?,?,?,?,?,'pending',?)""",
                (
                    approval_id,
                    session_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    user_id,
                    utc_now(),
                ),
            )
        self.update_session(session_id, status="waiting_approval", pending_approval_id=approval_id)
        return approval_id

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["arguments"] = json.loads(item.pop("arguments_json"))
        return item

    def decide_approval(self, approval_id: str, approved: bool, reviewer: str, comment: str) -> None:
        status = "approved" if approved else "rejected"
        changed = self.execute(
            """UPDATE approvals SET status=?,reviewer_id=?,comment=?,decided_at=?
            WHERE id=? AND status='pending'""",
            (status, reviewer, comment, utc_now(), approval_id),
        )
        if changed != 1:
            raise ValueError("审批不存在或已经处理")

    def trace(
        self,
        trace_id: str,
        session_id: str,
        event: str,
        payload: dict[str, Any],
        duration_ms: float | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO traces(trace_id,session_id,event,payload_json,duration_ms,created_at) VALUES(?,?,?,?,?,?)",
                (
                    trace_id,
                    session_id,
                    event,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    duration_ms,
                    utc_now(),
                ),
            )
