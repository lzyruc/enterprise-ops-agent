from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import Role


@dataclass(frozen=True)
class ToolPolicy:
    allowed_roles: frozenset[Role]
    approval_when: Callable[[dict[str, Any]], bool] | None = None


POLICIES: dict[str, ToolPolicy] = {
    "search_docs": ToolPolicy(frozenset(Role)),
    "search_policy": ToolPolicy(frozenset(Role)),
    "get_user": ToolPolicy(frozenset(Role)),
    "get_ticket": ToolPolicy(frozenset(Role)),
    "list_tickets": ToolPolicy(frozenset(Role)),
    "create_ticket": ToolPolicy(frozenset(Role), approval_when=lambda args: str(args.get("priority", "")).upper() == "P1"),
    "update_ticket": ToolPolicy(frozenset({Role.MANAGER, Role.ADMIN}), approval_when=lambda _args: True),
}


class PolicyEngine:
    def authorize(self, tool_name: str, role: Role, arguments: dict[str, Any]) -> tuple[bool, bool, str]:
        policy = POLICIES.get(tool_name)
        if not policy:
            return False, False, "工具未注册到安全策略"
        if role not in policy.allowed_roles:
            return False, False, f"角色 {role.value} 无权调用 {tool_name}"
        requires_approval = bool(policy.approval_when and policy.approval_when(arguments))
        return True, requires_approval, "需要人工审批" if requires_approval else "允许执行"
