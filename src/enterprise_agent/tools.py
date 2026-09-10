from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .database import Database
from .models import TicketPriority, utc_now
from .retrieval import HybridRetriever


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": False,
        }


class EnterpriseTools:
    def __init__(self, database: Database):
        self.database = database
        self.retriever = HybridRetriever(database)
        self.registry: dict[str, ToolSpec] = {}
        self._register_tools()

    def _add(self, name: str, description: str, parameters: dict[str, Any], handler: Callable[..., Any]) -> None:
        self.registry[name] = ToolSpec(name, description, parameters, handler)

    def _register_tools(self) -> None:
        query_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        self._add(
            "search_docs",
            "检索企业手册和操作文档",
            query_schema,
            lambda query: self.retriever.search(query, limit=4),
        )
        self._add(
            "search_policy",
            "检索企业审批和治理制度",
            query_schema,
            lambda query: self.retriever.search(query, kind="policy", limit=4),
        )
        self._add(
            "get_user",
            "按用户 ID 查询企业用户",
            {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
            self.get_user,
        )
        self._add(
            "get_ticket",
            "查询单个工单",
            {"type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"]},
            self.get_ticket,
        )
        self._add(
            "list_tickets",
            "查询工单列表",
            {
                "type": "object",
                "properties": {"status": {"type": "string"}, "creator_id": {"type": "string"}},
            },
            self.list_tickets,
        )
        self._add(
            "create_ticket",
            "创建企业运维工单；P1 必须人工审批",
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "priority": {"type": "string", "enum": [item.value for item in TicketPriority]},
                    "creator_id": {"type": "string"},
                    "owner_id": {"type": "string"},
                },
                "required": ["title", "description", "priority", "creator_id"],
            },
            self.create_ticket,
        )
        self._add(
            "update_ticket",
            "更新工单状态或负责人",
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "status": {"type": "string"},
                    "owner_id": {"type": "string"},
                },
                "required": ["ticket_id"],
            },
            self.update_ticket,
        )

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [spec.openai_schema() for spec in self.registry.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.registry:
            raise ValueError(f"未知工具: {name}")
        return self.registry[name].handler(**arguments)

    def get_user(self, user_id: str) -> dict[str, Any]:
        rows = self.database.query("SELECT id,name,role,department,active FROM users WHERE id=?", (user_id,))
        if not rows:
            raise ValueError("用户不存在")
        return rows[0]

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        rows = self.database.query("SELECT * FROM tickets WHERE id=?", (ticket_id,))
        if not rows:
            raise ValueError("工单不存在")
        return rows[0]

    def list_tickets(self, status: str | None = None, creator_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status.upper())
        if creator_id:
            clauses.append("creator_id=?")
            params.append(creator_id)
        sql = "SELECT * FROM tickets" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        return self.database.query(sql + " ORDER BY created_at DESC LIMIT 50", tuple(params))

    def create_ticket(
        self, title: str, description: str, priority: str, creator_id: str, owner_id: str | None = None
    ) -> dict[str, Any]:
        priority = TicketPriority(priority.upper()).value
        self.get_user(creator_id)
        if owner_id:
            self.get_user(owner_id)
        ticket_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
        now = utc_now()
        self.database.execute(
            """INSERT INTO tickets(id,title,description,priority,status,creator_id,owner_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (ticket_id, title.strip(), description.strip(), priority, "OPEN", creator_id, owner_id, now, now),
        )
        return self.get_ticket(ticket_id)

    def update_ticket(self, ticket_id: str, status: str | None = None, owner_id: str | None = None) -> dict[str, Any]:
        self.get_ticket(ticket_id)
        allowed = {"OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"}
        if status and status.upper() not in allowed:
            raise ValueError("非法工单状态")
        if owner_id:
            self.get_user(owner_id)
        self.database.execute(
            """UPDATE tickets SET status=COALESCE(?,status),owner_id=COALESCE(?,owner_id),updated_at=? WHERE id=?""",
            (status.upper() if status else None, owner_id, utc_now(), ticket_id),
        )
        return self.get_ticket(ticket_id)

    def result_text(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)
