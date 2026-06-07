# Job Board

Three components that work in unison to discover, score, and triage job postings:

| Component | Role | Path |
|---|---|---|
| **job-store** | Flask + SQLite backend. Inbox UI, scoring endpoint, dedupe, company-targets CRUD, ranking math. Single source of truth for the resume, the prompt, the schema, and the Anthropic API key. | `job-store/` |
| **poller** | CLI that walks the `company_targets` table, fetches current openings from each ATS, dedupes, applies title and location filters, and POSTs survivors to `job-store/jobs/score` for scoring. Lives inside `job-store/` because it shares Python imports with the backend. | `job-store/poller.py` |
| **firefox-plugin** | Browser extension. Extracts the JD from whatever page you're on, POSTs it to job-store, renders the score. Also surfaces a "watch this company" button when the page is on a supported ATS. | `firefox-plugin/` |

## How they talk

```
                   browser tab on a job posting
                              |
                              v
              +---------------------------+
              |     firefox-plugin        |
              |  (extract JD + POST)      |
              +-------------+-------------+
                            |
                            | POST /jobs/score
                            |  (no fit_score in the body)
                            v
   +-----------+    HTTP    +---------------------------+    HTTP    +-----------+
   |  poller   |----------->|         job-store         |<-----------|  browser  |
   |  (CLI)    |  POST /    |  Flask + SQLite           |    GET /   |  (inbox)  |
   +-----------+  jobs/score|  Inbox UI at /            |            +-----------+
                            |  Companies CRUD at /companies         
                            +-------------+-------------+           
                                          |
                                          | Anthropic API (server-side
                                          |  scoring; resume + prompt
                                          |  live in job-store, not in
                                          |  the plugin or the poller)
                                          v
                                  api.anthropic.com
```

Both the plugin and the poller POST job descriptions without a `fit_score`. job-store calls Anthropic, persists the analysis, and returns the score. Neither the plugin nor the poller ever sees the API key.

## Configuration

job-store reads these env vars:

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | server-side scoring |
| `RESUME_PATH` | yes | absolute path to the resume YAML (e.g. `~/wip/resume/resume_details.yaml`) |
| `ANTHROPIC_MODEL` | no | defaults to `claude-haiku-4-5` |
| `GROWTH_KEYWORDS` | no | comma-separated phrases the scorer rewards (career-growth signal) |
| `JOBS_DB_PATH` | no | SQLite DB location; defaults next to the code (the container sets `/data/jobs.db`) |
| `EXTENSION_DIST_DIR` | no | dir holding the signed Firefox `.xpi` served at `/extension`. Released images bake it in at `/app/extension`; local `flask run` defaults to `firefox-plugin/dist/` |

The poller has no env-var requirements of its own; it talks to job-store over HTTP. It honors `JOB_STORE_URL` for the backend URL.

The plugin's only configuration is the backend URL (set in the options page). It defaults to `http://127.0.0.1:5000`.

## Running locally

```bash
cd job-board/job-store
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export RESUME_PATH=~/wip/resume/resume_details.yaml
flask --app app run --port 5000
```

In another terminal:

```bash
cd job-board/job-store
.venv/bin/python poller.py --dry-run        # preview
.venv/bin/python poller.py                  # live run
```

For the plugin: open `about:debugging` in Firefox, load `job-board/firefox-plugin/manifest.json` as a temporary add-on, then point its backend URL at `http://127.0.0.1:5000` on the options page.

## Component-specific docs

- **job-store** internals and routes: [`job-store/README.md`](job-store/README.md)
- **firefox-plugin** install and dev notes: [`firefox-plugin/README.md`](firefox-plugin/README.md)
- **Architecture history** and the rationale for the three-component split: [`docs/automation-plan.md`](docs/automation-plan.md)
- **Server-side scoring** design (why scoring lives in job-store, not in the plugin or the poller): [`docs/server-side-scoring.md`](docs/server-side-scoring.md)
- **iCIMS adapter** notes and deferred-decision context: [`docs/icims-adapter-notes.md`](docs/icims-adapter-notes.md)

## Companion: GitHub Actions

This repo also hosts a collection of composite GitHub Actions used by a separate resume-as-code repo for tailoring and scoring at commit time. See the top-level `README.md` for those: `gemini-qualified`, `gemini-tailor`, `gemini-pick-best-fit`, `gemini-cover-outline`, `gemini-last-looks`, `render-resume`.
