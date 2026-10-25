"""HTTP API.  Run:  uvicorn app.main:app --port 8000

Roles:  staff -> ask           agent -> ask + see sources/scores + list KB
        admin -> everything, including re-ingestion and the audit log
"""
import json
import logging
import uuid
from collections import deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import conversation, db, ingest, rag, retrieval
from app.auth import (User, ask_limiter, authenticate, client_ip, current_user, issue_token,
                      login_limiter, require_roles)
from app.config import settings as s

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

MAX_BODY_BYTES = 8 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    ingest.ensure_index()  # rebuilds the index if LLM_API_KEY now points at another provider
    t, band = retrieval.gate_params()
    log.info("ready: provider=%s chat=%s fallbacks=%s embed=%s gate=%.3f (grey band %.2f)",
             s.provider, s.chat_model, s.chat_fallbacks, s.embed_model, t, band)
    yield
    db.close_pool()


app = FastAPI(
    title="Vectra IT KB Assistant",
    description="Answers staff IT questions from the Vectra knowledge base only, with citations. "
                "Click **Authorize** and log in with a demo account (see backend/.demo_credentials.txt).",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if s.enable_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if s.enable_docs else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=s.cors_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "Request body too large"}, status_code=413)
    response = await call_next(request)
    response.headers.update({
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    })
    if not request.url.path.startswith("/docs"):
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    # Don't echo the submitted input back (it may contain PII or attack payloads).
    errors = [{"field": ".".join(map(str, e["loc"][1:])), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_handler(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"detail": "Internal error"}, status_code=500)


# --- Schemas -----------------------------------------------------------------

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(Strict):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str
    expires_in: int


class AskRequest(Strict):
    question: str = Field(min_length=2, max_length=s.max_question_chars)
    conversation_id: uuid.UUID | None = Field(
        default=None, description="Omit to start a new conversation; send back the id from the previous "
                                  "answer to ask a follow-up.")


class Source(BaseModel):
    doc_id: str
    title: str


class AskResponse(BaseModel):
    answer: str
    citations: list[str]
    abstained: bool
    conversation_id: str
    sources: list[Source] = []
    trace: dict | None = None  # agents and admins only


# --- Routes ------------------------------------------------------------------

@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok"}


def _login(username: str, password: str, request: Request) -> LoginResponse:
    login_limiter.check(f"{client_ip(request)}:{username}")
    user = authenticate(username, password)
    if not user:
        log.warning("failed login for %r from %s", username, client_ip(request))
        raise HTTPException(401, "Invalid username or password", headers={"WWW-Authenticate": "Bearer"})
    token, ttl = issue_token(user)
    return LoginResponse(access_token=token, role=user.role, username=user.username, expires_in=ttl)


@app.post("/api/auth/login", response_model=LoginResponse, tags=["auth"])
def login(body: LoginRequest, request: Request):
    """JSON login used by the web app."""
    return _login(body.username, body.password, request)


@app.post("/api/auth/token", response_model=LoginResponse, tags=["auth"])
def token_form(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    """OAuth2 form login, used by Swagger's Authorize button."""
    LoginRequest(username=form.username, password=form.password)  # same validation as JSON login
    return _login(form.username, form.password, request)


@app.get("/api/auth/me", tags=["auth"])
def me(user: User = Depends(current_user)):
    return {"username": user.username, "role": user.role}


def _titles() -> dict[str, str]:
    with db.connect() as conn:
        return dict(conn.execute("SELECT doc_id, title FROM kb_chunks WHERE status = 'current'").fetchall())


@app.post("/api/ask", response_model=AskResponse, tags=["assistant"])
def ask(body: AskRequest, user: User = Depends(require_roles("staff", "agent", "admin"))):
    """Answer one question. Pass the returned conversation_id with the next question so follow-ups
    ("what about the first response?") are understood; history is stored server-side per account."""
    ask_limiter.check(user.username)
    conv_id = str(body.conversation_id) if body.conversation_id else None
    history = conversation.load(conv_id, user.username) if conv_id else []
    if not history:  # new, expired or not this account's: start a fresh conversation
        conv_id = conversation.new_id()
    try:
        result, trace = rag.answer_with_trace(body.question, account=user.username, role=user.role,
                                              history=history, conversation_id=conv_id)
    except rag.InvalidQuestion as e:
        raise HTTPException(422, str(e))
    conversation.append(conv_id, user.username, rag.make_turn(result, trace))
    titles = _titles()
    return AskResponse(
        **result,
        conversation_id=conv_id,
        sources=[Source(doc_id=c, title=titles.get(c, c)) for c in result["citations"]],
        trace=trace.__dict__ if user.role in ("agent", "admin") else None,
    )


@app.delete("/api/conversations/{conversation_id}", status_code=204, tags=["assistant"])
def delete_conversation(conversation_id: uuid.UUID, user: User = Depends(require_roles("staff", "agent", "admin"))):
    """Forget a conversation now ("New chat"), instead of waiting for CONVERSATION_TTL_MIN."""
    conversation.delete(str(conversation_id), user.username)


@app.get("/api/kb/articles", tags=["kb"])
def kb_articles(_: User = Depends(require_roles("agent", "admin"))):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT doc_id, title, effective_date::text, owner FROM kb_chunks ORDER BY doc_id").fetchall()
    meta = db.get_meta()
    return {"articles": [dict(zip(("doc_id", "title", "effective_date", "owner"), r)) for r in rows],
            "index": meta}


@app.post("/api/admin/ingest", tags=["admin"])
def admin_ingest(user: User = Depends(require_roles("admin"))):
    report = ingest.run()  # also re-calibrates the relevance gate
    rag.refresh_doc_cache()
    log.info("re-ingest by %s: indexed=%d rejected=%d", user.username, len(report["indexed"]), len(report["rejected"]))
    return report


@app.get("/api/admin/audit", tags=["admin"])
def admin_audit(limit: int = 50, _: User = Depends(require_roles("admin"))):
    limit = max(1, min(limit, 500))
    if not s.audit_log.exists():
        return {"records": []}
    with s.audit_log.open(encoding="utf-8") as f:
        lines = deque(f, maxlen=limit)
    return {"records": [json.loads(l) for l in reversed(lines)]}
