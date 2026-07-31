# Candidate Agent — research & build plan

*(Verified 2026-07-30 by a three-way review: an external-claims fact-check,
a reference-deployment audit, and an adversarial internal-consistency pass.
Corrections from that review are folded in below.)*

An AI agent that potential employers can talk to about a candidate's
employment history, skills, and interests. Like everything in this repo, it
is a **tool anyone can adopt for their own job hunt**: this plan describes
what to build and the properties a deployment must provide. Where the agent
runs, what domain it answers on, and what personal content it serves are the
operator's decisions, made at deploy time through runtime configuration —
none of that lives in this repository.

Three front doors, one brain — ordered by how little the employer must do:

1. **A browser URL** — a minimal chat page for people who don't run an AI
   client at all: enter the access code, start asking. Zero setup, works for
   everyone.
2. **Zero-install fetch, from their own AI** — the employer pastes one URL
   into claude.ai, ChatGPT, or Gemini and asks their assistant to interview
   the agent; the assistant makes plain HTTP GETs to an `ask` endpoint. No
   connector, no configuration, nothing installed — web fetch is the one
   capability nearly every assistant already has.
3. **A remote MCP connector, from their own AI** — for the deepest
   integration: the recruiter adds the server URL (plus code) to Claude Code,
   Claude Desktop, claude.ai custom connectors, or ChatGPT developer mode.
   Fully remote — Streamable HTTP runs no code on the employer's machine —
   but adding a connector is a configuration step some users won't take and
   some corporate tenants restrict, which is why it's the third door, not the
   first.

Access is gated by **per-employer unique codes** so access is attributable to
whoever the code was issued to (full conversation records arrive in phase 2),
and any code can be revoked without affecting the others.

---

## Verified constraints (July 2026)

**MCP transport & clients**
- Streamable HTTP is the standard; SSE-only transport is deprecated. Python
  stack: **FastMCP v3** — mount `mcp.http_app(transport="streamable-http")`
  into the FastAPI app (`app.mount("/mcp", mcp_app)` with the mcp app's
  lifespan). Note: `FastMCP.from_fastapi()` does the *opposite* (wraps REST
  endpoints as tools) — not what we want.
- Bearer-header auth verified per client: **Claude Code** (`--header` flag) ✓,
  **Claude Desktop** ✓, **ChatGPT developer mode** ✓ (via header config).
  **claude.ai custom connectors: currently broken for static bearer** — the
  client ignores the configured header and attempts OAuth (open upstream bugs
  as of July 2026). Consequence: the `/mcp/<code>` URL-path form is not a
  backup, it is the *required* path for claude.ai users until upstream fixes
  land.
- No client hard-requires OAuth discovery today: bearer-only servers work with
  graceful fallbacks (the spec mandates RFC 9728 metadata, but enforcement is
  pragmatic). OAuth 2.1 PKCE shim stays a phase-3 contingency — single
  mention, see phase 3.
- Logging which client connected (Claude Desktop vs Code vs ChatGPT) requires
  custom FastMCP middleware capturing `clientInfo` from the `initialize`
  handshake — it is not exposed for free.

**Model & cost (honest numbers)**
- `claude-sonnet-5` (the default): 1M context — a 150k-token corpus leaves
  vast headroom.
- Prompt-cache economics at full corpus (~150k tokens): cold-start cache
  write ≈ **$0.60**, cached follow-ups ≈ $0.03–0.04/message. A 10-question
  recruiter conversation ≈ **$0.50–0.80**, rising ~50% when Sonnet 5 intro
  pricing ends (Sept 2026). Dimes-to-a-dollar per conversation, *not*
  "pennies" — acceptable for the purpose, but per-code budgets are stated in
  dollars for a reason (below).

