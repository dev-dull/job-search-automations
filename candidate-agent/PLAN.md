# Candidate Agent — research & build plan

An AI agent that potential employers can talk to about the candidate's
employment history, skills, and interests. Two front doors, one brain:

1. **Their own chat client** — the service is a remote MCP server; a recruiter
   adds one URL (plus an access code) to Claude Desktop, claude.ai custom
   connectors, Claude Code, or ChatGPT developer mode, and their assistant can
   interview the agent directly.
2. **A browser URL** — a minimal chat page for people who don't run an AI
   client at all: enter the access code, start asking.

Access is gated by **per-employer unique codes** so every conversation is
attributable to whoever the code was issued to, and any code can be revoked
without affecting the others.

---

## Research findings (July 2026)

- **Remote MCP transport**: Streamable HTTP is the standard; the older
  SSE-only transport is deprecated. The recommended Python stack is
  **FastMCP** (v3.x) which mounts into a FastAPI/uvicorn app — one process can
  serve both the MCP endpoint and the browser UI.
- **Auth support**: all major clients now accept a static bearer header on
  remote MCP servers — Claude Code (`claude mcp add --transport http --header
  "Authorization: Bearer <code>" <url>`), Claude Desktop, claude.ai custom
  connectors (via advanced settings), and ChatGPT developer mode. A
  code-in-URL-path endpoint (`/mcp/<code>`) works everywhere as a fallback but
  leaks the code into proxy/CDN logs, so it's the backup pattern, not the
  primary.
- **OAuth 2.1**: the MCP spec mandates OAuth for public remote servers, and
  clients probe `/.well-known/oauth-protected-resource` on a 401. In practice
  bearer headers work today across the clients we care about. Plan: ship
  bearer-first, keep a minimal OAuth-shim (code-entry page acting as the
  authorization screen, short-lived tokens) as a fast-follow if some client
  refuses plain bearer auth.
- **Agent engine**: for a single-purpose Q&A persona with no server-side tool
  use, the raw **Messages API with prompt caching** is the right engine — it's
  the pattern this repo already uses in job-store, gives full control over the
  system prompt and costs, and streams natively. The Claude Agent SDK or
  Managed Agents only pay off once the agent needs tools/sandboxes; noted as a
  later option, not phase 1.
- **Browser streaming**: SSE (not websockets) — native `EventSource` in the
  browser, native SSE out of the Anthropic API, automatic reconnects, and it
  passes plain HTTP ingresses. One caveat for our ingress: response buffering
  must be off for the stream path.

## Architecture

```
employer's Claude/ChatGPT ──MCP (Streamable HTTP + Bearer code)──┐
                                                                 ▼
employer's browser ──/  code-entry page → SSE chat ──▶  candidate-agent (FastAPI)
                                                        ├─ auth: access-code table
                                                        ├─ answer engine (Messages API,
                                                        │   prompt-cached persona)
                                                        ├─ phase 2: session/transcript log
                                                        └─ profile docs (mounted, private)
                                                                 │
                                                                 ▼
                                                          Anthropic API
```

**One answer engine behind both doors.** The MCP surface does NOT hand raw
documents to the employer's model; it exposes a tool
(`ask_candidate_agent(question)`) that runs the same server-side engine the
browser uses. Consequences, all deliberate:

- Every answer is generated under OUR system prompt — tone, boundaries, and
  what's off-limits are enforced server-side, not left to the caller's model.
- Both surfaces produce identical transcript records (phase 2).
- The private profile documents never leave the server.

A small `get_profile_summary()` tool/resource returns a short public "card"
(name, headline, links) so client UIs have something to render immediately.

## Privacy & data separation (same rule as job-store)

This repo is public. **No personal content ships in it — not in code, tests,
fixtures, or prompts.** All personal inputs are runtime-provided paths, and the
k8s deployment mounts them from the private repo exactly like job-store mounts
the resume:

- `PROFILE_PATH` — the ONLY knowledge source: a curated, employer-facing
  profile document written for this purpose. Deliberately NOT `RESUME_PATH`'s
  file and NOT the preferences file: the scoring resume and preferences
  contain things an employer must never see (comp expectations, deal-breakers,
  companies under consideration, commented drafts). Curating a separate doc is
  the privacy boundary, not prompt instructions alone.
- `AGENT_FAQ_PATH` (optional) — canned answers for expected questions
  (availability, work authorization, links to code/talks).
- `ACCESS_CODES_PATH` (phase 1) — the code table, also private-repo-owned.

The system prompt additionally instructs the agent to decline questions
outside the profile (salary expectations, references' contacts, other
processes) and to redirect contact requests to the candidate's listed email.
Prompt-injection resistance comes primarily from the engine never *having*
sensitive data, secondarily from instructions.

## Access codes

Format: `firstpart-secondpart-checkword` style human-typeable token (e.g.
`maple-K7RT-hazel`), unique per employer/recruiter. Properties per code:

- `label` (who it was issued to), `created_at`, `expires_at` (default 30
  days), `revoked` flag, optional `note`.
- Phase 1 storage: a flat file in the private repo (path via
  `ACCESS_CODES_PATH`, same line-based format family as the company
  adjustments file), reloaded on mtime change. Minting = adding a line +
  `git push`; revoking = removing it. No admin UI needed to start sharing.
