from __future__ import annotations

import argparse

from .config import get_settings
from .engine import AgentEngine
from .models import Role


def main() -> None:
    parser = argparse.ArgumentParser(description="EnterpriseOps Agent CLI")
    parser.add_argument("message", nargs="?", default="查询 P1 工单审批制度并创建一个 P1 工单：生产 API 大面积超时")
    parser.add_argument("--user", default="u-1001")
    parser.add_argument("--role", choices=[item.value for item in Role], default="employee")
    parser.add_argument("--approve-as", default=None, help="自动使用指定 manager/admin 用户完成审批")
    args = parser.parse_args()
    engine = AgentEngine(get_settings())
    session_id = engine.create_session(args.user, Role(args.role))
    result = engine.chat(session_id, args.message)
    print(result.model_dump_json(indent=2))
    if result.approval_id and args.approve_as:
        resumed = engine.resume(result.approval_id, True, args.approve_as, "CLI demo approval")
        print(resumed.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
