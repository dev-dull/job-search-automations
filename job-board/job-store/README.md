# job-store

Flask + SQLite backend for the Job Board stack. Serves the inbox UI, runs server-side scoring against Anthropic, holds the `jobs` / `outcomes` / `company_targets` / `settings` tables, and provides the HTTP endpoints the poller and firefox-plugin both call.

The poller (`poller.py`) is a standalone HTTP client of the backend (it shares only the `adapters` package). The high-priority branch tool (`branch_high_priority.py`) lives in this same Python package because it imports the backend's `db`, `ranking`, and `urls` modules directly.

## What it does

- **Stores** every discovered job in `jobs.db` (SQLite). Dedupe by `dedupe_key` so the same posting reached via different URL shapes (Greenhouse embed wrapper vs `boards.greenhouse.io` direct) collapses to one row.
- **Scores** server-side. Both the plugin and the poller POST a description with no `fit_score`; this service calls Anthropic with the resume YAML and the prompt, persists the analysis, returns the score.
- **Ranks** on read with `fit_score * age_decay(posted_at) * platform_factor(ats)`. Recomputed every request so stale rows naturally drift down without needing a manual rerank pass.
- **Triages** via the UI at `/`: ranked list, status filters, apply/dismiss/outcome buttons, cleanup-stale button, re-rank-all button.
- **Manages targets** via `/companies`: company_targets CRUD with per-company deny lists, last_polled timestamps, and an auto-resolve probe for wrapper pages that embed a supported ATS.

## Files

| File | Role |
|---|---|
| `app.py` | Flask routes, `detect_ats()`, `probe_embedded_ats()`, the inbox/companies views, cleanup |
| `db.py` | Schema (`jobs`, `outcomes`, `company_targets`, `settings`), upsert/list helpers |
| `ranking.py` | Pure functions for `age_decay`, `platform_factor`, `compute_rank_score` |
| `urls.py` | `canonicalize_url` (storage-canonical) and `compute_dedupe_key` (gh:N, ashby:N, etc.) |
| `anthropic_client.py` | Resume loading, scoring prompt and schema, the Anthropic API call |
| `poller.py` | Targeted-company poller CLI (see below) |
| `branch_high_priority.py` | Bulk-creates branches in the resume repo for jobs above a fit/rank threshold |
| `csv_import.py` | One-shot CLI to seed `outcomes` from a historical job-hunt CSV |
| `adapters/{greenhouse,ashby,lever,workday}.py` | Per-ATS `list_jobs(identifier) -> [job, …]` |
| `templates/index.html` `templates/companies.html` `static/style.css` | Inbox + companies UI |
| `requirements.txt` | `flask` + `anthropic` |

## Setup

```bash
cd job-board/job-store
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export RESUME_PATH=~/wip/resume/resume_details.yaml
flask --app app run --port 5000
```

Open `http://127.0.0.1:5000/`. The DB is created on first run; `jobs.db` is gitignored.

Optional seeding step (historical platform-success stats so the smoothing prior isn't doing all the work):

```bash
.venv/bin/python csv_import.py path/to/your/job-hunt-outcomes.csv
```

Only rows flagged `Applied=TRUE` are imported.

## Running the poller

```bash
.venv/bin/python poller.py --show-locations       # print the location allowlist/denylist
.venv/bin/python poller.py --target 5 --dry-run   # preview a single target
.venv/bin/python poller.py                        # poll all targets
.venv/bin/python poller.py --set-locations "United States,USA,US-,Remote,Americas"
```

The poller is a **pure HTTP client** of job-store — it holds no DB access. It reads targets (`/companies.json`), existing URLs (`/jobs/urls`), and location settings (`/settings/locations`) over HTTP, walks each adapter's response newest-first, filters by deny list and location, lazy-fetches descriptions, POSTs survivors to `/jobs/score`, and stamps `last_polled` via `/companies/<id>/polled`. It stops at the first URL it already has, then moves to the next target. This lets it run anywhere with network reach to the backend (e.g. the Helm chart's CronJob).

