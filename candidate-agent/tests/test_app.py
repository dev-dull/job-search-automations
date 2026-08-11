"""HTTP-surface tests (fetch, browser, MCP auth). Requires fastapi + fastmcp +
httpx — skipped cleanly where those aren't installed (pure-stdlib tests in the
other files still run). CI installs the full requirements and runs everything.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DEPS = all(importlib.util.find_spec(m) for m in ("fastapi", "fastmcp", "httpx"))

if _DEPS:
    _root = tempfile.mkdtemp()
    with open(os.path.join(_root, "profile.md"), "w") as f:
        f.write("---\ntitle: Profile\nsummary: test\n---\nJordan Sample, platform engineer.\n")
    _codes = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    _codes.write("plain-code-1 | Header Co\n"
                 "urlok-code-2 | URL Co | url_auth\n"
                 "dead-code-3 | Gone Inc | revoked\n")
    _codes.close()
    os.environ["CORPUS_PATH"] = _root
    os.environ["ACCESS_CODES_PATH"] = _codes.name
    os.environ["AGENT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "agent.db")
    os.environ["ADMIN_TOKEN"] = "test-admin-token"
    os.environ.pop("REDACTION_DENYLIST_PATH", None)

    import app as app_mod
    from fastapi.testclient import TestClient

    # Never hit the real API: stub the Anthropic CLIENT (not the engine), so
    # the real engine paths — continuity, usage accounting, the phase-2
    # recorder — all execute.
    _USAGE = {"input_tokens": 10, "output_tokens": 5,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    class _Block:
        type = "text"

    def _resp(text):
        b = _Block(); b.text = text
        r = _Block(); r.content = [b]; r.usage = dict(_USAGE)
        return r

    class _FakeStream:
        def __enter__(self):
            self.text_stream = iter(["streamed ", "answer"])
            return self
        def __exit__(self, *a):
            return False
        def get_final_message(self):
            return _resp("streamed answer")

    class _FakeMessages:
        def create(self, **kw):
            return _resp(f"answer to: {kw['messages'][-1]['content']}")
        def stream(self, **kw):
            return _FakeStream()

    class _FakeClient:
        messages = _FakeMessages()

    app_mod.engine._client = _FakeClient()


@unittest.skipUnless(_DEPS, "fastapi/fastmcp/httpx not installed")
class SurfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # https base: the session cookie is Secure and would never be sent
        # back over the TestClient's default http://testserver.
        cls.client = TestClient(app_mod.app, base_url="https://testserver")
        cls.client.__enter__()          # run lifespan (corpus load)

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # Fresh durable counters per test: new DB file, re-attached everywhere.
        os.environ["AGENT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "agent.db")
        app_mod.db = app_mod.store_mod.Store(os.environ["AGENT_DB_PATH"])
        app_mod.table.attach_store(app_mod.db)
        app_mod.limiter = app_mod.store_mod.DurableLimiter(app_mod.db)
        app_mod.engine.limiter = app_mod.limiter
        app_mod.engine.recorder = app_mod.db.record_exchange

    # -- basics -------------------------------------------------------------

    def test_healthz_and_robots(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})
        robots = self.client.get("/robots.txt").text
        self.assertIn("Claude-User", robots)
        self.assertIn("Disallow: /", robots)

    # -- fetch surface ------------------------------------------------------

    def test_fetch_requires_url_auth_flag(self):
        r = self.client.get("/c/urlok-code-2")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/c/urlok-code-2/ask?q=", r.text)
        self.assertEqual(r.headers["cache-control"], "no-store")
        # A valid code WITHOUT url_auth must not work URL-carried.
        self.assertEqual(self.client.get("/c/plain-code-1").status_code, 403)
        self.assertEqual(self.client.get("/c/dead-code-3").status_code, 403)

    def test_fetch_ask(self):
        r = self.client.get("/c/urlok-code-2/ask", params={"q": "skills?"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, "answer to: skills?")
        self.assertEqual(
            self.client.get("/c/urlok-code-2/ask").status_code, 400)

    # -- browser surface ----------------------------------------------------

    def _enter(self, code="plain-code-1"):
        return self.client.post("/session", data={"code": code})

    def test_code_entry_sets_cookie_and_chat_works(self):
        r = self._enter()
        self.assertEqual(r.status_code, 200)
        self.assertIn("agent_session", r.cookies)
        chat = self.client.post("/chat", json={"message": "hi"},
                                headers={"X-Candidate-Agent": "1"})
        self.assertEqual(chat.status_code, 200)
        text = "".join(
            json.loads(f[6:]).get("text", "")
            for f in chat.text.split("\n\n") if f.startswith("data: "))
        self.assertEqual(text, "streamed answer")
        self.assertIn('{"done": true}', chat.text)

    def test_chat_requires_custom_header_and_session(self):
        self._enter()
        self.assertEqual(
            self.client.post("/chat", json={"message": "hi"}).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(
            self.client.post("/chat", json={"message": "hi"},
                             headers={"X-Candidate-Agent": "1"}).status_code, 401)

    def test_bad_code_rejected(self):
        r = self._enter("who-dis")
        self.assertEqual(r.status_code, 403)

    def test_revocation_kills_live_session(self):
        self._enter("plain-code-1")
        # Revoke by rewriting the code file; mtime bump makes it visible.
        with open(os.environ["ACCESS_CODES_PATH"], "w") as f:
            f.write("urlok-code-2 | URL Co | url_auth\n")
        os.utime(os.environ["ACCESS_CODES_PATH"],
                 (time.time() + 2, time.time() + 2))
        try:
            r = self.client.post("/chat", json={"message": "hi"},
                                 headers={"X-Candidate-Agent": "1"})
            self.assertEqual(r.status_code, 401)
        finally:
            with open(os.environ["ACCESS_CODES_PATH"], "w") as f:
                f.write("plain-code-1 | Header Co\n"
                        "urlok-code-2 | URL Co | url_auth\n"
                        "dead-code-3 | Gone Inc | revoked\n")
            os.utime(os.environ["ACCESS_CODES_PATH"],
                     (time.time() + 4, time.time() + 4))

    # -- MCP auth -----------------------------------------------------------

    def _mcp_initialize(self, headers=None, path="/mcp/"):
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18",
                           "capabilities": {},
                           "clientInfo": {"name": "test-client", "version": "1.0"}}}
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        h.update(headers or {})
        return self.client.post(path, json=body, headers=h)

    def test_mcp_rejects_missing_and_bad_codes(self):
        self.assertEqual(self._mcp_initialize().status_code, 401)
        self.assertEqual(
            self._mcp_initialize({"Authorization": "Bearer nope"}).status_code, 401)
        # Valid code but NOT url_auth: path form rejected.
        self.assertEqual(
            self._mcp_initialize(path="/mcp/plain-code-1/").status_code, 401)

    def test_mcp_accepts_bearer_and_url_auth_path(self):
        r = self._mcp_initialize({"Authorization": "Bearer plain-code-1"})
        self.assertEqual(r.status_code, 200)
        r2 = self._mcp_initialize(path="/mcp/urlok-code-2/")
        self.assertEqual(r2.status_code, 200)

    # -- phase 2: recording, admin, disclosure ---------------------------

    def test_web_chat_is_recorded(self):
        self._enter()
        self.client.post("/chat", json={"message": "record me"},
                         headers={"X-Candidate-Agent": "1"})
        summary = {r["code"]: r for r in app_mod.db.summary()}
        row = summary["plain-code-1"]
        self.assertEqual(row["sessions"], 1)
        self.assertEqual(row["messages"], 2)
        sess = app_mod.db.sessions_for_code("plain-code-1")[0]
        self.assertEqual(sess["surface"], "web")
        transcript = app_mod.db.transcript(sess["id"])
        self.assertEqual(transcript[0]["content"], "record me")
        self.assertEqual(transcript[1]["content"], "streamed answer")

    def test_fetch_ask_is_recorded_as_day_session(self):
        self.client.get("/c/urlok-code-2/ask", params={"q": "one"})
        self.client.get("/c/urlok-code-2/ask", params={"q": "two"})
        sessions = app_mod.db.sessions_for_code("urlok-code-2")
        self.assertEqual(len(sessions), 1)          # same UTC day -> one session
        self.assertEqual(sessions[0]["surface"], "fetch")
        self.assertEqual(len(app_mod.db.transcript(sessions[0]["id"])), 4)

    def test_admin_endpoint_gated(self):
        self.assertEqual(self.client.get("/admin/summary.json").status_code, 401)
        r = self.client.get("/admin/summary.json",
                            headers={"X-Admin-Token": "test-admin-token"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("codes", r.json())

    def test_admin_404_when_token_unset(self):
        saved = os.environ.pop("ADMIN_TOKEN")
        try:
            self.assertEqual(
                self.client.get("/admin/summary.json").status_code, 404)
        finally:
            os.environ["ADMIN_TOKEN"] = saved

    def test_admin_guessing_hits_the_limiter(self):
        import codes as codes_mod
        for _ in range(codes_mod.FAILED_ATTEMPTS_PER_IP_HOUR):
            self.client.get("/admin/summary.json",
                            headers={"X-Admin-Token": "wrong"})
        r = self.client.get("/admin/summary.json",
                            headers={"X-Admin-Token": "test-admin-token"})
        self.assertEqual(r.status_code, 429)     # even the right token: locked
        # Decoupled counters: admin guessing must NOT lock out employers on
        # the other surfaces (re-review finding on PR #74).
        self.assertEqual(self._enter().status_code, 200)

    def test_admin_non_ascii_header_is_401_not_500(self):
        # compare_digest on str raises TypeError on non-ASCII (Starlette
        # decodes headers as latin-1, so obs-text reaches the handler);
        # comparing bytes must yield a clean 401. httpx refuses non-ASCII
        # str headers, so send raw bytes.
        r = self.client.get("/admin/summary.json",
                            headers={b"X-Admin-Token": "wröng".encode("latin-1")})
        self.assertEqual(r.status_code, 401)

    def test_recorded_exchange_carries_real_cost(self):
        # Locks the token -> usage_cost_usd -> DB chain end to end.
        self._enter()
        self.client.post("/chat", json={"message": "cost me"},
                         headers={"X-Candidate-Agent": "1"})
        row = {r["code"]: r for r in app_mod.db.summary()}["plain-code-1"]
        self.assertGreater(row["cost_usd"], 0)

    def test_disclosure_present(self):
        self.assertIn("recorded", self.client.get("/").text)
        self.assertIn("recorded", self.client.get("/c/urlok-code-2").text)


if __name__ == "__main__":
    unittest.main()
