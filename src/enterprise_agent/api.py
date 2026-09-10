from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .engine import AgentEngine
from .models import ApprovalDecision, ChatResponse, MessageRequest, SessionCreate

SettingsDep = Annotated[Settings, Depends(get_settings)]


@lru_cache
def get_engine() -> AgentEngine:
    settings = get_settings()
    return AgentEngine(settings)


def require_api_key(
    settings: SettingsDep,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    if settings.app_env != "development" and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


app = FastAPI(
    title="EnterpriseOps Agent API",
    version="1.0.0",
    description="RAG + tools + RBAC + human approval + memory + observability",
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
)

EngineDep = Annotated[AgentEngine, Depends(get_engine)]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": settings.agent_mode, "version": "1.0.0"}


@app.get("/v1/capabilities", dependencies=[Depends(require_api_key)])
def capabilities(engine: EngineDep) -> dict:
    return {
        "features": ["hybrid-rag", "tool-calling", "rbac", "hitl", "persistent-memory", "tracing", "mcp"],
        "tools": [{"name": spec.name, "description": spec.description} for spec in engine.tools.registry.values()],
        "mode": settings.agent_mode,
    }


@app.post("/v1/sessions", dependencies=[Depends(require_api_key)], status_code=201)
def create_session(body: SessionCreate, engine: EngineDep) -> dict[str, str]:
    try:
        return {"session_id": engine.create_session(body.user_id, body.role)}
    except (ValueError, PermissionError) as exception:
        raise HTTPException(status_code=400, detail=str(exception)) from exception


@app.post("/v1/sessions/{session_id}/messages", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
def chat(session_id: str, body: MessageRequest, engine: EngineDep) -> ChatResponse:
    try:
        return engine.chat(session_id, body.message)
    except ValueError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception


@app.get("/v1/sessions/{session_id}", dependencies=[Depends(require_api_key)])
def get_session(session_id: str, engine: EngineDep) -> dict:
    session = engine.database.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session["messages"] = engine.database.messages(session_id)
    return session


@app.post(
    "/v1/approvals/{approval_id}/decision",
    response_model=ChatResponse,
    dependencies=[Depends(require_api_key)],
)
def decide(approval_id: str, body: ApprovalDecision, engine: EngineDep) -> ChatResponse:
    try:
        return engine.resume(approval_id, body.approved, body.reviewer_id, body.comment)
    except PermissionError as exception:
        raise HTTPException(status_code=403, detail=str(exception)) from exception
    except ValueError as exception:
        raise HTTPException(status_code=409, detail=str(exception)) from exception


@app.get("/v1/traces/{trace_id}", dependencies=[Depends(require_api_key)])
def trace(trace_id: str, engine: EngineDep) -> list[dict]:
    return engine.database.query("SELECT * FROM traces WHERE trace_id=? ORDER BY id", (trace_id,))


@app.get("/v1/tickets", dependencies=[Depends(require_api_key)])
def tickets(engine: EngineDep, status: str | None = None, creator_id: str | None = None) -> list[dict]:
    return engine.tools.list_tickets(status, creator_id)


def run() -> None:
    import uvicorn

    uvicorn.run("enterprise_agent.api:app", host="0.0.0.0", port=8000, reload=False)