**Orchestration framework — considered and declined (decision record)**
- LangChain / LlamaIndex / AutoGen were evaluated as an engine layer; the
  engine stays on the direct Messages API. Reasons: (1) the cost model
  depends on precise `cache_control` placement over byte-stable system
  blocks — abstraction layers that rebuild message arrays are where cache
  hits silently die; (2) the workload is one persona + a small tool loop —
  AutoGen targets multi-agent teams (and is mid-merge into Microsoft's Agent
  Framework), LangChain pays off for multi-provider chain composition we
  don't have; (3) LlamaIndex's strength is heavy ingestion/vector pipelines,
  which the retrieval ladder deliberately avoids at rungs 1–2 (curated
  markdown + FTS5 needs no framework). Revisit only at rung 3, where
  LlamaIndex's ingestion utilities may earn a place in the embeddings
  pipeline; a framework adopted earlier would be dependency weight and
  public-endpoint attack surface without payoff.

**Runtime**
- FTS5 (needed for retrieval rung 2) ships in `python:3.13-slim`'s bundled
  SQLite but **not** in `python:3.12-slim`. The image pins 3.13 and asserts
  FTS5 at startup so the retrieval rung can't be silently blocked.
- SQLite means **single process** (`uvicorn --workers 1`); async handles
  concurrent streams within it.

## Deployment requirements (what any operator must provide)

The service is one container. It runs anywhere that can satisfy these
properties — a k8s cluster, a VPS, a container platform:

1. **Public HTTPS reachability.** Employers are strangers on the internet;
   the endpoint must be reachable from outside the operator's network, with
   valid TLS. Self-hosters should verify this explicitly — a hostname that
   resolves internally (split-horizon DNS, LAN-only ingress, a router
   occupying 443) is the classic silent failure. Tunnels (cloudflared,
   Tailscale Funnel), port-forwards, or a VPS relay all work; the app doesn't
   care.
2. **Streaming-friendly ingress.** Both surfaces hold open streaming
   responses. The proxy in front must not buffer them, and its write/idle
   timeouts bound stream lifetime — the app and chat page handle reconnects,
   but operators should know their proxy's limits.
3. **Private content mounted at runtime.** The corpus, access codes, and
   denylist are files the operator supplies via mounted paths (env vars
   below). They live wherever the operator keeps private data — typically a
   private git repo. **How fresh the mount is bounds revocation latency**: a
   sync mechanism that pulls every minute gives ~2-minute worst-case
   revocation; a clone-once-at-boot mount means revocation waits for a
   restart. The reference k8s manifests use a git-sync sidecar with a short
   interval and loud failure logging; operators choosing other mounts should
   document their own latency.
