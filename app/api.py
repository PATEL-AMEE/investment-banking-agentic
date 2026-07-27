from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # .env holds LLM/Azure/Neo4j credentials (gitignored)

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
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
from app.services.llm_adapter import LLMAdapter
import os
from fastapi import Depends, Header
from app.services.azure_ad import user_has_role
from app.services.ingestion import seed_demo_data
from app.services.workflow import run_inspection_workflow
from app.services.onboarding import run_onboarding_workflow
from app.services.audit import audit_log
from app.services.event_bus import TOPIC_REVIEW_ESCALATED, TOPIC_REVIEW_RESOLVED, event_bus
from app.services.local_auth import (
    AUD_CLIENT,
    auth_required,
    issue_client_token,
    issue_staff_token,
    validate_local_token,
)
from app.services import rbac
from app.services.rbac import check_permission
from app.agents.document_analysis import run_document_analysis
from app.agents.client_profiling import run_client_profiling
from app.agents.copilot import run_copilot
from app.agents.supervisor import run_supervisor

app = FastAPI(title="Investment Banking Agentic AI Platform")
BASE_DIR = Path(__file__).resolve().parent

# Record incoming HTTP requests as server spans so Azure Monitor's request-based
# dashboards (Overview, Performance, Application Map) populate, with the agent/
# tool/LLM spans nested underneath.
from app.services.telemetry import instrument_fastapi

instrument_fastapi(app)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """Stop browsers serving a stale UI after a page changes.

    The static HTML pages (login, dashboard, chat, …) are app UI, not assets —
    a cached copy hides new controls (e.g. the review-queue approve buttons).
    Force revalidation on every HTML response; JSON/API responses are untouched.
    """
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response
NEO4J_URI = os.getenv("NEO4J_URI", "")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

