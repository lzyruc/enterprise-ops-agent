from __future__ import annotations

from .config import get_settings
from .engine import AgentEngine
from .models import Role


def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("EnterpriseOps Agent")
    engine = AgentEngine(get_settings())

    @mcp.tool()
    def search_enterprise_policy(query: str) -> list[dict]:
        """Search enterprise policies with hybrid retrieval."""
        return engine.tools.retriever.search(query, kind="policy", limit=4)

    @mcp.tool()
    def search_enterprise_docs(query: str) -> list[dict]:
        """Search enterprise handbooks and runbooks."""
        return engine.tools.retriever.search(query, limit=4)

    @mcp.tool()
    def run_enterprise_agent(message: str, user_id: str = "u-1001", role: str = "employee") -> dict:
        """Run the governed Agent. High-risk actions return an approval id and never execute directly."""
        session_id = engine.create_session(user_id, Role(role))
        return engine.chat(session_id, message).model_dump()

    @mcp.tool()
    def decide_enterprise_approval(approval_id: str, approved: bool, reviewer_id: str, comment: str = "") -> dict:
        """Approve or reject a pending high-risk operation as a manager/admin."""
        return engine.resume(approval_id, approved, reviewer_id, comment).model_dump()

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
