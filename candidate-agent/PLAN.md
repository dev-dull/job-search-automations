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

## Knowledge corpus — well beyond the resume

The agent's knowledge is a **curated corpus**, not just a profile doc: cover
letters, blog posts, video transcripts, project documentation — anything the
candidate chooses to publish to employers, with redactions applied at the
source.

Corpus layout (lives in the private repo, mounted at `CORPUS_PATH`):

```
corpus/
  profile.md              # the spine: identity, headline, links (was PROFILE_PATH)
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

- A **redaction linter** at ingest: `REDACTION_DENYLIST_PATH` points at a
  private list of strings that must never appear (names of target companies,
  phone number, street address, ...). A hit fails startup loudly with the file
  and line — a redaction miss becomes a deploy failure instead of a leak.
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
   Cache reads keep the per-message cost in cents even at 100k+ tokens.
2. **Agentic keyword retrieval (phase 2, or when the corpus outgrows #1).**
   The answer engine becomes a small tool loop: the model sees `profile.md` +
   a corpus index (titles/types/summaries) in full, plus two server-side
   tools — `search_corpus(query)` (SQLite FTS5/BM25 over the docs; the DB
   already exists for session recording) and `read_doc(id)`. The model pulls
   what each question needs. This is the modern replacement for classic
   embeddings-RAG on curated corpora: better precision on exact terms
   (project names, tool names), no embedding pipeline, no chunking tuning,
   and it runs on infrastructure we already have.
3. **Embeddings (only if needed).** If FTS retrieval demonstrably misses
   paraphrased questions at scale, add vector search (sqlite-vec + a local
   embedding model or Voyage API) as a *second* search tool beside FTS, not a
   replacement. This rung may never be needed.

The employer-facing surfaces don't change as the ladder climbs: retrieval
tools are internal to the server-side engine, so MCP callers still see only
`ask_candidate_agent` — control, logging, and the privacy boundary stay
identical.

## Privacy & data separation (same rule as job-store)

This repo is public. **No personal content ships in it — not in code, tests,
fixtures, or prompts.** All personal inputs are runtime-provided paths, and the
k8s deployment mounts them from the private repo exactly like job-store mounts
the resume:

- `CORPUS_PATH` — the curated corpus described above. Deliberately disjoint
  from `RESUME_PATH`'s file and the preferences file: the scoring resume and
  preferences contain things an employer must never see (comp expectations,
  deal-breakers, companies under consideration, commented drafts). Curation
  into `corpus/` is the privacy boundary, not prompt instructions alone.
- `REDACTION_DENYLIST_PATH` (optional but recommended) — the never-appear
  string list enforced at ingest.
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
   instructions + the assembled corpus), streaming and non-streaming entry
   points, token accounting per code. Model via `ANTHROPIC_MODEL`, default
   `claude-sonnet-5` (employer-facing quality is worth more than Haiku
   savings at this traffic level; measured cost per conversation is pennies).
3. `corpus.py` — corpus walker: front-matter parsing, redaction-linter pass,
   token-count report at startup (logs how close the corpus is to the
   full-context ceiling so the ladder climb is visible in advance), mtime
   reload.
4. `codes.py` — access-code file parsing, validation, rate/budget counters.
5. Templates/static — chat page (plain HTML + EventSource, streaming into a
   transcript div; mobile-friendly; clearly labeled as an AI agent).
6. Tests — synthetic corpus fixtures only; auth (valid/expired/revoked),
   rate limiting, corpus assembly + redaction linter (a planted denylist hit
   must fail), MCP tool contract via FastMCP test client.
7. Deploy — Dockerfile + helm chart cloned from job-store's shape (single
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
  without `CORPUS_PATH`, and refuses to start on a redaction-linter hit.

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
- **Retrieval rung 2 lands here if the corpus has outgrown full-context**:
  FTS5 index over the corpus in the same SQLite DB, `search_corpus` /
  `read_doc` tools in the engine loop.

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
3. **Corpus seeding** — user curates/redacts the initial corpus in the
   private repo (the plan treats curation as the hard privacy boundary; I can
   draft the front-matter template, a redaction checklist, and the denylist
   skeleton, but the content selection is theirs). Also: roughly how much
   material exists today (posts x KB, transcripts x hours)? That sizes which
   retrieval rung phase 1 actually starts on.
4. **Disclosure/tone** — any constraints on what the agent should volunteer
   vs. only answer when asked (e.g., availability date, location)?
5. **Recording consent wording** for phase 2 — plain line on the page, or
   also spoken by the agent at conversation start?
