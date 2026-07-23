from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # .env holds LLM/Azure/Neo4j credentials (gitignored)

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime

from app.services.graph_store import GraphStore
from app.services.azure_ad import get_current_user_from_header, validate_jwt
from app.services.azure_config import get_azure_ad_settings
from app.services.neo4j_store import Neo4jStore
import os
from fastapi import Depends, Header
from app.services.azure_ad import user_has_role
from app.services.ingestion import seed_demo_data
from app.services.workflow import run_inspection_workflow
from app.services.onboarding import run_onboarding_workflow
from app.services.audit import audit_log
from app.services.event_bus import TOPIC_REVIEW_ESCALATED, TOPIC_REVIEW_RESOLVED, event_bus
from app.services.local_auth import auth_required, issue_token, validate_local_token
from app.agents.document_analysis import run_document_analysis
from app.agents.client_profiling import run_client_profiling
from app.agents.copilot import run_copilot
from app.agents.supervisor import run_supervisor

app = FastAPI(title="Investment Banking Agentic AI Platform")
BASE_DIR = Path(__file__).resolve().parent
NEO4J_URI = os.getenv("NEO4J_URI", "")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

if NEO4J_URI:
    store = Neo4jStore(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        store.load_demo_data(data_dir=BASE_DIR.parent / "data")
    except Exception:
        # best-effort seed
        pass
else:
    store = GraphStore(data_dir=BASE_DIR.parent / "data")
    seed_demo_data(store, data_dir=BASE_DIR.parent / "data")


# Mount reviewer static UI
try:
    app.mount(
        "/reviewer/static",
        StaticFiles(directory=BASE_DIR / "static" / "reviewer"),
        name="reviewer_static",
    )
except Exception:
    # best-effort; static files may not exist in some environments
    pass


@app.get("/reviewer")
def reviewer_page() -> FileResponse:
    """Serve the reviewer single-page UI."""
    index_path = BASE_DIR / "static" / "reviewer" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Reviewer UI not available")
    return FileResponse(index_path)


azure_ad_settings = get_azure_ad_settings()


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    """Resolve the caller's identity.

    Azure AD (prod) > local JWT (dev RBAC) > anonymous dev fallback.
    Set REQUIRE_AUTH=true to reject anonymous access entirely.
    """
    if azure_ad_settings.get("enable_azure_ad"):
        try:
            return get_current_user_from_header(authorization)
        except Exception as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    if authorization:
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid Authorization header format")
        try:
            return validate_local_token(parts[1])
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    if auth_required():
        raise HTTPException(status_code=401, detail="Authentication required")
    return {"sub": "local-user", "name": "local-user", "roles": []}


def _is_anonymous_dev(current_user: dict) -> bool:
    return (
        not azure_ad_settings.get("enable_azure_ad")
        and not auth_required()
        and current_user.get("sub") == "local-user"
    )


def require_reviewer_role(current_user: dict = Depends(get_current_user)) -> dict:
    """Require the reviewer role.

    Enforced whenever the caller presents claims (Azure AD or local JWT) or
    REQUIRE_AUTH is on; the no-op path only remains for anonymous dev access.
    """
    if _is_anonymous_dev(current_user):
        return current_user
    if user_has_role(current_user, "reviewer") or user_has_role(current_user, "Compliance.Reviewer"):
        return current_user
    raise HTTPException(status_code=403, detail="insufficient role: reviewer required")


# Roles allowed to reach LLM-backed endpoints (copilot, MCP, NLP). RBAC for
# LLM endpoints: authenticated callers must hold an analyst/reviewer role;
# the no-op path only remains for anonymous dev access.
_LLM_ROLES = ("analyst", "reviewer", "Copilot.Analyst", "Compliance.Reviewer")


def require_llm_access(current_user: dict = Depends(get_current_user)) -> dict:
    if _is_anonymous_dev(current_user):
        return current_user
    if any(user_has_role(current_user, role) for role in _LLM_ROLES):
        return current_user
    raise HTTPException(status_code=403, detail="insufficient role: analyst or reviewer required for LLM endpoints")


class TokenRequest(BaseModel):
    username: str
    roles: list[str] = []


@app.post("/api/auth/token")
def issue_dev_token(payload: TokenRequest) -> Dict[str, Any]:
    """Issue a local development JWT (disabled when Azure AD is enabled)."""
    if azure_ad_settings.get("enable_azure_ad"):
        raise HTTPException(status_code=400, detail="Local tokens are disabled; use Azure AD")
    token = issue_token(payload.username, payload.roles)
    return {"access_token": token, "token_type": "bearer", "roles": payload.roles}


class InspectRequest(BaseModel):
    clientId: str
    txData: Dict[str, Any]
    requestId: Optional[str] = None
    userId: Optional[str] = None
    sessionId: Optional[str] = None
    workflowStep: Optional[str] = None


class InspectResponse(BaseModel):
    decision: str
    decisionCode: str
    confidence: float
    rationale: str
    provenance: list[Dict[str, Any]]
    reviewRequired: bool


class UploadResponse(BaseModel):
    doc_id: str
    status: str
    sha256: str
    metadata: Dict[str, Any]
    nlp: Dict[str, Any] = {}


class KYCRequest(BaseModel):
    clientName: str
    jurisdiction: str
    beneficialOwners: list[Dict[str, Any]] = []
    clientId: Optional[str] = None
    requestId: Optional[str] = None
    userId: Optional[str] = None


class KYCResponse(BaseModel):
    requestId: str
    clientId: str
    status: str
    riskScore: float
    riskTier: str
    pepFound: bool
    sanctionsCheckResult: str
    reviewRequired: bool
    reviewTaskId: Optional[str] = None
    provenance: list[Dict[str, Any]]


class ReviewTask(BaseModel):
    review_id: str
    client_id: str
    reason: str
    severity: str
    user_id: str
    status: str


class ReviewDecision(BaseModel):
    decision: str
    reviewer: str
    notes: str | None = None


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Landing on the bare URL should show the dashboard, not a 404."""
    return RedirectResponse(url="/dashboard")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/agents/inspect", response_model=InspectResponse)
def inspect(req: InspectRequest, current_user: dict = Depends(get_current_user)) -> InspectResponse:
    try:
        result = run_inspection_workflow(
            client_id=req.clientId,
            tx_data=req.txData,
            request_id=req.requestId or "req-unknown",
            user_id=req.userId or "user-unknown",
            session_id=req.sessionId or "session-unknown",
            workflow_step=req.workflowStep or "compliance-review",
            store=store,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    audit_log.record(
        event_type="compliance_decision",
        actor_id="AGENT_COMPLIANCE_001",
        action="inspect_client",
        result=result["decision"],
        resource_id=req.clientId,
        request_id=result["request_id"],
        metadata={"confidence": result["confidence"], "reviewRequired": result["reviewRequired"]},
    )

    return InspectResponse(
        decision=result["decision"],
        decisionCode=result["decisionCode"],
        confidence=result["confidence"],
        rationale=result["rationale"],
        provenance=result["provenance"],
        reviewRequired=result["reviewRequired"],
    )


@app.post("/api/agents/onboarding/kyc", response_model=KYCResponse)
def onboarding_kyc(req: KYCRequest, current_user: dict = Depends(get_current_user)) -> KYCResponse:
    client_id = req.clientId or "CLIENT-" + (req.clientName or "unknown").upper().replace(" ", "-")
    result = run_onboarding_workflow(
        client_id=client_id,
        client_name=req.clientName,
        jurisdiction=req.jurisdiction,
        beneficial_owners=req.beneficialOwners,
        request_id=req.requestId or "req-unknown",
        user_id=req.userId or "user-unknown",
        store=store,
        audit_log=audit_log,
    )
    return KYCResponse(
        requestId=result["request_id"],
        clientId=result["client_id"],
        status=result["status"],
        riskScore=result["risk_score"],
        riskTier=result["risk_tier"],
        pepFound=result["pep_found"],
        sanctionsCheckResult=result["sanctions_check_result"],
        reviewRequired=result["review_required"],
        reviewTaskId=result["review_task_id"],
        provenance=result["provenance"],
    )


@app.post("/api/documents/upload", response_model=UploadResponse)
def upload_document(file: UploadFile = File(...), metadata: Optional[str] = Form(None), current_user: dict = Depends(get_current_user)) -> UploadResponse:
    safe_name = file.filename or "upload.bin"
    upload_path = Path("uploads") / safe_name
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    with upload_path.open("wb") as handle:
        handle.write(file.file.read())

    metadata_obj = {}
    if metadata:
        import json

        try:
            metadata_obj = json.loads(metadata)
        except json.JSONDecodeError:
            metadata_obj = {"raw": metadata}

    result = run_document_analysis(str(upload_path), metadata=metadata_obj, store=store)
    return UploadResponse(**result)


@app.post("/api/copilot/query")
def copilot_query(payload: Dict[str, Any], current_user: dict = Depends(require_llm_access)) -> Dict[str, Any]:
    return run_copilot(payload.get("query", ""), store)


class AskRequest(BaseModel):
    query: str
    clientId: Optional[str] = None
    clientName: Optional[str] = None
    jurisdiction: str = "GB"
    text: Optional[str] = None
    requestId: Optional[str] = None


@app.post("/api/agents/ask")
def agents_ask(req: AskRequest, current_user: dict = Depends(require_llm_access)) -> Dict[str, Any]:
    """Supervisor agent: one NL entry point routed across the worker agents.

    Classifies the request, enforces per-intent RBAC centrally, dispatches
    the surviving intents to the specialised agents over audited MCP
    ``tools/call`` hops, and aggregates their outputs into one answer.
    """
    # Anonymous dev callers bypass the supervisor's per-intent RBAC (roles=None),
    # matching how the rest of the API treats unauthenticated local use.
    roles = None if _is_anonymous_dev(current_user) else list(current_user.get("roles") or [])
    result = run_supervisor(
        req.query,
        store,
        user_id=str(current_user.get("sub", "user-unknown")),
        roles=roles,
        client_id=req.clientId,
        client_name=req.clientName,
        jurisdiction=req.jurisdiction,
        text=req.text,
        request_id=req.requestId,
    )
    if result.get("all_denied"):
        raise HTTPException(status_code=403, detail="insufficient role for every routed agent: " + ", ".join(result.get("denied", [])))
    return result


class NLPAnalyzeRequest(BaseModel):
    text: str


@app.post("/api/nlp/analyze")
def nlp_analyze(payload: NLPAnalyzeRequest, current_user: dict = Depends(require_llm_access)) -> Dict[str, Any]:
    """NLP pipeline: NER, contract clause extraction, document classification."""
    from app.services.nlp_pipeline import analyze

    return analyze(payload.text)


@app.post("/api/mcp")
def mcp_endpoint(request: Dict[str, Any], current_user: dict = Depends(require_llm_access)) -> Dict[str, Any]:
    """MCP JSON-RPC 2.0 endpoint (initialize, tools/list, tools/call).

    Inter-agent and external-host access to the platform's agents as MCP
    tools; every tools/call lands on the signed audit trail.
    """
    from app.mcp.server import get_mcp_server

    response = get_mcp_server(store).handle(request, actor_id=str(current_user.get("sub", "mcp-client")))
    return response if response is not None else {}


@app.post("/api/eval/run")
def run_eval(current_user: dict = Depends(require_llm_access)) -> Dict[str, Any]:
    """Run the RAGAS-style evaluation harness over the golden Q&A dataset.

    Scores faithfulness, hallucination rate, answer relevancy, and context
    precision/recall across the copilot RAG chain on the live store.
    """
    from app.eval.harness import load_golden_dataset, run_evaluation

    try:
        dataset = load_golden_dataset(BASE_DIR.parent / "data" / "eval" / "golden_qa.json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="golden dataset not found") from exc
    report = run_evaluation(dataset, store)
    audit_log.record(
        event_type="rag_evaluation",
        actor_id=str(current_user.get("sub", "local-user")),
        action="run_evaluation",
        metadata={"case_count": report["case_count"], **report["aggregate"]},
    )
    return report


@app.get("/api/mcp/tools")
def mcp_tools(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Convenience listing of the MCP tool catalogue (same data as tools/list)."""
    from app.mcp.server import get_mcp_server

    server = get_mcp_server(store)
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {"count": len(server.tool_names()), "tools": response["result"]["tools"]}


@app.get("/api/agents/profile/{client_id}")
def client_profile(client_id: str, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """360° client profile assembled by the client-profiling agent."""
    try:
        return run_client_profiling(client_id, store)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/reviews/pending", response_model=list[ReviewTask])
def pending_reviews(current_user: dict = Depends(require_reviewer_role)) -> list[ReviewTask]:
    return [ReviewTask(**review) for review in store.list_pending_reviews()]


@app.post("/api/reviews/{review_id}/resolve")
def resolve_review(review_id: str, payload: ReviewDecision, current_user: dict = Depends(require_reviewer_role)) -> Dict[str, Any]:
    try:
        review = store.resolve_review(review_id, payload.decision, payload.reviewer, payload.notes)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    event_bus.publish(
        TOPIC_REVIEW_RESOLVED,
        {
            "review_task_id": review["review_id"],
            "decision": review["decision"],
            "reviewer": review["reviewer"],
        },
    )
    return {
        "review_id": review["review_id"],
        "status": review["status"],
        "decision": review["decision"],
        "reviewer": review["reviewer"],
    }


def _store_counts() -> Dict[str, int]:
    """Node counts by label — works on the in-memory store; best-effort on Neo4j."""
    nodes = getattr(store, "nodes", None)
    if nodes is not None:
        counts: Dict[str, int] = {}
        for node in nodes.values():
            label = node.get("label", "Unknown")
            counts[label] = counts.get(label, 0) + 1
        return counts
    try:
        return store.graph_summary()
    except Exception:
        return {}


@app.get("/api/dashboard/summary")
def dashboard_summary(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    counts = _store_counts()
    nodes = getattr(store, "nodes", {}) or {}
    clients = sorted(
        [node for node in nodes.values() if node.get("label") == "ClientProfile"],
        key=lambda node: node.get("client_id", ""),
    )

    decisions = []
    for client in clients:
        client_id = client.get("client_id", "")
        try:
            result = run_inspection_workflow(
                client_id=client_id,
                tx_data={"amount": 0, "currency": "GBP"},
                request_id=f"DASH-{client_id}",
                user_id="dashboard",
                session_id="dashboard",
                workflow_step="dashboard-scan",
                store=store,
                persist_review=False,
            )
        except ValueError:
            continue
        decisions.append(
            {
                "client_id": client_id,
                "name": client.get("name"),
                "country": client.get("country"),
                "risk_score": float(client.get("risk_score", 0)),
                "decision": result["decision"],
                "confidence": result["confidence"],
                "review_required": result["reviewRequired"],
            }
        )

    reviews = store.list_all_reviews()
    pending = [review for review in reviews if review.get("status") == "pending"]
    resolved = [review for review in reviews if review.get("status") == "resolved"]
    events = audit_log.list()

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "data_source": "Neo4j" if NEO4J_URI else "in-memory GraphStore",
        "counts": counts,
        "decisions": decisions,
        "decision_summary": {
            "pass": len([d for d in decisions if d["decision"] == "pass"]),
            "warn": len([d for d in decisions if d["decision"] == "warn"]),
            "fail": len([d for d in decisions if d["decision"] == "fail"]),
            "review_required": len([d for d in decisions if d["review_required"]]),
        },
        "reviews": {"pending": len(pending), "resolved": len(resolved)},
        "audit": {"count": len(events), "recent": list(reversed(events[-5:]))},
    }


@app.get("/dashboard")
def dashboard_page() -> FileResponse:
    """Serve the live analytics dashboard UI."""
    index_path = BASE_DIR / "static" / "dashboard" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI not available")
    return FileResponse(index_path)


@app.get("/chat")
def chat_page() -> FileResponse:
    """Serve the supervisor copilot chat UI."""
    index_path = BASE_DIR / "static" / "chat" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Chat UI not available")
    return FileResponse(index_path)


@app.get("/overview")
def overview_page() -> FileResponse:
    """Serve the projected analytics/overview dashboard (design-target figures)."""
    index_path = BASE_DIR / "static" / "overview" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Overview UI not available")
    return FileResponse(index_path)


@app.get("/api/audit/logs")
def get_audit_logs(request_id: Optional[str] = None, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    events = audit_log.list(request_id)
    return {"request_id": request_id, "count": len(events), "audit_events": events}


@app.get("/api/audit/verify")
def verify_audit_chain(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Verify the SHA-256 hash chain over the append-only audit trail."""
    return audit_log.verify_chain()


@app.get("/api/events/recent")
def recent_events(topic: Optional[str] = None, limit: int = 50, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Recent domain events published on the agent event bus."""
    events = event_bus.recent(topic, limit)
    return {"topic": topic, "count": len(events), "events": events}


@app.get("/api/telemetry/spans")
def telemetry_spans(limit: int = 100, current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Recent OpenTelemetry spans (agent runs, tool calls, LLM requests)."""
    from app.services.telemetry import recent_spans

    spans = recent_spans(limit)
    return {"count": len(spans), "spans": spans}


@app.get("/api/telemetry/llm")
def telemetry_llm(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Aggregate LLM token usage and latency per model."""
    from app.services.telemetry import llm_usage

    return llm_usage.summary()


# --- event subscribers: agents/services reacting to each other's events ---
def _audit_escalation(event: Dict[str, Any]) -> None:
    """Record every escalation on the immutable audit trail, regardless of
    which agent raised it — the audit service reacts to the event, the
    publishing agent doesn't know or care."""
    payload = event["payload"]
    audit_log.record(
        event_type="review_escalated",
        actor_id=str(payload.get("source_agent", "unknown")),
        action="escalate_to_human",
        result="pending",
        resource_id=str(payload.get("client_id")),
        request_id=payload.get("request_id"),
        metadata={"review_task_id": payload.get("review_task_id")},
    )


event_bus.subscribe(TOPIC_REVIEW_ESCALATED, _audit_escalation)


@app.get("/api/reviews/dashboard")
def review_dashboard(current_user: dict = Depends(require_reviewer_role)) -> Dict[str, Any]:
    pending = store.list_pending_reviews()
    resolved = [review for review in store.list_all_reviews() if review.get("status") == "resolved"]
    return {
        "pending_count": len(pending),
        "resolved_count": len(resolved),
        "pending": pending,
        "resolved": resolved,
    }
