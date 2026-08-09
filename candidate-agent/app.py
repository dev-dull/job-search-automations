"""candidate-agent: three front doors, one brain (see PLAN.md).

Surfaces:
- GET  /                     browser code-entry page
- POST /session              code entry -> HttpOnly cookie (fresh session id)
- POST /chat                 browser chat, streams the answer (cookie + header)
- GET  /c/<code>             zero-install fetch surface: landing page
- GET  /c/<code>/ask?q=...   zero-install fetch surface: one answer
- /mcp                       remote MCP (Streamable HTTP), bearer code
- /mcp/<code>                MCP with URL-carried code (url_auth codes only)
- GET  /robots.txt, /healthz

Run: uvicorn app:app --workers 1   (single process — see PLAN.md)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

import codes as codes_mod
import corpus as corpus_mod
import engine as engine_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("candidate-agent")

CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "the candidate")
SESSION_TTL_S = 12 * 3600

corpus = corpus_mod.Corpus()
table = codes_mod.CodeTable()
limiter = codes_mod.Limiter()
engine = engine_mod.Engine(corpus, limiter)

# In-memory browser sessions: sid -> dict(code, history, created, last)
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _expired_page(status: str) -> HTMLResponse:
    msg = ("This access code has expired or been withdrawn."
           if status == "expired" else "That access code wasn't recognized.")
    return HTMLResponse(
        f"<p>{msg} Please contact {CONTACT_EMAIL} for a fresh link.</p>",
        status_code=403)


def _validate(request: Request, raw_code: str | None,
              *, url_carried: bool) -> codes_mod.Code | None:
    """Shared validation: failed-attempt limiting + url_auth enforcement."""
    ip = _client_ip(request)
    if not limiter.attempts_ok(ip):
        return None
    c = table.lookup(raw_code)
    if c is None or (url_carried and not c.url_auth):
        limiter.record_failed_attempt(ip)
        return None
    return c


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------

from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP(
    "candidate-agent",
    instructions=(
        "This server answers questions about one job candidate on their "
        "behalf, grounded in their published corpus. Ask natural-language "
        "questions with ask_candidate_agent."),
)


def _mcp_code(request: Request | None = None) -> codes_mod.Code | None:
    """The validated code for the current MCP request (set by middleware)."""
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
    except Exception:                                    # noqa: BLE001
        return None
    return getattr(req.state, "agent_code", None)


def _mcp_session_key(code: codes_mod.Code) -> str:
    try:
        from fastmcp.server.dependencies import get_http_headers
        sid = get_http_headers().get("mcp-session-id", "")
    except Exception:                                    # noqa: BLE001
        sid = ""
    return f"mcp:{code.code}:{sid}"


@mcp.tool
def ask_candidate_agent(question: str) -> str:
    """Ask a natural-language question about the candidate — their employment
    history, skills, projects, or interests. Answers are grounded in the
    candidate's published corpus."""
    code = _mcp_code()
    if code is None:
        return "This connection is not authorized. Check the access code."
    if not limiter.allow_request(code.code):
        return "Rate limit reached for this access code; try again later."
    corpus.check_reload()
    return engine.answer(code.code, question,
                         continuity_key=_mcp_session_key(code))


@mcp.tool
def get_profile_summary() -> str:
    """A short public card for the candidate: identity, headline, links."""
    code = _mcp_code()
    if code is None:
        return "This connection is not authorized. Check the access code."
    corpus.check_reload()
    return corpus.profile_summary()


mcp_app = mcp.http_app(path="/", transport="streamable-http")


