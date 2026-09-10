from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Role(StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    ADMIN = "admin"


class TicketPriority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None


class PlannerDecision(BaseModel):
    reasoning: str = ""
    tool_call: ToolCall | None = None
    final_answer: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    user_id: str = Field(default="u-1001", min_length=1, max_length=100)
    role: Role = Role.EMPLOYEE


class SessionCreate(BaseModel):
    user_id: str = Field(default="u-1001", min_length=1, max_length=100)
    role: Role = Role.EMPLOYEE


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)


class ChatResponse(BaseModel):
    session_id: str
    status: Literal["completed", "waiting_approval", "failed"]
    answer: str
    approval_id: str | None = None
    trace_id: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    approved: bool
    reviewer_id: str = Field(min_length=1, max_length=100)
    comment: str = Field(default="", max_length=1_000)


class AgentState(BaseModel):
    session_id: str
    user_id: str
    role: Role
    user_message: str
    summary: str = ""
    observations: list[dict[str, Any]] = Field(default_factory=list)
    completed_tools: list[str] = Field(default_factory=list)
    step: int = 0