- Phase 2 storage: moves into the SQLite DB (needed anyway for session
  records) with a tiny `mint_code.py` CLI; the file stays supported as seed
  input.

Where the code travels:
- MCP: `Authorization: Bearer <code>` header (primary); `/mcp/<code>` path
  accepted as fallback for header-less clients.
- Browser: code-entry form → server-set HttpOnly session cookie; the code
  never sits in the page URL, so it can't be shared by copy-pasting a link.

Abuse guards from day one (this is a public LLM endpoint gated by codes that
strangers hold): per-code rate limit (requests/hour), per-code daily token
budget, hard `max_tokens` per answer, conversation-length cap, and a global
daily budget kill-switch. A revoked/expired code gets a friendly "this link
has expired — contact <email>" rather than a bare 401 on the browser surface.

## Phase 1 — shareable MVP

Goal: a URL + code you can put in an application or hand a recruiter this
week.

Components (new `candidate-agent/` service, sibling of `job-board/`):

1. `app.py` — FastAPI:
   - `POST /mcp` (FastMCP mount, Streamable HTTP) with bearer-code auth
     middleware; tools: `ask_candidate_agent(question)`,
     `get_profile_summary()`.
   - `GET /` — code-entry page (single template, no JS framework).
   - `POST /chat` + `GET /chat/stream` — SSE chat for the browser surface,
     cookie-authed, in-memory per-session history (single replica, matching
     job-store's Recreate deployment model).
   - `GET /healthz`.
2. `engine.py` — Messages API wrapper: prompt-cached system blocks (persona
   instructions + profile + FAQ), streaming and non-streaming entry points,
   token accounting per code. Model via `ANTHROPIC_MODEL`, default
   `claude-sonnet-5` (employer-facing quality is worth more than Haiku
   savings at this traffic level; measured cost per conversation is pennies).
3. `codes.py` — access-code file parsing, validation, rate/budget counters.
4. Templates/static — chat page (plain HTML + EventSource, streaming into a
   transcript div; mobile-friendly; clearly labeled as an AI agent).
5. Tests — synthetic profile fixtures only; auth (valid/expired/revoked),
   rate limiting, engine prompt assembly, MCP tool contract via FastMCP test
   client.
6. Deploy — Dockerfile + helm chart cloned from job-store's shape (single
   replica, private-repo init-container mount for profile/codes, ingress with
   `proxy_buffering off` on the stream route), image built by the existing CI
   pattern, pinned via Homelab GitOps. New hostname (e.g.
   `agent.k8s.deep13.lol` — final name TBD by user; it appears in every
   recruiter's address bar).

Definition of done for phase 1:
- `claude mcp add --transport http --header "Authorization: Bearer <code>"
  https://<host>/mcp` works from a fresh machine, and `ask_candidate_agent`
  answers profile questions.
- The same code unlocks the browser page and streams answers.
- A revoked code stops working on both surfaces within one reload interval.
- Nothing personal exists in the public repo; the service refuses to start
  without `PROFILE_PATH`.

## Phase 2 — session recording & analytics

Everything phase 1 keeps in memory becomes durable and attributable:

- SQLite (`AGENT_DB_PATH`, WAL, 30s busy timeout — job-store lessons) with:
  - `codes` — the phase-1 file promoted to a table.
  - `sessions` — code, surface (`mcp` | `web`), started/last-active/ended
    (duration derivable), IP (from `X-Forwarded-For` via ProxyFix), browser
    `User-Agent`, and for MCP the client name/version from the `initialize`
    handshake (Claude Desktop vs Claude Code vs ChatGPT is visible there).
  - `messages` — session, role, content, timestamp, input/output token
    counts.
- Disclosure line on the chat page and in the MCP server description:
  conversations are recorded and reviewed. (Both ethical and useful — it
  deters abuse.)
- `GET /admin` (separate admin token, not employer codes): sessions per code,
  transcripts, token spend per code, last-seen. Phase 2a can simply be a
  read-only CLI (`report.py`) before any admin UI exists.
- Optional push: a note into Trilium or email when a new session starts —
  "someone at <code label> is talking to your agent right now."

## Phase 3 candidates (not planned in detail)

- OAuth 2.1 PKCE shim if a target client drops bearer support.
- Per-employer tailoring: a code can carry a role/JD context so the agent
  emphasizes relevant experience.
- "Ask me instead" handoff — the agent offers to schedule with the real human
  when questions exceed its scope.
- Multi-candidate support (parameterize the persona) if this becomes a
  shareable template for others.

## Open questions (need the user's call)

1. **Hostname** — `agent.k8s.deep13.lol`, something on `devdull.lol`, or a
   dedicated domain? It's employer-visible branding.
2. **Model** — default `claude-sonnet-5` for quality, or Haiku to start?
3. **Profile doc** — user writes the curated employer-facing profile in the
   private repo (the plan treats it as the hard privacy boundary; I can draft
   a skeleton/checklist of sections, but the content is theirs).
4. **Disclosure/tone** — any constraints on what the agent should volunteer
   vs. only answer when asked (e.g., availability date, location)?
5. **Recording consent wording** for phase 2 — plain line on the page, or
   also spoken by the agent at conversation start?
