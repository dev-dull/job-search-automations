# Server-Side Scoring — Design Document

**Status:** Proposed. No code written yet.
**Author:** Alastair Drong + Claude
**Date:** 2026-05-19

## Goal

Move the Anthropic-based fit-scoring call out of the Firefox extension and into
the `job-store` backend. The plugin becomes a thin client (extract, POST, render);
the backend owns the resume, the API key, the prompt, and the schema.

This makes the Firefox plugin, the future discovery bot, and any other future
consumer share one source of truth for "how does this candidate fit this role?"

## Current state (problems this addresses)

| Concern | Today | After |
|---|---|---|
| Anthropic API key location | `browser.storage.local` (one per browser, set via plugin options page) | Backend env var, set once |
| Resume content of record | `browser.storage.local` cached copy, manually re-loaded via the plugin's "Load now" button | `resume_details.yaml` on disk, read by backend at scoring time |
| Prompt / schema definitions | Hard-coded in `popup.js` (the plugin) | One Python module in `job-store/` |
| Re-scoring an existing job | Re-visit the URL in Firefox; the plugin always re-scores | A backend endpoint can re-score any saved job |
| Discovery bot scoring | Would have to duplicate the scoring logic in `job-bot/` | Discovery bot just POSTs and reads the response |
| Manual paste | Plugin-only | Available via the inbox UI as well, optionally |
| Cost tracking | Implicit (you see usage in the popup) | Backend can log per-call usage to SQLite for spend analysis |

The plugin's *extraction* logic (LinkedIn anchor, Greenhouse-embed API fallback,
ATS-host detection, etc.) stays in the plugin. The plugin still owns "what is the
JD on this page" — that's a browser-side problem.

## Proposed architecture

```
   ┌──────────────────────┐                  ┌──────────────────────────────┐
   │  Firefox plugin      │                  │  job-store (Flask, local)    │
   │                      │  POST /jobs/score│                              │
   │  extract JD ────────►├─────────────────►│  • read resume_details.yaml  │
   │                      │  { url, title,   │  • call Anthropic Haiku 4.5  │
   │  render result ◄─────┤    description,  │  • upsert row in SQLite      │
   │                      │    ats_platform }│  • return score + analysis   │
   └──────────────────────┘                  │                              │
                                             │  also:                       │
   ┌──────────────────────┐                  │  GET /jobs/<id>/rescore      │
   │  discovery bot       │  POST /jobs/score│  GET /resume                 │
   │                      ├─────────────────►│  GET /companies/<id>/...     │
   │  iterate companies   │  (one per role)  │                              │
   └──────────────────────┘                  └──────────────────────────────┘
```

The plugin no longer needs:
- The Anthropic API key
- The resume content
- The `FIT_SCHEMA`, `SYSTEM_INSTRUCTIONS`
- The `MODEL` constant
- The `scoreJob()` function

The plugin still needs:
- The `extractInPage` logic (LinkedIn anchor, Greenhouse API fallback, longest-text fallback)
- The `detectAts` logic
- The popup UI
- The backend URL (already in `browser.storage.local`)

## API surface (additions and modifications to `job-store`)

### `POST /jobs/score` — modified

**Current** payload (the plugin already computes the score client-side):
```json
{
  "url": "https://...",
  "company": "Optional",
  "title": "Optional",
  "description": "string",
  "ats_platform": "greenhouse|lever|ashby|workday|linkedin|other",
  "discovered_by": "plugin",
  "fit_score": 82,
  "analysis": { "summary": "...", "strengths": [...], "gaps": [...], "recommendation": "..." }
}
```

**New** payload (plugin sends the JD, backend scores it):
```json
{
  "url": "https://...",
  "title": "Optional",
  "description": "string",
  "ats_platform": "greenhouse|lever|ashby|workday|linkedin|other",
  "discovered_by": "plugin"
}
```