class MCPAuthMiddleware:
    """ASGI middleware in front of the mounted MCP app.

    Accepts the code as `Authorization: Bearer <code>` or as the first path
    segment (/mcp/<code>/..., url_auth codes only — the claude.ai form).
    Also captures `initialize` clientInfo for logging (phase-2 storage).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        path = scope["path"]
        raw = None
        url_carried = False

        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
        else:
            # /<code> or /<code>/rest — strip the code segment before routing.
            # Starlette mounts hand raw ASGI apps the FULL path with the mount
            # prefix in root_path, so split only the part inside the mount.
            root = scope.get("root_path", "")
            inner = path[len(root):] if root and path.startswith(root) else path
            segs = [s for s in inner.split("/") if s]
            if segs:
                candidate = urllib.parse.unquote(segs[0])
                if table.lookup(candidate):
                    raw = candidate
                    url_carried = True
                    scope = dict(scope)
                    scope["path"] = root + "/" + "/".join(segs[1:])
                    # Never let the code reach access logs via raw_path either.
                    scope["raw_path"] = scope["path"].encode()

        ip = (scope.get("client") or ("unknown",))[0]
        if not limiter.attempts_ok(ip):
            return await _asgi_json(send, 429, {"error": "too many attempts"})
        code = table.lookup(raw)
        if code is None or (url_carried and not code.url_auth):
            limiter.record_failed_attempt(ip)
            return await _asgi_json(send, 401, {"error": "unauthorized"})

        # Peek at JSON-RPC initialize for clientInfo (audit logging).
        receive = _InitializePeek(receive, code)
        scope.setdefault("state", {})
        scope["state"]["agent_code"] = code
        return await self.app(scope, receive, send)


class _InitializePeek:
    """Wraps ASGI receive to log MCP clientInfo without consuming the body."""

    def __init__(self, receive, code):
        self._receive = receive
        self._code = code

    async def __call__(self):
        message = await self._receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            if b'"initialize"' in body and b"clientInfo" in body:
                try:
                    payload = json.loads(body)
                    info = payload.get("params", {}).get("clientInfo", {})
                    log.info("mcp initialize: code=%s client=%s/%s",
                             self._code.label, info.get("name"), info.get("version"))
                except Exception:                        # noqa: BLE001
                    pass
        return message


async def _asgi_json(send, status: int, payload: dict):
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------
# FastAPI app (browser + fetch surfaces), MCP mounted under /mcp
# --------------------------------------------------------------------------

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(application):
    # FastAPI ignores on_event handlers when a lifespan is passed, so corpus
    # startup and the MCP session manager both live here.
    corpus.load_or_die()
    tokens = corpus.token_estimate()
    if tokens > 150_000:
        log.warning("corpus ~%d tokens exceeds the full-context ceiling; "
                    "plan retrieval rung 2 (see PLAN.md)", tokens)
    async with mcp_app.lifespan(application):
        yield


app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.mount("/mcp", MCPAuthMiddleware(mcp_app))


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    # The gate is the code, not obscurity — but no reason to invite crawlers.
    return (
        "User-agent: Claude-User\nAllow: /c/\n\n"
        "User-agent: ChatGPT-User\nAllow: /c/\n\n"
        "User-agent: Google-Extended\nAllow: /c/\n\n"
        "User-agent: *\nDisallow: /\n"
    )


# -- zero-install fetch surface --------------------------------------------

_FETCH_HEADERS = {"Cache-Control": "no-store"}


@app.get("/c/{code}", response_class=PlainTextResponse)
def fetch_landing(code: str, request: Request):
    c = _validate(request, code, url_carried=True)
    if c is None:
        return PlainTextResponse("Unknown or expired access code.",
                                 status_code=403, headers=_FETCH_HEADERS)
    corpus.check_reload()
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse(
        "# Candidate agent\n\n"
        "This endpoint answers questions about one job candidate, for "
        "potential employers. It is an AI agent maintained by the candidate; "
        "answers are grounded in documents they published for this purpose.\n\n"
        "## How to ask\n\n"
        f"Fetch: {base}/c/{code}/ask?q=<url-encoded question>\n\n"
        "Ask one concise question per request (keep the URL short). You may "
        "ask follow-ups; recent questions from this access code are "
        "remembered briefly. Responses are plain markdown.\n\n"
        "## Profile card\n\n" + corpus.profile_summary() + "\n",
        headers=_FETCH_HEADERS, media_type="text/markdown")


@app.get("/c/{code}/ask", response_class=PlainTextResponse)
def fetch_ask(code: str, request: Request, q: str = ""):
    c = _validate(request, code, url_carried=True)
    if c is None:
        return PlainTextResponse("Unknown or expired access code.",
                                 status_code=403, headers=_FETCH_HEADERS)
    if not q.strip():
        return PlainTextResponse("Provide a question via ?q=",
                                 status_code=400, headers=_FETCH_HEADERS)
    if not limiter.allow_request(c.code):
        return PlainTextResponse("Rate limit reached; try again later.",
                                 status_code=429, headers=_FETCH_HEADERS)
    corpus.check_reload()
    answer = engine.answer(c.code, q.strip(),
                           continuity_key=f"fetch:{c.code}")
    return PlainTextResponse(answer, headers=_FETCH_HEADERS,
                             media_type="text/markdown")


# -- browser surface --------------------------------------------------------

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _template(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def index():
    return _template("entry.html")


@app.post("/session")
async def start_session(request: Request):
    form = await request.form()
    raw = (form.get("code") or "").strip()
    ip = _client_ip(request)
    if not limiter.attempts_ok(ip):
        return _expired_page("unknown")
    c = table.lookup(raw)
    if c is None:
        limiter.record_failed_attempt(ip)
        return _expired_page(table.status(raw))
    sid = secrets.token_urlsafe(32)         # fresh id at code entry (fixation)
    with _sessions_lock:
        _sessions[sid] = {"code": raw, "history": [],
                          "created": time.time(), "last": time.time()}
        for k in [k for k, v in _sessions.items()
                  if time.time() - v["last"] > SESSION_TTL_S]:
            del _sessions[k]
    resp = HTMLResponse(_template("chat.html").replace("{{LABEL}}", c.label))
    resp.set_cookie("agent_session", sid, httponly=True, samesite="lax",
                    secure=True, max_age=SESSION_TTL_S)
    return resp


def _session_for(request: Request) -> tuple[dict, codes_mod.Code] | None:
    sid = request.cookies.get("agent_session")
    with _sessions_lock:
        sess = _sessions.get(sid) if sid else None
    if not sess:
        return None
    # Re-validate the code EVERY request: revocation kills live sessions.
    c = table.lookup(sess["code"])
    if c is None:
        with _sessions_lock:
            _sessions.pop(sid, None)
        return None
    return sess, c


@app.post("/chat")
async def chat(request: Request):
    if request.headers.get("x-candidate-agent") != "1":   # CSRF: custom header
        return JSONResponse({"error": "missing header"}, status_code=403)
    found = _session_for(request)
    if not found:
        return JSONResponse({"error": "no session"}, status_code=401)
    sess, c = found
    payload = await request.json()
    question = (payload.get("message") or "").strip()
    if not question:
        return JSONResponse({"error": "empty message"}, status_code=400)
    if len(sess["history"]) >= 2 * engine_mod.MAX_TURNS:
        return JSONResponse({"error": "conversation limit reached"}, status_code=429)
    if not limiter.allow_request(c.code):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    corpus.check_reload()
    sess["last"] = time.time()
    history = list(sess["history"])

    def generate():
        chunks = []
        try:
            for text in engine.stream_answer(c.code, history, question):
                chunks.append(text)
                yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as e:                           # noqa: BLE001
            log.error("stream failed: %s", e)
            yield f"data: {json.dumps({'error': 'answer failed; please retry'})}\n\n"
            return
        sess["history"] = history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": "".join(chunks)}]
        yield "data: {\"done\": true}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
