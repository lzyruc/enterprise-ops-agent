from __future__ import annotations

import json
import tempfile
from pathlib import Path

from enterprise_agent.config import Settings
from enterprise_agent.engine import AgentEngine
from enterprise_agent.models import Role


def main() -> None:
    cases_path = Path(__file__).with_name("cases.jsonl")
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line]
    passed = 0
    details = []
    with tempfile.TemporaryDirectory() as directory:
        engine = AgentEngine(Settings(database_path=Path(directory) / "eval.db", agent_mode="demo"))
        for case in cases:
            session = engine.create_session("u-1001", Role.EMPLOYEE)
            result = engine.chat(session, case["message"])
            tools = [call["tool"] for call in result.tool_calls]
            approval_path = result.status == "waiting_approval"
            checks = [result.status == case["expected_status"]]
            if case.get("expected_tool"):
                checks.append(case["expected_tool"] in tools or case["expected_tool"] in result.answer)
            if "approval_required" in case:
                checks.append(approval_path == case["approval_required"])
            if case.get("answer_contains"):
                checks.append(case["answer_contains"] in result.answer)
            ok = all(checks)
            passed += int(ok)
            details.append({"id": case["id"], "passed": ok, "status": result.status, "tools": tools})
    report = {"passed": passed, "total": len(cases), "pass_rate": passed / len(cases), "cases": details}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