**New** response:
```json
{
  "id": 123,
  "rank_score": 80,
  "status": "ranked",
  "fit_score": 82,
  "company": "Voxel51",
  "analysis": {
    "summary": "...",
    "strengths": [...],
    "gaps": [...],
    "recommendation": "strong_match|consider|weak_match|skip"
  },
  "usage": {
    "input_tokens": 3803,
    "cache_read_input_tokens": 7719,
    "cache_creation_input_tokens": 0,
    "output_tokens": 108,
    "cost_usd": 0.0089
  }
}
```

**Backwards compatibility:** if the request includes `fit_score` and `analysis`,
backend skips its own Anthropic call and just persists what was sent. This lets
the plugin be migrated incrementally — old plugin builds still work.

### `GET /resume` — new

Reads `resume_details.yaml` from a configured path. Returns plain text. Used by
the plugin to "refresh resume now" or to bootstrap a new plugin install.

```json
{
  "content": "<full YAML content>",
  "path": "/path/to/resume_details.yaml",
  "mtime": "2026-05-19T14:21:00Z",
  "sha256": "..."
}
```

### `POST /jobs/<id>/rescore` — new

Re-runs scoring against the stored `description`. Optional payload:

```json
{ "force": true }  // skip the "already scored recently" guard
```

Use case: after a resume change, re-score the top N jobs to see whether the
new resume framing moves any borderline scores into the "strong_match" zone.

### `POST /companies/<id>/rebuild-deny-list` — already designed in chunk 2

No change. Reads role titles from the company's ATS API, calls Anthropic, writes
the new deny list. Uses the same Anthropic client as scoring.

## Anthropic client module (`job-store/anthropic_client.py`)

One module owns:
- API key loading (`os.environ["ANTHROPIC_API_KEY"]`)
- Resume loading + caching (read `resume_details.yaml`, watch mtime, refresh)
- The `score_job(description, url, title, ats_platform)` function
- The `generate_deny_list(role_titles)` function
- The `MODEL = "claude-haiku-4-5"` constant + the `FIT_SCHEMA`

Pseudocode:

```python
def score_job(*, description, url, title, ats_platform):
    resume = _get_resume_cached()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        system=[
            {"type": "text", "text": SYSTEM_INSTRUCTIONS},
            {"type": "text", "text": f"<resume>\n{resume}\n</resume>",
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": _format_job_prompt(...)}],
        output_config={"format": {"type": "json_schema", "schema": FIT_SCHEMA}},
    )
    return _parse_fit_response(response)
```

Prompt caching keeps per-scoring cost at ~$0.01 across many jobs.

## Plugin changes (`firefox-plugin/popup.js`)

Delete:
- `FIT_SCHEMA`, `SYSTEM_INSTRUCTIONS`, `MODEL`, `API_URL`, `MAX_DESCRIPTION_CHARS`
- `scoreJob()` function
- Options page fields for API key and resume content
- `browser.storage.local` reads/writes for `apiKey`, `resume`, `resumeFilePath`

Add (or keep):
- `extractInPage`, `extractCurrentTab`, `detectAts` — unchanged
- `sendToBackend` — renamed to `scoreViaBackend`, returns the full analysis
- `renderResult` — unchanged structurally, just receives the response from the
  backend instead of computing locally

Net result: the plugin becomes about half its current size and has zero external
dependencies beyond the local backend.

## Migration phases

**Phase 0 — Today:** plugin does scoring; backend just persists.

**Phase 1 — Add `/resume` endpoint and the new `/jobs/score` server-side path.**
Backend can score on its own; plugin keeps doing client-side scoring. New
endpoint is dormant for the plugin but ready for the discovery bot.

**Phase 2 — Discovery bot uses server-side scoring.** Bot POSTs raw JDs to
`/jobs/score` without `fit_score`/`analysis`; backend handles scoring. This
proves the path works at modest volume.

**Phase 3 — Plugin migrates.** Plugin stops calling Anthropic directly. Submits
raw JD to `/jobs/score`. Renders the response. Old behavior preserved as
fallback if backend is unreachable.

