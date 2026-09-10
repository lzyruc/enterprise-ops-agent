from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

from .config import Settings
from .database import Database
from .models import AgentState, ChatResponse, Role
from .planner import DemoPlanner, OpenAIPlanner, Planner
from .policy import PolicyEngine
from .tools import EnterpriseTools


class AgentEngine:
    def __init__(self, settings: Settings, database: Database | None = None, planner: Planner | None = None):
        self.settings = settings
        self.database = database or Database(settings.database_path)
        knowledge = Path(__file__).resolve().parents[2] / "data" / "knowledge.json"
        if knowledge.exists():
            self.database.seed_documents(knowledge)
        self.tools = EnterpriseTools(self.database)
        self.policy = PolicyEngine()
        self.planner = planner or self._planner_from_settings()

    def _planner_from_settings(self) -> Planner:
        if self.settings.agent_mode.lower() == "openai":
            if not self.settings.openai_api_key:
                raise ValueError("AGENT_MODE=openai 时必须配置 OPENAI_API_KEY")
            return OpenAIPlanner(self.settings.openai_api_key, self.settings.openai_model)
        return DemoPlanner()

    def create_session(self, user_id: str, role: Role) -> str:
        user = self.tools.get_user(user_id)
        if user["role"] != role.value:
            raise PermissionError("请求角色与用户目录中的角色不一致")
        return self.database.create_session(user_id, role.value)

    def chat(self, session_id: str, message: str) -> ChatResponse:
        session = self._session(session_id)
        if session["status"] == "waiting_approval":
            raise ValueError("当前会话正在等待人工审批，请先处理审批")
        trace_id = f"tr_{uuid.uuid4().hex[:16]}"
        self.database.add_message(session_id, "user", message)
        state = AgentState(
            session_id=session_id,
            user_id=session["user_id"],
            role=Role(session["role"]),
            user_message=message,
            summary=session["summary"] or "",
        )
        executed: list[dict[str, Any]] = []
        try:
            for step in range(1, self.settings.max_agent_steps + 1):
                state.step = step
                started = time.perf_counter()
                decision = self.planner.decide(state, self.tools)
                self.database.trace(
                    trace_id,
                    session_id,
                    "planner_decision",
                    decision.model_dump(),
                    (time.perf_counter() - started) * 1000,
                )
                if decision.final_answer:
                    return self._complete(session_id, trace_id, decision.final_answer, executed)
                if not decision.tool_call:
                    raise RuntimeError("规划器未返回工具调用或最终答案")
                call = decision.tool_call
                self._bind_identity(call.name, call.arguments, state.user_id)
                allowed, approval_required, reason = self.policy.authorize(call.name, state.role, call.arguments)
                self.database.trace(
                    trace_id,
                    session_id,
                    "policy_check",
                    {
                        "tool": call.name,
                        "allowed": allowed,
                        "approval_required": approval_required,
                        "reason": reason,
                    },
                )
                if not allowed:
                    return self._fail(session_id, trace_id, reason, executed)
                if approval_required:
                    approval_id = self.database.create_approval(session_id, call.name, call.arguments, state.user_id)
                    answer = f"操作已暂停，等待人工审批（{approval_id}）：{reason}。"
                    self.database.add_message(session_id, "assistant", answer)
                    return ChatResponse(
                        session_id=session_id,
                        status="waiting_approval",
                        answer=answer,
                        approval_id=approval_id,
                        trace_id=trace_id,
                        tool_calls=executed,
                    )
                result = self._execute_tool(trace_id, session_id, call.name, call.arguments)
                item = {"tool": call.name, "arguments": call.arguments, "result": result}
                executed.append(item)
                state.observations.append(item)
                state.completed_tools.append(call.name)
            return self._fail(session_id, trace_id, "超过最大执行步数，已安全终止", executed)
        except Exception as exception:
            self.database.trace(
                trace_id,
                session_id,
                "agent_error",
                {"type": type(exception).__name__, "message": str(exception)},
            )
            return self._fail(session_id, trace_id, f"任务执行失败：{exception}", executed)

    def resume(self, approval_id: str, approved: bool, reviewer_id: str, comment: str = "") -> ChatResponse:
        approval = self.database.get_approval(approval_id)
        if not approval:
            raise ValueError("审批不存在")
        reviewer = self.tools.get_user(reviewer_id)
        if reviewer["role"] not in {Role.MANAGER.value, Role.ADMIN.value}:
            raise PermissionError("只有 manager 或 admin 可以审批")
        self.database.decide_approval(approval_id, approved, reviewer_id, comment)
        session_id = approval["session_id"]
        trace_id = f"tr_{uuid.uuid4().hex[:16]}"
        if not approved:
            answer = f"审批 {approval_id} 已拒绝，未执行 {approval['tool_name']}。"
            return self._complete(session_id, trace_id, answer, [])
        result = self._execute_tool(trace_id, session_id, approval["tool_name"], approval["arguments"])
        answer = self._result_answer(approval["tool_name"], result, approval_id)
        return self._complete(
            session_id,
            trace_id,
            answer,
            [{"tool": approval["tool_name"], "arguments": approval["arguments"], "result": result}],
        )

    def _execute_tool(self, trace_id: str, session_id: str, name: str, arguments: dict[str, Any]) -> Any:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.tools.execute, name, arguments)
            try:
                result = future.result(timeout=self.settings.tool_timeout_seconds)
            except TimeoutError as exception:
                future.cancel()
                raise TimeoutError(f"工具 {name} 执行超时") from exception
        self.database.trace(
            trace_id,
            session_id,
            "tool_result",
            {"tool": name, "arguments": arguments, "result": result},
            (time.perf_counter() - started) * 1000,
        )
        return result

    def _bind_identity(self, name: str, arguments: dict[str, Any], user_id: str) -> None:
        if name == "create_ticket":
            arguments["creator_id"] = user_id
        if name == "list_tickets" and "creator_id" in arguments:
            arguments["creator_id"] = user_id

    def _complete(self, session_id: str, trace_id: str, answer: str, calls: list[dict[str, Any]]) -> ChatResponse:
        self.database.add_message(session_id, "assistant", answer)
        summary = self._summary(session_id)
        self.database.update_session(session_id, status="completed", summary=summary, pending_approval_id=None)
        return ChatResponse(session_id=session_id, status="completed", answer=answer, trace_id=trace_id, tool_calls=calls)

    def _fail(self, session_id: str, trace_id: str, answer: str, calls: list[dict[str, Any]]) -> ChatResponse:
        self.database.add_message(session_id, "assistant", answer)
        self.database.update_session(session_id, status="failed", pending_approval_id=None)
        return ChatResponse(session_id=session_id, status="failed", answer=answer, trace_id=trace_id, tool_calls=calls)

    def _summary(self, session_id: str) -> str:
        messages = self.database.messages(session_id, limit=8)
        return " | ".join(f"{item['role']}: {item['content'][:180]}" for item in messages)[-1_500:]

    def _session(self, session_id: str) -> dict[str, Any]:
        session = self.database.get_session(session_id)
        if not session:
            raise ValueError("会话不存在")
        return session

    def _result_answer(self, tool_name: str, result: Any, approval_id: str) -> str:
        if tool_name == "create_ticket":
            return f"审批 {approval_id} 已通过，工单 {result['id']} 已创建（{result['priority']} / {result['status']}）。"
        return f"审批 {approval_id} 已通过，{tool_name} 执行完成。"
