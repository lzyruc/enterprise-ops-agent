from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from enterprise_agent.api import app, get_engine
from enterprise_agent.config import Settings
from enterprise_agent.engine import AgentEngine
from enterprise_agent.models import Role
from enterprise_agent.policy import PolicyEngine
from enterprise_agent.retrieval import HybridRetriever


@pytest.fixture()
def engine(tmp_path: Path) -> AgentEngine:
    settings = Settings(database_path=tmp_path / "test.db", agent_mode="demo", tool_timeout_seconds=3)
    return AgentEngine(settings)


def test_hybrid_retrieval_returns_p1_policy(engine: AgentEngine) -> None:
    results = HybridRetriever(engine.database).search("P1 工单需要谁审批", kind="policy")
    assert results
    assert results[0]["id"] == "policy-p1-approval"
    assert results[0]["score"] > 0


def test_p1_ticket_is_paused_then_created_after_manager_approval(engine: AgentEngine) -> None:
    session_id = engine.create_session("u-1001", Role.EMPLOYEE)
    paused = engine.chat(session_id, "查询审批制度，并创建 P1 工单：生产 API 大面积超时")
    assert paused.status == "waiting_approval"
    assert paused.approval_id
    assert engine.tools.list_tickets() == []

    completed = engine.resume(paused.approval_id, True, "u-2001", "确认影响核心服务")
    assert completed.status == "completed"
    tickets = engine.tools.list_tickets()
    assert len(tickets) == 1
    assert tickets[0]["priority"] == "P1"
    assert tickets[0]["creator_id"] == "u-1001"


def test_rejected_approval_has_no_side_effect(engine: AgentEngine) -> None:
    session_id = engine.create_session("u-1001", Role.EMPLOYEE)
    paused = engine.chat(session_id, "创建 P1 工单：数据库疑似异常")
    completed = engine.resume(paused.approval_id, False, "u-2001", "影响范围不足")
    assert completed.status == "completed"
    assert engine.tools.list_tickets() == []


def test_employee_cannot_approve(engine: AgentEngine) -> None:
    session_id = engine.create_session("u-1001", Role.EMPLOYEE)
    paused = engine.chat(session_id, "创建 P1 工单：核心接口不可用")
    with pytest.raises(PermissionError):
        engine.resume(paused.approval_id, True, "u-1001")


def test_claimed_role_must_match_directory(engine: AgentEngine) -> None:
    with pytest.raises(PermissionError):
        engine.create_session("u-1001", Role.ADMIN)


def test_policy_blocks_employee_ticket_update() -> None:
    allowed, approval, _ = PolicyEngine().authorize("update_ticket", Role.EMPLOYEE, {})
    assert not allowed
    assert not approval


def test_trace_records_planner_policy_and_tools(engine: AgentEngine) -> None:
    session_id = engine.create_session("u-1001", Role.EMPLOYEE)
    result = engine.chat(session_id, "查询差旅报销制度")
    events = engine.database.query("SELECT event FROM traces WHERE trace_id=?", (result.trace_id,))
    names = {event["event"] for event in events}
    assert {"planner_decision", "policy_check", "tool_result"}.issubset(names)


def test_openai_tool_schemas_include_required_function_fields(engine: AgentEngine) -> None:
    schemas = engine.tools.schemas
    assert schemas
    assert all({"type", "name", "description", "parameters", "strict"} <= schema.keys() for schema in schemas)


def test_fastapi_session_and_chat_contract(engine: AgentEngine) -> None:
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        client = TestClient(app)
        created = client.post("/v1/sessions", json={"user_id": "u-1001", "role": "employee"})
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        response = client.post(f"/v1/sessions/{session_id}/messages", json={"message": "查询 API 延迟处理手册"})
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()
