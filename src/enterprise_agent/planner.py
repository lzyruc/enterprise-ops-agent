from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from openai import APIConnectionError, APITimeoutError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import AgentState, PlannerDecision, ToolCall
from .tools import EnterpriseTools

SYSTEM_PROMPT = """你是 EnterpriseOps 企业运营助手。你必须：
1. 先检索制度再执行受制度约束的操作；2. 不伪造工具结果；3. 写操作使用工具；
4. 用户身份以系统给出的 user_id/role 为准；5. 简洁说明结果、证据和下一步。
高风险动作会由执行层暂停并请求人工审批，你不要绕过审批。
"""


class Planner(ABC):
    @abstractmethod
    def decide(self, state: AgentState, tools: EnterpriseTools) -> PlannerDecision:
        raise NotImplementedError


class DemoPlanner(Planner):
    """Deterministic planner for interviews, CI and no-key local demos."""

    def decide(self, state: AgentState, tools: EnterpriseTools) -> PlannerDecision:
        text = state.user_message.strip()
        done = set(state.completed_tools)
        priority_match = re.search(r"\bP([1-4])\b", text, re.IGNORECASE)
        priority = f"P{priority_match.group(1)}" if priority_match else "P3"
        wants_create = any(word in text for word in ("创建", "新建", "提交", "create")) and any(
            word in text for word in ("工单", "ticket", "事故")
        )

        if wants_create and (priority == "P1" or "制度" in text or "审批" in text) and "search_policy" not in done:
            return PlannerDecision(
                reasoning="写操作前先检索相关制度",
                tool_call=ToolCall(name="search_policy", arguments={"query": f"{priority} 工单 创建 审批"}),
            )
        if wants_create and "create_ticket" not in done:
            title = self._title(text)
            return PlannerDecision(
                reasoning="已具备必要信息，准备创建工单",
                tool_call=ToolCall(
                    name="create_ticket",
                    arguments={
                        "title": title,
                        "description": text,
                        "priority": priority,
                        "creator_id": state.user_id,
                    },
                ),
            )
        ticket_match = re.search(r"INC-[A-Z0-9]{8}", text.upper())
        if ticket_match and "get_ticket" not in done:
            return PlannerDecision(tool_call=ToolCall(name="get_ticket", arguments={"ticket_id": ticket_match.group(0)}))
        if any(word in text for word in ("工单列表", "我的工单", "list tickets")) and "list_tickets" not in done:
            return PlannerDecision(tool_call=ToolCall(name="list_tickets", arguments={"creator_id": state.user_id}))
        if not state.observations and any(word in text for word in ("制度", "规定", "政策", "审批", "policy")):
            return PlannerDecision(tool_call=ToolCall(name="search_policy", arguments={"query": text}))
        if not state.observations:
            return PlannerDecision(tool_call=ToolCall(name="search_docs", arguments={"query": text}))
        return PlannerDecision(final_answer=self._answer(state))

    def _title(self, text: str) -> str:
        cleaned = re.sub(r"请|帮我|创建|新建|提交|一个|P[1-4]|工单", "", text, flags=re.IGNORECASE).strip("：:，,。 ")
        return (cleaned or "企业运营事件")[:80]

    def _answer(self, state: AgentState) -> str:
        last = state.observations[-1]
        if last["tool"] == "create_ticket":
            ticket = last["result"]
            return f"工单 {ticket['id']} 已创建，优先级 {ticket['priority']}，当前状态 {ticket['status']}。"
        if last["tool"] in {"search_policy", "search_docs"}:
            results = last["result"]
            if not results:
                return "知识库中没有找到足够相关的内容。"
            lines = [f"- {item['title']}（相关度 {item['score']:.2f}）：{item['content']}" for item in results[:3]]
            return "已检索到以下依据：\n" + "\n".join(lines)
        return "操作完成：" + json.dumps(last["result"], ensure_ascii=False, default=str)


class OpenAIPlanner(Planner):
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type((APIConnectionError, APITimeoutError, RateLimitError)),
        reraise=True,
    )
    def decide(self, state: AgentState, tools: EnterpriseTools) -> PlannerDecision:
        context = json.dumps(
            {
                "user_id": state.user_id,
                "role": state.role.value,
                "request": state.user_message,
                "summary": state.summary,
                "observations": state.observations[-5:],
            },
            ensure_ascii=False,
            default=str,
        )
        response = self.client.responses.create(
            model=self.model,
            instructions=SYSTEM_PROMPT,
            input=context,
            tools=tools.schemas,
            tool_choice="auto",
        )
        for item in response.output:
            if getattr(item, "type", None) == "function_call":
                return PlannerDecision(
                    reasoning="模型选择调用企业工具",
                    tool_call=ToolCall(
                        name=item.name,
                        arguments=json.loads(item.arguments or "{}"),
                        call_id=item.call_id,
                    ),
                )
        return PlannerDecision(final_answer=response.output_text or "任务已完成。")
