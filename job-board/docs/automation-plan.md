# Job-search automation plan

> **Status: historical planning document.** This is the original plan; the Job
> Board has since shipped and evolved past some of the decisions below. Notably:
> the poller is now a pure HTTP client (no shared DB) running as a Kubernetes
> CronJob, not a homelab cron; job-store is containerized and deployed via a Helm
> chart and can be exposed over Ingress + TLS (with an auth caveat), not strictly
> local-only; the autonomous crawler and local-Gemma pre-filter were never built.
> For how things actually work today, see the [root README](../../README.md#how-it-all-works-together)
> and [`../README.md`](../README.md). Kept for design rationale.

## Goal

Replace the manual "find a job → cut a branch → run the pipeline" loop with an automated discovery, scoring, and ranking system that surfaces the highest-fit, highest-likelihood-of-callback positions first. Once the user picks a position to apply to, hand off to the existing branch-per-job CI/CD pipeline (`process-resume.yaml` et al.) unchanged.

## Architecture

```
[ discovery: 3 producers ]
        │
        ▼
   [ shared store ] ──► [ analysis worker ] ──► [ ranker ] ──► [ local webpage ]
                                                                     │
                                                          (user clicks "apply")
                                                                     ▼
                                                       [ existing resume-repo CI/CD ]
                                                                     │
                                                          (callback / ghost / offer)
                                                                     ▼
                                                          [ outcome tracking ] ──┐
                                                                                  │
                                                          feeds platform_factor ◄┘
```

## Discovery methods

Three independent producers writing to the same shared store. Each gets its own planning session and document — only the interface matters here.

1. **Targeted-company poller** — given a configurable list of careers-page URLs, polls on a schedule.
2. **Browser plugin** — recognizes job-search sites and captures listings as the user browses.
3. **Autonomous crawler** — long-running agent that walks job boards / new company career pages on a schedule.

All three emit the same record shape (see schema). Suggested build order: poller → plugin → crawler. Poller is the smallest viable producer and validates the shared pipeline end-to-end before more complex producers exist.

## Shared pipeline

### Store

**Decision:** Relational DB, not a queue. Start with SQLite; migrate to Postgres only if multiple writers or remote access become a real need.

Rationale: jobs move through stages and we need dedupe-by-URL, querying for ranking, and stage-transition updates — a queue alone can't do that. The `status` column drives the state machine; workers `SELECT … WHERE status = ? ORDER BY discovered_at LIMIT 1` to pull next work.

**`jobs` table:**

| column | type | notes |
|---|---|---|
| id | int PK | |
| url | text UNIQUE | dedupe key across all 3 producers |
| company | text | |
| title | text | |
| description | text | full JD body |
| ats_platform | text | greenhouse, ashby, workday, lever, icims, … |
| posted_at | date | drives age-decay in ranking |
| discovered_at | timestamp | |
| discovered_by | text | poller / plugin / crawler |
| status | text | discovered, analyzing, ranked, applied, closed |
| fit_score | real NULL | 0–100, set by analysis step |
| analysis_json | text NULL | full structured output (fit, strengths, weaknesses, company impressions) |
| rank_score | real NULL | computed ranking score; recomputed on read or on schedule |
| applied_at | timestamp NULL | |
| branch | text NULL | resume-repo branch name once applied |

**`outcomes` table** (1:1 with `jobs` once applied; mirrors current CSV columns):

| column | type | notes |
|---|---|---|
| job_id | int FK → jobs.id | |
| referral | bool | did the application include a referral |
| callback | bool | |
| callback_at | date NULL | |
| ghosted | bool | default flips true after N days with no contact (configurable) |
| offer | bool | |
| notes | text | |

This replaces the CSV outright once outcome tracking is in place.

### Analysis worker

Pulls `status='discovered'` rows, runs analysis, writes `fit_score` + `analysis_json`, advances to `status='ranked'`. Output shape mirrors the existing `gemini-qualified` action so the rest of the pipeline doesn't care which model produced it.

**Path differs by producer.** The two-stage local-prefilter design originally drafted here was hedging against the autonomous crawler's potentially high volume. At browser-plugin volume (intrinsically bounded by what the user looks at, ~50/day) the math doesn't justify local infra. Settled positions:

| Producer | Model path |
|---|---|
| Browser plugin (`firefox-plugin/`) | **Anthropic API + Claude Haiku 4.5**, prompt-cached resume. ~$5–12/month at expected volume. Already shipped; no local infra needed. |
| Targeted-company poller | Same as plugin to start (shared analysis worker); revisit if volume turns out large. |
| Autonomous crawler | **Two-stage** — local Gemma (pre-filter, on homelab RTX 2080 once available) + Anthropic API for the survivors. This is where the local-inference investment actually pays back. |

**Interim hosting (until homelab GPU is ready):** Anthropic API only, no local Gemma. The crawler isn't built yet anyway, so this isn't a stopgap for anything in production — just the default for the plugin and poller paths.

**Why Anthropic over Gemini for the new pipeline:** consolidates with everyday Claude Code usage (one vendor, one key, one SDK pattern), and the existing CI/CD's Gemini integration is unchanged — that's the *applicant-facing* tooling (tailor, cover-letter, last-looks) which is a different concern. Decoupling the discovery analyzer from the application tooling is a feature: the two pipelines can evolve independently.

**Open items still worth measuring:**

- Bake-off across producers: once the autonomous crawler exists, run 5–10 historical CSV rows through Haiku-only vs. Gemma-prefilter+Haiku and compare cost-per-correct-ranking before locking the crawler's analysis path.
- Confirm Gemma generation when starting the crawler subproject — the homelab work targets whatever's current at build time.

### Ranking

`rank_score` is the primary sort key in the UI. Recomputed daily so age-decay actually moves things over time.

**Skeleton formula** (weights to tune against real data):

```
rank_score = fit_score * age_decay(posted_at) * platform_factor(ats_platform)

age_decay(d):
  days = today - d
  return clamp(1.0 - days / 45, 0.3, 1.0)
  # rationale: 45-day half-life-ish; postings older than that
  # likely have a candidate further in the pipeline already.
  # 0.3 floor keeps stale jobs visible rather than dropping them.

platform_factor(p):
  global_rate    = total_callbacks / total_applied
  # Bayesian smoothing for platforms with low n
  platform_rate  = (platform_callbacks + 2) / (platform_applied + 10)
  return clamp(0.5 + (platform_rate / global_rate) * 0.5, 0.5, 1.5)
  # rationale: platform swings rank up to ±50% but can't dominate fit score.
```

**Open tuning items** — fix once we have ≥30 outcomes in the new system:

- 45-day decay constant.
- 0.3 age floor.
- Bayesian prior strength `(+2, +10)`.
- Whether to add a referral multiplier when we already know the company has a referral path open.

### UI

**Decision:** local-only webpage, served by the same process that holds the SQLite handle. Single-user, never exposed publicly.

Rationale:
- Ranked-list-with-drill-down is the right shape for browse + triage; CLI is too lossy, Slack/email-style notifications are wrong for this volume.
- Local-only because a public list of currently-targeted companies is information leakage about an in-progress job hunt.

Stack: Flask + a single HTML page with a sortable table, status filters, and click-through to the listing URL. The `apply` button on a row is what fires the handoff to the resume CI/CD.

### Hosting

| component | host | rationale |
|---|---|---|
| SQLite DB + Flask UI | homelab | always-on, persistent, single-user, no cost |
| Local Gemma inference (pre-filter) | homelab GPU (RTX 2080) | zero marginal cost |
| Frontier API calls (analysis) | from the homelab worker | no infra, just an API key |
| Targeted-company poller | homelab cron | low CPU, scheduled, can tolerate restarts |
| Autonomous crawler | DigitalOcean droplet | residential homelab IPs may get flagged by job boards; rotate from a clean cloud IP |
| Browser plugin | local (browser) | by definition |

AWS credits stay in reserve — burst analysis if backlog grows, or fail-over hosting if the homelab is down. No day-to-day AWS dependency.

## Application handoff

When the user clicks "apply" in the UI:

1. UI POSTs to a backend endpoint with the `job.id`.
2. Backend creates branch `companyName-jobID-YYYYMMDD` (matches existing convention) in the resume repo, scaffolds `job.txt` from the stored description, and `company.txt` if analysis captured careers-page text.
3. Push the branch. `process-resume.yaml` triggers the existing `score-resume` flow.
4. Update the DB row: `status='applied'`, `applied_at=now()`, `branch=…`.
5. From there, the user iterates manually using the existing CI/CD just like today — this plan does not change any of that.

## Outcome tracking

Currently the CSV is updated by hand. In the new system:

- "Mark callback / ghost / offer" buttons on each row in the UI write to `outcomes`.
- A nightly job auto-flips `ghosted=true` for applied rows older than N days with no callback (configurable; start at 30).
- `platform_factor` recomputes from `outcomes` on a schedule, so as platform success rates shift the ranking adapts.
- One-time import: load the existing CSV into `jobs` + `outcomes` so the platform stats start with real data, not zero.

## Research items (do these before kicking off subprojects)

1. **Model bake-off** — Anthropic vs Gemini vs local-prefilter+frontier. Score 5–10 historical CSV rows with each, compare ranking against actual callback/offer outcomes. Blocks the analysis subproject.
2. **Gemma sizing** — confirm 4B int4 fits and runs at acceptable latency on the 1660 Super (so the 2080 stays free for anything that needs more VRAM). Blocks the pre-filter design.
3. **Volume estimate** — how many listings/week will the three producers actually emit? Drives whether the pre-filter is needed at all, and whether SQLite is sufficient long-term.

## Subproject planning sessions

Each gets its own document and a separate planning session. Build the shared pipeline alongside the first subproject so it has a real consumer from day one.

- `firefox-plugin/` — **shipped 2026-05-03** as standalone analyzer (no backend yet); see `firefox-plugin/README.md`. Inverted from the originally-planned ordering because the plugin works as a useful manual tool even without the storage backend, so it ships value immediately.
- `AUTOMATION_PLAN_targeted_poller.md` — next; simplest producer, validates the shared pipeline end-to-end. Will be the first consumer of the SQLite store once it's stood up.
- `AUTOMATION_PLAN_autonomous_crawler.md` — last; depends on rest being stable, has the most failure modes (rate limits, bot detection, schema drift). This is where the local Gemma pre-filter lands.