The backend URL comes from `--backend` or the `JOB_STORE_URL` env var (default `http://127.0.0.1:5000`). `--max-new N` caps per-target spend on first polls of big tenants.

## HTTP API

### `POST /jobs/score`

Called by the plugin (extract-and-post) and the poller (after filtering). Body:

```json
{
  "url": "https://boards.greenhouse.io/...",
  "title": "Senior DevOps Engineer",
  "description": "About the role...",
  "ats_platform": "greenhouse",
  "discovered_by": "plugin"
}
```

Backend canonicalizes the URL, dedupes via `dedupe_key`, calls Anthropic if the row is new (or `force: true` is set), persists `analysis_json`, returns `{id, fit_score, company, analysis, rank_score, status}`. Legacy callers that already have a `fit_score` and `analysis` skip the Anthropic step.

### `GET /jobs/score?url=<encoded>`

Read-only lookup. Returns the existing score for a URL without spending another Anthropic call. Used by the plugin's toolbar-badge updater.

### `GET /jobs?status={open|applied|closed|all}`

JSON list of jobs in the named view, with live-recomputed ranks.

### `POST /jobs/<id>/{apply,dismiss,outcome}`

Form-encoded. `apply` records the resume-repo branch name; `dismiss` closes the row; `outcome` records `referral`, `callback`, `ghosted`, `rejected`, `offer`, `notes`. Marking `rejected` also auto-closes the row.

### `POST /admin/rerank` `POST /admin/cleanup`

`rerank` recomputes and persists `rank_score` for every row (mostly cosmetic since reads recompute live). `cleanup` deletes age-aged and dead-URL rows in `discovered`/`ranked` status that have no outcome data; threshold via the `cleanup_days_threshold` setting.

### `/companies` `/companies/<id>` `/companies.json`

Companies UI + JSON read. POST `/companies` accepts a careers URL; if it isn't on a supported ATS, the backend fetches it and looks for an embedded Greenhouse/Ashby/Lever/Workday board.

### `GET /resume`

Returns the on-disk resume (content, mtime, sha256). Used by the plugin and as a sanity-check that `RESUME_PATH` resolves.

## Ranking math

```
rank_score = fit_score * age_decay(posted_at OR discovered_at) * platform_factor(ats)

age_decay(d):
  days = today - d
  return clamp(1.0 - days / 45, 0.3, 1.0)

platform_factor(p):
  smoothed = (callbacks + 2) / (applied + 10)
  return clamp(0.5 + (smoothed / global_callback_rate) * 0.5, 0.5, 1.5)
```

Constants live at the top of `ranking.py`. Plan to revisit once you have at least 30 outcomes in the system; until then the smoothing prior dominates per-platform stats.

## Production notes

Single-writer SQLite, `PRAGMA journal_mode = WAL` for read concurrency. Suitable for one-user homelab use. For cluster deployment, see the (planned) Dockerfile and Helm chart issues on the repo.

`flask run` is fine for dev. For something sustained, swap in `gunicorn -w 1 -b 127.0.0.1:5000 app:app` (single worker since SQLite tolerates one writer).

## Running with Docker

The `Dockerfile` produces a gunicorn-served image. The resume and API key are injected at runtime — never baked into the image — and the DB lives at `JOBS_DB_PATH` so it can sit on a mounted volume.

```bash
docker build -t job-store .
docker run --rm -p 5000:5000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e RESUME_PATH=/etc/job-store/resume.yaml \
  -v "$PWD/resume.yaml:/etc/job-store/resume.yaml:ro" \
  -v job-store-data:/data \
  job-store
```

| Env var | In the image | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | injected at run | server-side scoring |
| `RESUME_PATH` | injected at run | mount the resume read-only and point here |
| `JOBS_DB_PATH` | defaults to `/data/jobs.db` | DB location; keep it on the mounted volume so state survives restarts (WAL/SHM siblings land in the same dir). Unset, it defaults next to the code for local `flask run`. |

CI builds and pushes `ghcr.io/dev-dull/job-store:{git-sha}` (and `:latest` on `main`) via `.github/workflows/build-job-store.yaml`.