4. **An Anthropic API key** with a spend limit the operator is comfortable
   with (the app's own budget guards are a second line, not the first).

The repo ships: the container image build, a helm chart with the
ingress/host/mount details left as values, and a compose file for
non-k8s operators. No hostnames, providers, or operator infrastructure are
baked in.

## Architecture

```
employer's Claude/ChatGPT ──MCP (Streamable HTTP + Bearer code)──┐
employer's Claude/ChatGPT ──web fetch: GET /c/<code>/ask?q=… ────┤
                                                                 ▼
employer's browser ──/  code-entry page → SSE chat ──▶  candidate-agent (FastAPI)
                                                        ├─ auth: access-code file (ph.1) / table (ph.2)
                                                        ├─ answer engine (Messages API,
                                                        │   prompt-cached corpus)
                                                        ├─ phase 2: session/transcript log
                                                        └─ corpus (mounted, private, redacted)
                                                                 │
                                                                 ▼
                                                          Anthropic API
```

**One answer engine behind both doors.** The MCP surface does NOT hand raw
documents to the employer's model; it exposes a tool
(`ask_candidate_agent(question)`) that runs the same server-side engine the
browser uses. Consequences, all deliberate:

- Every answer is generated under the operator's system prompt — tone,
  boundaries, and what's off-limits are enforced server-side, not left to the
  caller's model.
- The private corpus never leaves the server.
- Both surfaces log through the same engine — but the records differ by
  nature (stated honestly): the **browser** surface logs full conversations
  with server-held history; the **MCP** surface logs individual Q/A tool
  calls, grouped best-effort by MCP transport session — the employer's
  surrounding conversation is never visible to the server, and one connector
  session may span several of their chats. To reduce the quality asymmetry on
  follow-ups, the engine threads its own short history keyed on the MCP
  session ID (documented as best-effort, since transport sessions ≠
  conversations).

A small `get_profile_summary()` tool/resource returns a short public "card"
(name, headline, links) so client UIs have something to render immediately.

**The fetch surface (verified July 2026).** claude.ai, ChatGPT, and Gemini
can all fetch user-pasted URLs mid-conversation, repeatedly, with query
parameters — which makes a plain GET endpoint an API their assistants can
drive with no setup:

- `GET /c/<code>` — a small markdown landing page: who this agent is, how to
  ask (`/c/<code>/ask?q=…`), and ground rules. The employer pastes this one
  URL; their assistant reads the instructions and takes it from there.
- `GET /c/<code>/ask?q=<question>` — runs the same server-side answer engine,
  returns a markdown answer. Stateless by design (each fetch independent);
  the engine threads best-effort continuity keyed on the code + client, same
  spirit as the MCP session handling.
- Constraints honored from live client behavior: keep total URLs short
  (Claude's fetcher caps around 250 chars — codes stay compact and the
  landing page says "ask concise questions"); answers are served
  `Cache-Control: no-store` because at least one assistant caches fetches for
  ~15 minutes (a follow-up asking the same question should still work);
  responses stay small (tens of KB).
- `robots.txt` allows the known assistant fetcher agents (`Claude-User`,
  `ChatGPT-User`, Google's fetcher) on `/c/*` and disallows everything else —
  the gate is the code, not obscurity, but there's no reason to invite
  generic crawlers.
- Reach check from the research: assistant web fetch is available in more
  corporate environments than custom connectors, but not all (some tenants
  disable web tools entirely) — which is exactly why the browser chat page
  remains door #1.

Not built, with reasons recorded: A2A agent cards (no consumer assistant
acts as an A2A client yet), custom GPT Actions (requires the recruiter to
build a GPT, and enterprise tenants commonly block them — revisit only as an
opt-in extra), email-in agents (async, poor fit).

## Knowledge corpus — well beyond the resume

The agent's knowledge is a **curated corpus**, not just a profile doc: cover
letters, blog posts, video transcripts, project documentation — anything the
candidate chooses to publish to employers, with redactions applied at the
source.

Corpus layout (operator-owned, mounted at `CORPUS_PATH`):

```
corpus/
  profile.md              # the spine: identity, headline, links
  faq.md                  # canned answers (availability, authorization, ...)
  posts/*.md              # blog posts
  cover-letters/*.md      # redacted cover letters
  transcripts/*.md        # video/talk transcripts
  projects/*.md           # per-project documentation: what it is, the problem,
                          # architecture/decisions, outcomes, repo link if public
```

Each document carries YAML front-matter metadata: `title`, `type`, `date`,
`tags`, and optional `summary` (one line, used in the corpus index the model
sees). No central manifest to keep in sync — the file *is* the record.

**Redaction is a source-side workflow, not a runtime filter.** Documents are
redacted before they enter `corpus/` (e.g. cover letters get `[COMPANY]`
placeholders). Two safety nets on top:

- A **redaction linter** at ingest: `REDACTION_DENYLIST_PATH` points at an
  operator-supplied list of strings that must never appear (names of target
  companies, phone number, street address, ...). Semantics, precisely: a hit
  at **startup fails startup loudly** with file and line; a hit on a **live
  reload** (the content sync delivered a bad document) **rejects the new
  corpus, keeps serving the last-good in-memory corpus, and alerts loudly** —
  a redaction miss becomes a blocked update, never a leak and never a
  crash-loop.
- The engine's system prompt still declines out-of-bounds topics (comp,
  references' contacts, active processes), as the second layer.

**Retrieval strategy — a ladder, not a vector DB by default.** The right
mechanism depends on corpus size in tokens, and the design climbs one rung
only when the corpus outgrows the current one:

1. **Full-context (phase 1).** The whole corpus is concatenated (with
   per-doc headers from front-matter) into prompt-cached system blocks.
   Viable up to roughly ~150k tokens (~600KB of text) — which comfortably
   holds a profile, dozens of posts/letters, and several transcripts. Zero
   retrieval infrastructure, zero retrieval misses: the model always sees
   everything, which is exactly what makes small-corpus RAG underperform.
   (Cost at this ceiling: see Verified constraints — dimes per conversation.)
2. **Agentic keyword retrieval (phase 2, or when the corpus outgrows #1).**
   The answer engine becomes a small tool loop: the model sees `profile.md` +
   a corpus index (titles/types/summaries) in full, plus two server-side
   tools — `search_corpus(query)` (SQLite FTS5/BM25 over the docs; the DB
   already exists for session recording, and the 3.13 base image + startup
   assert guarantee FTS5) and `read_doc(id)`. The model pulls what each
   question needs. This is the modern replacement for classic embeddings-RAG
   on curated corpora: better precision on exact terms (project names, tool
   names), no embedding pipeline, no chunking tuning.
3. **Embeddings (only if needed).** If FTS retrieval demonstrably misses
   paraphrased questions at scale, add vector search (sqlite-vec + a local
   embedding model or Voyage API) as a *second* search tool beside FTS, not a
   replacement. This rung may never be needed.

The employer-facing surfaces don't change as the ladder climbs: retrieval
tools are internal to the server-side engine, so MCP callers still see only
`ask_candidate_agent` — control, logging, and the privacy boundary stay
identical.

## Privacy & data separation (the repo-wide rule)

This repo is public and contains **no personal content — not in code, tests,
fixtures, or prompts.** All personal inputs are runtime-provided paths the
operator mounts from their own private storage:

- `CORPUS_PATH` — the curated corpus described above. Deliberately disjoint
  from any scoring-resume or preferences files an operator uses with the
  job-board tools: those contain things an employer must never see (comp
  expectations, deal-breakers, companies under consideration, drafts).
  Curation into `corpus/` is the privacy boundary, not prompt instructions
  alone.
- `REDACTION_DENYLIST_PATH` (optional but recommended) — the never-appear
  string list enforced at ingest.
- `ACCESS_CODES_PATH` (phase 1) — the code table, operator-owned.

Full env inventory: `ANTHROPIC_API_KEY` (secret), `ANTHROPIC_MODEL` (default
`claude-sonnet-5`), `CORPUS_PATH`, `ACCESS_CODES_PATH`,
`REDACTION_DENYLIST_PATH`, `AGENT_DB_PATH` (phase 2), `ADMIN_TOKEN`
(phase 2), plus rate/budget knobs (`RATE_LIMIT_PER_HOUR`,
`DAILY_BUDGET_USD_PER_CODE`, `DAILY_BUDGET_USD_GLOBAL`) with sane defaults.

## Access codes

Format: `firstpart-secondpart-checkword` style human-typeable token (e.g.
`maple-K7RT-hazel`), unique per employer/recruiter. Properties per code:

- `label` (who it was issued to), `created_at`, `expires_at` (default 30
  days), `revoked` flag, optional `note`, and a per-code `url_auth` flag
  (below).
- Phase 1 storage: a flat file at `ACCESS_CODES_PATH`, reloaded when the
  mounted content changes. Minting = adding a line and letting the content
  sync deliver it; revoking = removing it. **Revocation latency = the
  operator's content-sync interval + reload** (~2 minutes with the reference
  sidecar). No admin UI needed to start sharing.
- Phase 2 storage: moves into the SQLite DB (needed anyway for session
  records) with a tiny `mint_code.py` CLI; the file stays supported as seed
  input.

Where the code travels:
- MCP: `Authorization: Bearer <code>` header (primary — Claude Code, Claude
  Desktop, ChatGPT). `/mcp/<code>` path form: **per-code opt-in via the
  `url_auth` flag**, issued deliberately for claude.ai users (whose connector
  currently can't send static headers). URL-authed codes are accepted knowing
  the code lands in access logs — so app logs scrub `/mcp/*` paths (operators
  should scrub their ingress logs likewise), and those codes default to
  shorter expiry.
- Fetch surface: the code is inherently URL-carried, so it uses the same
  `url_auth` opt-in flag as the MCP path form — one flag governs both
  URL-carried uses, with the same log-scrubbing and shorter-expiry defaults.
  A code minted for a claude.ai recruiter works for both their connector and
  their assistant's plain fetches.
- Browser: code-entry form → server-set HttpOnly cookie (`SameSite=Lax`,
  fresh session ID issued at code entry); `/chat` additionally requires a
  custom header (free via `fetch`, blocks cross-site form posts). The code
  never sits in the page URL. **Every cookie-authed request re-validates the
  code against the current table**, so revocation kills live sessions too.

Abuse guards from day one (this is a public LLM endpoint gated by codes that
strangers hold):
- Per-code request rate limit and **cost-weighted daily budget** — the budget
  sums the API's reported `input`, `cache_read`, `cache_creation`, and
  `output` token counts at their respective prices (i.e. it tracks dollars).
  Cached corpus reads are ~10% of input price, which is what makes
  full-context rung 1 and per-code budgets compatible.
- Hard `max_tokens` per answer; per-conversation turn cap on the browser
  surface; per-transport-session tool-call cap on MCP (a "conversation" cap
  is unenforceable there — see architecture note).
- Global daily budget kill-switch.
- **Failed-attempt limiter**: per-IP + global circuit breaker on invalid-code
  attempts (the codes are deliberately human-typeable, i.e. low-entropy;
  unthrottled guessing is otherwise the one ungated path).
- Phase-1 counters are **in-memory and reset on restart** — a known,
  accepted gap at single-replica scale; thresholds are set conservatively
  because of it, and counters move into the DB in phase 2.
- A revoked/expired code gets a friendly "this link has expired — contact
  <email>" rather than a bare 401 on the browser surface.

## Phase 1 — shareable MVP

Goal: a URL + code the operator can put in an application or hand a
recruiter. (Operators must clear deployment requirement #1 — public
reachability — before anything here matters outside their LAN.)

Components (this `candidate-agent/` directory, sibling of `job-board/`):

1. `app.py` — FastAPI (uvicorn, single process):
   - `/mcp` — FastMCP `http_app` mounted; bearer-code middleware +
     `/mcp/<code>` opt-in path handling; `initialize`-capture middleware
     (client name/version, for phase-2 logging); tools:
     `ask_candidate_agent(question)`, `get_profile_summary()`.
   - `GET /c/<code>` + `GET /c/<code>/ask` — the zero-install fetch surface
     (markdown responses, `no-store`, url_auth-gated, per-code rate limits
     shared with the other surfaces).
   - `GET /robots.txt` — allow known assistant fetchers on `/c/*`, disallow
     the rest.
   - `GET /` — code-entry page (single template, no JS framework).
   - `POST /chat` + SSE stream — cookie-authed per the codes section;
     in-memory history; client-side reconnect handling (proxies commonly cap
     stream lifetime).
   - `GET /healthz`.
   - Client IPs behind a proxy via uvicorn `--proxy-headers`.
2. `engine.py` — Messages API wrapper: prompt-cached system blocks (persona
   instructions + the assembled corpus), streaming and non-streaming entry
   points, cost-weighted usage accounting per code, short per-MCP-session
   history. Model via `ANTHROPIC_MODEL`.
3. `corpus.py` — corpus walker: front-matter parsing, redaction-linter pass
   (startup-fail vs reload-reject semantics as specified), token-count report
   at startup (logs headroom against the full-context ceiling), reload on
   mounted-content changes.
4. `codes.py` — access-code file parsing, validation (incl. `url_auth` flag),
   rate/budget counters, failed-attempt limiter.
5. Templates/static — chat page (plain HTML + EventSource; mobile-friendly;
   clearly labeled as an AI agent).
6. Tests — synthetic corpus fixtures only; auth (valid/expired/revoked,
   bearer AND url-path form, cookie re-validation), failed-attempt limiter,
   rate limiting, corpus assembly + redaction linter (planted denylist hits
   at startup and at reload), MCP tool contract via FastMCP test client.
7. Packaging — Dockerfile (**`python:3.13-slim`**, FTS5 startup assert,
   uvicorn `--workers 1`, non-root, read-only-rootfs-compatible); a helm
   chart with host/ingress-class/TLS and the private-content mount left as
   values (an optional git-sync sidecar with a short interval and loud
   failures as the reference content-sync); a docker-compose example for
   non-k8s operators. Image built by this repo's existing CI pattern.

Definition of done for phase 1:
- `claude mcp add --transport http --header "Authorization: Bearer <code>"
  https://<host>/mcp` works from a fresh machine, and `ask_candidate_agent`
  answers corpus questions.
- A `url_auth`-flagged code works via `/mcp/<code>` (the claude.ai path).
- Pasting `/c/<code>` into a fresh claude.ai or ChatGPT conversation and
  asking three follow-up questions produces three grounded answers via the
  fetch surface (the "zero-install demo" — this is the door most recruiters
  will actually use).
- The same code unlocks the browser page and streams answers, surviving a
  forced reconnect.
- A revoked code stops working on both surfaces — including already-open
  browser sessions — within the deployment's stated content-sync latency.
- Nothing personal exists in this repo; the service refuses to start
  without `CORPUS_PATH`; a planted denylist hit blocks startup, and a
  planted hit delivered via content sync is rejected while the last-good
  corpus keeps serving.
- Verified reachable from a network that is not the operator's own.

## Phase 2 — session recording & analytics

Everything phase 1 keeps in memory becomes durable and attributable:

- SQLite (`AGENT_DB_PATH`, WAL, generous busy timeout — a lesson the
  job-board tools learned the hard way) with:
  - `codes` — the phase-1 file promoted to a table.
  - `sessions` — code, surface (`mcp` | `web` | `fetch`), started/last-active/ended,
    IP (proxy headers), browser `User-Agent`, and for MCP the client
    name/version captured by the `initialize` middleware. For `web` a session
    is a real conversation; for `mcp` it is a transport session (best-effort
    grouping — documented, not pretended away).
  - `messages` — session, role, content, timestamp, token counts by class
    (input / cache_read / cache_creation / output) so per-code cost is exact.
- Rate/budget counters move from memory into the DB (restart-proof).
- Disclosure line on the chat page and in the MCP server description:
  conversations are recorded and reviewed. (Both ethical and useful — it
  deters abuse.)
- `GET /admin` (separate `ADMIN_TOKEN`; operators can additionally shield it
  with whatever forward-auth their ingress offers): sessions per code,
  transcripts, cost per code, last-seen. Phase 2a can be a read-only CLI
  (`report.py`) before any admin UI exists.
- Optional webhook on session start ("someone at <code label> is talking to
  your agent right now") — operators point it at email, chat, or their notes
  system.
- **Retrieval rung 2 lands here if the corpus has outgrown full-context**:
  FTS5 index over the corpus in the same SQLite DB, `search_corpus` /
  `read_doc` tools in the engine loop.

## Phase 3 candidates (not planned in detail)

- OAuth 2.1 PKCE shim — the contingency if a target client drops bearer
  support or claude.ai's header bug outlives its workaround (single home for
  this item; the code-entry page would double as the authorization screen).
- Per-employer tailoring: a code can carry a role/JD context so the agent
  emphasizes relevant experience.
- "Ask me instead" handoff — the agent offers to schedule with the real human
  when questions exceed its scope.
- A corpus-scaffolding helper: front-matter templates, a redaction checklist,
  and a denylist skeleton to help new operators seed their corpus safely.

## Operator deployment checklist (decisions this repo does not make)

1. **Exposure & hostname** — how the service becomes publicly reachable
   (tunnel, port-forward, VPS relay) and what employer-visible hostname it
   answers on, with TLS.
2. **Content home & sync** — where the private corpus/codes live and what
   sync mechanism delivers changes (this sets revocation latency; document
   it).
3. **Corpus seeding** — what material goes in (posts, letters, transcripts,
   project docs), redacted at source; corpus token size determines the
   retrieval rung.
4. **Model & budgets** — `ANTHROPIC_MODEL` and the rate/budget knobs, plus a
   provider-side spend limit.
5. **Tone & disclosure** — what the agent volunteers vs. answers only when
   asked; recording-disclosure wording (phase 2).