if NEO4J_URI:
    store = Neo4jStore(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
    try:
        # Seed only an empty graph. Re-seeding a populated Neo4j on every pod
        # start re-pushes the whole dataset over the network, which can exceed
        # container health-probe windows and crash-loop the pod. Re-seed
        # deliberately with scripts/seed_neo4j.py instead.
        if not store.list_clients(limit=1):
            store.load_demo_data(data_dir=BASE_DIR.parent / "data")
    except Exception:
        # best-effort seed
        pass
else:
    store = GraphStore(data_dir=BASE_DIR.parent / "data")
    seed_demo_data(store, data_dir=BASE_DIR.parent / "data")

# Persist client-portal state (application records + client accounts) in the
# shared store so the onboarding→review flow works across replicas and survives
# restarts. On Neo4j this is shared/durable; on the in-memory GraphStore it's
# per-process (fine for a single local instance and for tests).
from app.services import applications as _applications
from app.services import client_identity as _client_identity

_applications.set_store(store)
_client_identity.set_store(store)


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

# Shared static assets (auth.js session helper used by every UI page)
try:
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
except Exception:
    pass


@app.get("/login")
def login_page() -> FileResponse:
    """Serve the sign-in page (dev JWT; Azure AD replaces this in prod)."""
    index_path = BASE_DIR / "static" / "login" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Login UI not available")
    return FileResponse(index_path)


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


# The only permissions a client-portal token may ever exercise. Anything else
# is the internal staff app's surface and must reject a client-audience token
# outright — the two apps are separate identity domains (two logins), so a
# client credential can never reach staff tooling regardless of role claims.
_CLIENT_PORTAL_PERMISSIONS = frozenset({rbac.PERM_APPLICATION_SUBMIT, rbac.PERM_APPLICATION_STATUS})


def require_permission(permission: str):
    """Dependency factory: role -> permission check from the central policy.

    Enforced whenever the caller presents claims (Azure AD or local JWT) or
    REQUIRE_AUTH is on; the no-op path only remains for anonymous dev access.
    Every real check — allowed or denied — is written to the signed audit
    trail as an ``rbac_check`` event (see :mod:`app.services.rbac`).
    """

    def dependency(current_user: dict = Depends(get_current_user)) -> dict:
        if _is_anonymous_dev(current_user):
            return current_user
        # App-boundary gate: a client-portal token is confined to the
        # client-facing surface, before any role logic runs.
        if current_user.get("aud") == AUD_CLIENT and permission not in _CLIENT_PORTAL_PERMISSIONS:
            audit_log.record(
                event_type="rbac_check",
                actor_id=str(current_user.get("sub", "user-unknown")),
                action=f"permission:{permission}",
                result="denied",
                metadata={"reason": "client-portal token cannot access internal staff endpoints"},
            )
            raise HTTPException(status_code=403, detail="client portal token cannot access internal endpoints")
        roles = list(current_user.get("roles") or [])
        if check_permission(str(current_user.get("sub", "user-unknown")), roles, permission):
            return current_user
        raise HTTPException(status_code=403, detail=f"insufficient role: permission '{permission}' required")

    return dependency


# Named dependencies for each endpoint surface (kept as module-level values so
# the OpenAPI schema shows one dependency per route, not a closure per call).
require_compliance_inspect = require_permission(rbac.PERM_COMPLIANCE_INSPECT)
require_onboarding_kyc = require_permission(rbac.PERM_ONBOARDING_KYC)
require_client_profile = require_permission(rbac.PERM_CLIENT_PROFILE)
require_documents_upload = require_permission(rbac.PERM_DOCUMENTS_UPLOAD)
require_copilot_query = require_permission(rbac.PERM_COPILOT_QUERY)
require_agents_ask = require_permission(rbac.PERM_AGENTS_ASK)
require_nlp_analyze = require_permission(rbac.PERM_NLP_ANALYZE)
require_mcp_call = require_permission(rbac.PERM_MCP_CALL)
require_eval_run = require_permission(rbac.PERM_EVAL_RUN)
require_reviews_read = require_permission(rbac.PERM_REVIEWS_READ)
require_reviews_resolve = require_permission(rbac.PERM_REVIEWS_RESOLVE)
require_audit_read = require_permission(rbac.PERM_AUDIT_READ)
require_dashboard_read = require_permission(rbac.PERM_DASHBOARD_READ)


class TokenRequest(BaseModel):
    username: str
    roles: list[str] = []


@app.post("/api/auth/token")
def issue_dev_token(payload: TokenRequest) -> Dict[str, Any]:
    """Issue a local development JWT (disabled when Azure AD is enabled).

    Low-level dev/test helper: roles are taken as given. The staff login page
    does NOT use this — it uses ``/api/auth/staff/login``, where the role comes
    from the directory, not the caller, so nobody can self-assign a role.
    """
    if azure_ad_settings.get("enable_azure_ad"):
        raise HTTPException(status_code=400, detail="Local tokens are disabled; use Azure AD")
    token = issue_staff_token(payload.username, payload.roles)
    return {"access_token": token, "token_type": "bearer", "roles": payload.roles}


class StaffLogin(BaseModel):
    email: str


@app.post("/api/auth/staff/login")
def staff_login(payload: StaffLogin) -> Dict[str, Any]:
    """Staff sign-in by Microsoft work email (dev stand-in for Entra SSO).

    Looks the email up in the staff directory and issues a token with the
    role the directory assigns — the browser never picks its own role. In
    production this endpoint is replaced by the Azure AD authorization-code
    flow; the email + password + MFA are handled by Microsoft, not here.
    """
    if azure_ad_settings.get("enable_azure_ad"):
        raise HTTPException(status_code=400, detail="Local sign-in is disabled; use Microsoft SSO")
    from app.services import staff_directory

    member = staff_directory.lookup(payload.email)
    if member is None:
        audit_log.record(
            event_type="staff_login",
            actor_id=f"staff:{(payload.email or '').strip().lower()}",
            action="sign_in",
            result="denied",
            metadata={"reason": "email not in staff directory"},
        )
        raise HTTPException(status_code=401, detail="We couldn't find an account with that work email.")
    token = issue_staff_token(member["name"], [member["role"]])
    audit_log.record(
        event_type="staff_login",
        actor_id=f"staff:{member['email']}",
        action="sign_in",
        result="allowed",
        metadata={"role": member["role"]},
    )
    return {"access_token": token, "token_type": "bearer", "name": member["name"], "roles": [member["role"]]}


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
    # Approval package for the human reviewer: decision summary, evidence,
    # policy references, recommendation, approver role, available actions.
    details: Dict[str, Any] = {}


class ReviewDecision(BaseModel):
    decision: str
    reviewer: str
    notes: str | None = None


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Front door: staff start at the corporate sign-in, then reach the app."""
    return RedirectResponse(url="/login")


@app.get("/api/auth/config")
def auth_config() -> Dict[str, Any]:
    """Tells the login page which sign-in flow to run.

    ``azure_ad`` on → the "Sign in with Microsoft" button starts the real
    corporate SSO redirect; off (dev) → it opens the demo directory picker
    that issues a local staff token.
    """
    return {"azure_ad": bool(azure_ad_settings.get("enable_azure_ad"))}


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness: is the process alive? Deliberately dependency-free.

    A liveness probe that checked Neo4j or Azure OpenAI would restart every pod
    during a dependency outage — turning a degradation into an outage. Dependency
    checks belong in ``/ready``.
    """
    return {"status": "ok"}


@app.get("/ready")
def ready(response: Response) -> Dict[str, Any]:
    """Readiness: can this replica actually serve requests?

    Checks each dependency and distinguishes two states, because they warrant
    different responses:

    - **not ready** (HTTP 503) — the graph store is unreachable, so nothing can
      be served. Kubernetes pulls the pod out of the load-balancer rotation.
    - **degraded** (HTTP 200) — the LLM provider isn't configured, so answers
      come from the deterministic stub. The pod still serves, so it stays in
      rotation, but the flag is what an Azure Monitor availability test alerts on.

    The LLM check is configuration-only on purpose: probes run every 10s, and
    issuing a real completion on each one would burn tokens and quota.
    """
    checks: Dict[str, Dict[str, Any]] = {}

    if isinstance(store, Neo4jStore):
        try:
            store.driver.verify_connectivity()
            checks["graph_store"] = {"ok": True, "kind": "neo4j"}
        except Exception as exc:
            checks["graph_store"] = {"ok": False, "kind": "neo4j", "error": str(exc)[:200]}
    else:
        # In-memory store: always available, but flag that it isn't shared across
        # replicas so a "why did state vanish?" question is answerable from here.
        checks["graph_store"] = {"ok": True, "kind": "in-memory", "shared": False}

    provider = LLMAdapter().provider
    checks["llm"] = {"ok": provider != "mock", "provider": provider}

    from app.services.azure_search_store import azure_search_configured

    checks["vector_store"] = {
        "ok": True,
        "kind": "azure-search" if azure_search_configured() else "in-memory",
    }

    from app.services.telemetry import langsmith_enabled

    checks["telemetry"] = {
        "ok": True,
        "azure_monitor": bool(os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")),
        "langsmith": langsmith_enabled(),
    }

    # Only the graph store is required to serve; a stubbed LLM is degraded, not down.
    ready_ok = checks["graph_store"]["ok"]
    degraded = [name for name, check in checks.items() if not check["ok"]]
    if not ready_ok:
        response.status_code = 503

    return {
        "status": "ready" if ready_ok else "not_ready",
        "degraded": degraded,
        "checks": checks,
    }


@app.post("/api/agents/inspect", response_model=InspectResponse)
def inspect(req: InspectRequest, current_user: dict = Depends(require_compliance_inspect)) -> InspectResponse:
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
def onboarding_kyc(req: KYCRequest, current_user: dict = Depends(require_onboarding_kyc)) -> KYCResponse:
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
def upload_document(file: UploadFile = File(...), metadata: Optional[str] = Form(None), current_user: dict = Depends(require_documents_upload)) -> UploadResponse:
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
def copilot_query(payload: Dict[str, Any], current_user: dict = Depends(require_copilot_query)) -> Dict[str, Any]:
    return run_copilot(payload.get("query", ""), store)


class FeedbackRequest(BaseModel):
    query: str
    answer: str = ""
    verdict: str  # "wrong" | "good"
    comment: Optional[str] = None
    sources: list[str] = []


require_feedback_submit = require_permission(rbac.PERM_FEEDBACK_SUBMIT)


@app.post("/api/copilot/feedback")
def submit_feedback(req: FeedbackRequest, current_user: dict = Depends(require_feedback_submit)) -> Dict[str, Any]:
    """Staff flags a Copilot answer as wrong/good (Phase 6.6 feedback loop).

    A ``wrong`` verdict becomes a regression case for the Phase 9 eval harness
    (see ``/api/eval/run``), so a human-spotted failure is measured until fixed.
    """
    from app.services import feedback

    try:
        entry = feedback.record_feedback(
            query=req.query,
            answer=req.answer,
            verdict=req.verdict,
            comment=req.comment,
            sources=req.sources,
            user_id=str(current_user.get("sub", "user-unknown")),
            audit_log=audit_log,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"feedback_id": entry["feedback_id"], "verdict": entry["verdict"], **feedback.summary()}


@app.get("/api/copilot/feedback")
def feedback_summary(current_user: dict = Depends(require_reviews_read)) -> Dict[str, Any]:
    """Aggregate feedback counts + the flagged-question regression set."""
    from app.services import feedback

    return {**feedback.summary(), "flagged": feedback.flagged_questions()}


class AskRequest(BaseModel):
    query: str
    clientId: Optional[str] = None
    clientName: Optional[str] = None
    jurisdiction: str = "GB"
    text: Optional[str] = None
    requestId: Optional[str] = None


@app.post("/api/agents/ask")
def agents_ask(req: AskRequest, current_user: dict = Depends(require_agents_ask)) -> Dict[str, Any]:
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
def nlp_analyze(payload: NLPAnalyzeRequest, current_user: dict = Depends(require_nlp_analyze)) -> Dict[str, Any]:
    """NLP pipeline: NER, contract clause extraction, document classification."""
    from app.services.nlp_pipeline import analyze

    return analyze(payload.text)


@app.post("/api/mcp")
def mcp_endpoint(request: Dict[str, Any], current_user: dict = Depends(require_mcp_call)) -> Dict[str, Any]:
    """MCP JSON-RPC 2.0 endpoint (initialize, tools/list, tools/call).

    Inter-agent and external-host access to the platform's agents as MCP
    tools; every tools/call lands on the signed audit trail.
    """
    from app.mcp.server import get_mcp_server

    response = get_mcp_server(store).handle(request, actor_id=str(current_user.get("sub", "mcp-client")))
    return response if response is not None else {}


@app.post("/api/eval/run")
def run_eval(current_user: dict = Depends(require_eval_run)) -> Dict[str, Any]:
    """Run the RAGAS-style evaluation harness over the golden Q&A dataset.

    Scores faithfulness, hallucination rate, answer relevancy, and context
    precision/recall across the copilot RAG chain on the live store.
    """
    from app.eval.harness import load_golden_dataset, run_ablation, run_evaluation, run_feedback_regression

    try:
        dataset = load_golden_dataset(BASE_DIR.parent / "data" / "eval" / "golden_qa.json")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="golden dataset not found") from exc
    report = run_evaluation(dataset, store)
    # Fold in the Phase 6.6 feedback loop: questions staff flagged as wrong are
    # re-scored here so a human-spotted failure keeps being measured until fixed.
    report["feedback_regression"] = run_feedback_regression(store)
    # A/B the two retrieval chains (deterministic) so the report shows what the
    # graph hop adds over plain vector RAG (recovers graph-connected sources).
    report["ablation"] = run_ablation(dataset, store)
    # Persist the aggregate to the shared store so the dashboard's AI-quality
    # panel can show the latest scores without re-running the (LLM-driven) eval
    # on every page load. Survives restarts and is shared across replicas.
    try:
        if hasattr(store, "kv_put"):
            store.kv_put("Eval", "latest", {
                "generated_at": report["generated_at"],
                "engine": report.get("engine", "lexical"),
                "case_count": report["case_count"],
                "aggregate": report["aggregate"],
                "graph_rag_delta": report.get("ablation", {}).get("graph_rag_delta"),
            })
    except Exception:
        pass
    audit_log.record(
        event_type="rag_evaluation",
        actor_id=str(current_user.get("sub", "local-user")),
        action="run_evaluation",
        metadata={
            "case_count": report["case_count"],
            "flagged_count": report["feedback_regression"]["flagged_count"],
            **report["aggregate"],
        },
    )
    return report


@app.get("/api/eval/summary")
def eval_summary() -> Dict[str, Any]:
    """Latest RAGAS-style evaluation aggregate for the dashboard (read-only).

    Returns the scores from the most recent ``/api/eval/run`` (stored in the
    shared KV). ``available: false`` until the harness has been run once.
    """
    latest = None
    try:
        if hasattr(store, "kv_get"):
            latest = store.kv_get("Eval", "latest")
    except Exception:
        latest = None
    if not latest:
        return {"available": False}
    return {"available": True, **latest}


@app.get("/api/mcp/tools")
def mcp_tools(current_user: dict = Depends(require_mcp_call)) -> Dict[str, Any]:
    """Convenience listing of the MCP tool catalogue (same data as tools/list)."""
    from app.mcp.server import get_mcp_server

    server = get_mcp_server(store)
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {"count": len(server.tool_names()), "tools": response["result"]["tools"]}


@app.get("/api/agents/profile/{client_id}")
def client_profile(client_id: str, current_user: dict = Depends(require_client_profile)) -> Dict[str, Any]:
    """360° client profile assembled by the client-profiling agent."""
    try:
        return run_client_profiling(client_id, store)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ------------------------------------------------------------- client portal
# The client is a separate, much more limited actor: they submit through the
# intake form and check their OWN status — never the internal detail.

class ClientApplication(BaseModel):
    companyName: str
    jurisdiction: str = "GB"
    product: str = "Corporate account"
    beneficialOwners: list[Dict[str, Any]] = []
    documentsProvided: list[str] = []
    documentText: Optional[str] = None
    # Portal sign-up credentials (external identity — separate from staff SSO).
    # Optional so programmatic/test submissions still work; the portal UI
    # always supplies them, and they are what a returning client signs in with.
    email: Optional[str] = None
    password: Optional[str] = None


class ClientLogin(BaseModel):
    email: str
    password: str


def _issue_client_session(client_id: str, company_name: str, subject: str) -> str:
    """Mint a client-portal token scoped to one client's own records."""
    return issue_client_token(subject, ["client"], extra_claims={"client_id": client_id})


@app.post("/api/client/apply")
def client_apply(req: ClientApplication) -> Dict[str, Any]:
    """Client intake: submission triggers the whole onboarding chain.

    Runs the onboarding agent (sanctions/PEP screening, risk scoring,
    review escalation) plus NLP over any supplied documents — all outputs
    stay internal. The response is the client-safe view plus a client-portal
    token scoped to this client's own records (``client`` role + ``client_id``
    claim). When credentials are supplied, the form doubles as portal sign-up
    (mirroring an Azure AD B2C sign-up policy) so the client can return later.
    """
    from app.services import applications, client_identity

    record = applications.submit_application(
        company_name=req.companyName,
        jurisdiction=req.jurisdiction,
        product=req.product,
        beneficial_owners=req.beneficialOwners,
        documents_provided=req.documentsProvided,
        document_text=req.documentText,
        store=store,
        audit_log=audit_log,
        contact_email=req.email,
    )
    if req.email and req.password:
        client_identity.register(
            email=req.email,
            password=req.password,
            client_id=record["client_id"],
            company_name=req.companyName,
            application_id=record["application_id"],
        )
    token = _issue_client_session(record["client_id"], req.companyName, f"client:{req.companyName}")
    view = applications.client_view(record, store)
    view["accessToken"] = token
    return view


@app.post("/api/client/login")
def client_login(req: ClientLogin) -> Dict[str, Any]:
    """Returning-client sign-in for the portal (Azure AD B2C in prod).

    Authenticates against the client identity store — entirely separate from
    staff SSO — and returns a client-portal token plus the plain-language
    status of the client's most recent application.
    """
    from app.services import applications, client_identity

    account = client_identity.authenticate(req.email, req.password)
    if account is None:
        audit_log.record(
            event_type="client_login",
            actor_id=f"client-portal:{req.email.strip().lower()}",
            action="sign_in",
            result="denied",
            metadata={"reason": "invalid credentials"},
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")

    records = applications.applications_for_client(account.client_id)
    latest = max(records, key=lambda r: r["submitted_at"], default=None)
    token = _issue_client_session(account.client_id, account.company_name, f"client:{account.email}")
    audit_log.record(
        event_type="client_login",
        actor_id=f"client-portal:{account.email}",
        action="sign_in",
        result="allowed",
        resource_id=latest["application_id"] if latest else None,
    )
    payload: Dict[str, Any] = {"accessToken": token, "company": account.company_name}
    if latest is not None:
        view = applications.client_view(latest, store)
        view["accessToken"] = token
        payload = view
    return payload


require_application_status = require_permission(rbac.PERM_APPLICATION_STATUS)


@app.get("/api/client/status/{application_id}")
def client_status(application_id: str, current_user: dict = Depends(require_application_status)) -> Dict[str, Any]:
    """Own-record status check: plain-language status only.

    Resource scoping on top of the role permission: a client token may only
    read applications belonging to its own ``client_id`` claim. Staff roles
    with broader read rights (risk_manager, compliance_analyst) may look up
    any application; the response shape is still the client-safe view.
    """
    from app.services import applications

    record = applications.get_application(application_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Application not found")
    token_client_id = current_user.get("client_id")
    if token_client_id and token_client_id != record["client_id"]:
        # A client token for a different company: deny and audit.
        audit_log.record(
            event_type="rbac_check",
            actor_id=str(current_user.get("sub", "user-unknown")),
            action="resource:application.status.read",
            result="denied",
            resource_id=application_id,
            metadata={"reason": "application belongs to a different client"},
        )
        raise HTTPException(status_code=403, detail="You may only view your own application")
    return applications.client_view(record, store)


require_application_submit = require_permission(rbac.PERM_APPLICATION_SUBMIT)


@app.post("/api/client/documents/{application_id}")
def client_submit_documents(
    application_id: str,
    files: list[UploadFile] = File(...),
    docTypes: list[str] = Form(...),
    current_user: dict = Depends(require_application_submit),
) -> Dict[str, Any]:
    """Client uploads outstanding documents for their own application.

    ``files`` and ``docTypes`` are parallel arrays (one required-document key
    per file). Each file runs through the document-analysis agent internally;
    the client gets back only the refreshed plain-language status view.
    """
    from app.services import applications

    record = applications.get_application(application_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Application not found")
    token_client_id = current_user.get("client_id")
    if token_client_id and token_client_id != record["client_id"]:
        audit_log.record(
            event_type="rbac_check",
            actor_id=str(current_user.get("sub", "user-unknown")),
            action="resource:application.documents.submit",
            result="denied",
            resource_id=application_id,
            metadata={"reason": "application belongs to a different client"},
        )
        raise HTTPException(status_code=403, detail="You may only submit documents for your own application")
    if len(files) != len(docTypes):
        raise HTTPException(status_code=422, detail="files and docTypes must align one-to-one")

    upload_dir = Path("uploads") / "client" / application_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    submitted = []
    for upload, doc_type in zip(files, docTypes):
        if doc_type not in applications.REQUIRED_DOCUMENTS:
            raise HTTPException(status_code=422, detail=f"Unknown document type: {doc_type}")
        safe_name = Path(upload.filename or "upload.bin").name
        file_path = upload_dir / f"{doc_type}-{safe_name}"
        with file_path.open("wb") as handle:
            handle.write(upload.file.read())
        submitted.append({"doc_type": doc_type, "file_path": str(file_path), "original_name": safe_name})

    applications.add_documents(record=record, submitted=submitted, store=store, audit_log=audit_log)
    return applications.client_view(record, store)


@app.get("/client")
def client_portal_page() -> FileResponse:
    """Serve the client-facing intake/status portal."""
    index_path = BASE_DIR / "static" / "client" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Client portal not available")
    return FileResponse(index_path)


@app.get("/client/login")
def client_login_page() -> FileResponse:
    """Serve the client portal sign-in page (Azure AD B2C replaces this in prod)."""
    index_path = BASE_DIR / "static" / "client" / "login" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Client login not available")
    return FileResponse(index_path)


@app.get("/api/reviews/pending", response_model=list[ReviewTask])
def pending_reviews(current_user: dict = Depends(require_reviews_read)) -> list[ReviewTask]:
    # Deduplicate defensively by review_id (keep the most recent upsert).
    unique: Dict[str, Dict[str, Any]] = {}
    for review in store.list_pending_reviews():
        unique[review["review_id"]] = review
    return [ReviewTask(**review) for review in unique.values()]


@app.post("/api/reviews/{review_id}/resolve")
def resolve_review(review_id: str, payload: ReviewDecision, current_user: dict = Depends(require_reviews_resolve)) -> Dict[str, Any]:
    try:
        review = store.resolve_review(review_id, payload.decision, payload.reviewer, payload.notes)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # GraphStore raises on a missing id; Neo4jStore returns None. Treat both as
    # not-found so a bad review_id is a clean 404, never a 500.
    if review is None:
        raise HTTPException(status_code=404, detail=f"Review {review_id} not found")
    event_bus.publish(
        TOPIC_REVIEW_RESOLVED,
        {
            "review_task_id": review["review_id"],
            "decision": review["decision"],
            "reviewer": review["reviewer"],
        },
    )
    # If this review gates a client application, the decision changes the
    # client's status — notify them (approved/declined), client-safe wording.
    from app.services import applications

    app_record = applications.application_for_review(review["review_id"])
    if app_record is not None:
        applications.notify_status_change(app_record, store, audit_log)
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
        # Neo4j's graph_summary() nests as {nodes:{...}, relationships:{...}};
        # the dashboard wants a flat label->count map like the in-memory store.
        return store.graph_summary().get("nodes", {})
    except Exception:
        return {}


@app.get("/api/dashboard/summary")
def dashboard_summary(current_user: dict = Depends(require_dashboard_read)) -> Dict[str, Any]:
    counts = _store_counts()
    # Read-only preview scan: run the compliance workflow over a bounded sample
    # of clients (store-agnostic — works on both in-memory and Neo4j). Scanning
    # every client on each dashboard load would be needlessly slow.
    DASH_SCAN_LIMIT = 25
    clients = store.list_clients(limit=DASH_SCAN_LIMIT)

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


@app.get("/flow")
def flow_page() -> FileResponse:
    """Serve the staff request-flow diagram (SSO → supervisor → confidence → audit)."""
    index_path = BASE_DIR / "static" / "flow" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Flow UI not available")
    return FileResponse(index_path)


@app.get("/telemetry")
def telemetry_page() -> FileResponse:
    """Serve the live trace-waterfall UI (per-request latency + audit correlation)."""
    index_path = BASE_DIR / "static" / "telemetry" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Telemetry UI not available")
    return FileResponse(index_path)


@app.get("/api/audit/logs")
def get_audit_logs(request_id: Optional[str] = None, current_user: dict = Depends(require_audit_read)) -> Dict[str, Any]:
    events = audit_log.list(request_id)
    return {"request_id": request_id, "count": len(events), "audit_events": events}


@app.get("/api/audit/verify")
def verify_audit_chain(current_user: dict = Depends(require_audit_read)) -> Dict[str, Any]:
    """Verify the SHA-256 hash chain over the append-only audit trail."""
    return audit_log.verify_chain()


@app.get("/api/events/recent")
def recent_events(topic: Optional[str] = None, limit: int = 50, current_user: dict = Depends(require_dashboard_read)) -> Dict[str, Any]:
    """Recent domain events published on the agent event bus."""
    events = event_bus.recent(topic, limit)
    return {"topic": topic, "count": len(events), "events": events}


@app.get("/api/telemetry/spans")
def telemetry_spans(limit: int = 100, current_user: dict = Depends(require_dashboard_read)) -> Dict[str, Any]:
    """Recent OpenTelemetry spans (agent runs, tool calls, LLM requests)."""
    from app.services.telemetry import recent_spans

    spans = recent_spans(limit)
    return {"count": len(spans), "spans": spans}


@app.get("/api/telemetry/llm")
def telemetry_llm(current_user: dict = Depends(require_dashboard_read)) -> Dict[str, Any]:
    """Aggregate LLM token usage and latency per model."""
    from app.services.telemetry import llm_usage

    return llm_usage.summary()


@app.get("/api/telemetry/traces")
def telemetry_traces(limit: int = 25, current_user: dict = Depends(require_dashboard_read)) -> Dict[str, Any]:
    """Recent distinct request traces, newest first (the 'recent requests' list).

    Access is role-restricted (dashboard.read) — trace dashboards are
    engineering data and are themselves gated, per Phase 7.
    """
    from app.services.telemetry import list_traces

    traces = list_traces(limit)
    return {"count": len(traces), "traces": traces}


@app.get("/api/telemetry/trace/{trace_id}")
def telemetry_trace(trace_id: str, current_user: dict = Depends(require_dashboard_read)) -> Dict[str, Any]:
    """Waterfall for one request: per-step latency across the pipeline, plus
    the audit events that share this trace id (the compliance-facing record of
    the same request). This is the single pane that joins the two systems.
    """
    from app.services.telemetry import get_trace

    waterfall = get_trace(trace_id)
    if not waterfall["found"]:
        raise HTTPException(status_code=404, detail=f"No trace {trace_id} in the retained window")
    # Correlated compliance record — same trace id, separate system.
    waterfall["audit_events"] = [
        {k: e.get(k) for k in ("log_id", "event_type", "actor_id", "action", "result", "resource_id")}
        for e in audit_log.list(trace_id=trace_id)
    ]
    return waterfall


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
def review_dashboard(current_user: dict = Depends(require_reviews_read)) -> Dict[str, Any]:
    pending = store.list_pending_reviews()
    resolved = [review for review in store.list_all_reviews() if review.get("status") == "resolved"]
    return {
        "pending_count": len(pending),
        "resolved_count": len(resolved),
        "pending": pending,
        "resolved": resolved,
    }