**Phase 4 — Cleanup.** Plugin options page loses the API-key + resume fields.
The plugin only needs the backend URL.

Each phase is independently shippable.

## Failure modes and fallbacks

| Scenario | Today | After |
|---|---|---|
| Backend unreachable | Plugin still scores (Anthropic direct), can't save | Plugin scores via cached client-side fallback only if user has chosen to keep one; otherwise shows "backend offline, no score" |
| Anthropic API down | Plugin fails | Backend fails; plugin shows "scoring unavailable" |
| Stale resume | Plugin's `browser.storage.local` copy can drift from disk | Backend reads `resume_details.yaml` per call (cheap), so always fresh |
| API key rotated | User opens plugin options, pastes new key | User updates one env var, restarts `flask` |
| Cost runaway | Plugin doesn't track | Backend logs every call's `usage` to SQLite; easy to query |

**Recommended fallback policy:** the plugin keeps the option (off by default)
to fall back to a client-side Anthropic call if the backend doesn't respond
within ~5 seconds. This is only useful if the user wants to score on the road
without their job-store running. Most days the fallback is unused; on the few
days it matters (laptop offline, on a plane, etc.) it salvages the workflow.

## Open questions

1. **Should the plugin display Gemini's deep analysis when available?** The Gemini
   workflow produces 4 scores; the plugin only shows one. Could add a "deeper
   analysis available — view in inbox" affordance once a branch has been pushed.
   Out of scope for this proposal but worth considering later.

2. **One model, or both at the backend?** This proposal centralizes on Haiku 4.5
   (the plugin's current model). The Gemini workflow stays separate (different
   prompt structure, different purpose: deep analysis after creating a branch).
   Long-term, could converge on one — but that's a separate design.

3. **Resume hot-reload vs explicit reload?** Cheapest: re-read `resume_details.yaml`
   on every scoring call. The file is small (~20KB) and reads are local.
   Alternative: cache + watch mtime. Recommendation: start with re-read-per-call;
   optimize only if profiling shows it matters.

4. **Where does the bot fit?** The bot becomes the discovery driver:
   for each configured company, fetch role titles → filter against deny list →
   for each surviving role, POST description to `/jobs/score`. No bot-side
   Anthropic client. The bot is purely "iterate and dispatch."

## Non-goals

- Replacing the Gemini deep-analysis workflow. It runs after creating an
  application branch, produces a different shape of analysis, and stays as-is.
- Building a web UI for editing the resume. Resume edits still happen via
  `git` and `resume_details.yaml`.
- Multi-user support. This is a single-user tool.
- Authentication on the backend. It's bound to `127.0.0.1` and remains so.

## Tradeoffs accepted

- Plugin gains a hard dependency on the local backend running. Without it, the
  plugin shows "backend offline." (Mitigated by the optional client-side
  fallback.)
- The backend now needs an Anthropic API key. One more secret to manage.
- Latency for scoring goes up slightly: extract → HTTP POST → backend Anthropic
  call → response. The HTTP hop is local, so ~10-20ms overhead per call.

## Cost analysis

No change. Same model, same prompt structure, same prompt caching. The Anthropic
call moves from browser to backend; the wire-cost is identical. The backend
adds ~20ms latency and gains the ability to log per-call usage for spend
tracking, which the plugin can't currently do.

## Concrete next step (if/when this is greenlit)

Smallest meaningful piece:

1. Add `GET /resume` endpoint to `job-store/app.py` (reads `resume_details.yaml`,
   returns the content + mtime). ~20 lines.
2. Add `anthropic_client.py` module with `score_job()` that mirrors `popup.js`'s
   current `scoreJob()` exactly. ~80 lines.
3. Extend `POST /jobs/score` to call `score_job()` when no `fit_score` is
   provided. ~10 lines.
4. Test by POSTing a JD to the backend (via curl) without a fit_score and
   verifying the response shape matches what the plugin expects.

That's it for Phase 1. Discovery bot consumption (Phase 2) and plugin migration
(Phase 3) come later.
