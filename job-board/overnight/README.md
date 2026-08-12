# Overnight discovery agent

Crawls public job sources for senior infrastructure roles at companies the watch
list has never heard of, prescreens them for free on a local LLM, and submits
only the survivors for paid scoring.

**This is not a poller.** `job-store/poller.py` already covers the watch list on
a CronJob. This widens the funnel to companies outside it. Roles from companies
already being watched are skipped on sight.

```
sources (HN, WWR) -> dedupe vs job-store seen-set -> local prescreen (free)
   -> POST survivors to /jobs/score (paid, capped) -> CSV + morning report
```

## What you need

1. **A running job-store** (see `../job-store/`) reachable over HTTP — the
   agent reads its seen-set and resume, and submits survivors to it.
2. **A local LLM endpoint speaking the OpenAI chat API** with support for
   grammar-constrained JSON (`json_schema` response format). llama.cpp's
   `llama-server` behind [llama-swap](https://github.com/mostlygeek/llama-swap)
   is the tested path; anything API-compatible works. You want two model
   aliases:
   - a **triage model**: small, fast, ideally GPU-resident, served with
     several parallel slots (this stage sees every posting);
   - a **gate/fit model**: your strongest local instruction-follower (this
     stage sees only triage survivors, so slow is fine overnight).
   If your models are "thinking" models, the client already disables that
   per-request — see the gotchas below.
3. **A gates file** — your private hard requirements (below).
4. Python 3 stdlib. There is nothing to `pip install`.

## Run it

```bash
export JOB_STORE=https://<your-job-store>          # or --backend
export LLAMA_SWAP=http://<your-llm-host>:8080      # or --llm; default localhost:8080
export DISCOVERY_USER_AGENT="overnight-discovery/0.1 (job search agent; you@example.com)"

python3 discover.py --gates ~/private/gates.txt --out ~/reports            # dry run
python3 discover.py --gates ~/private/gates.txt --out ~/reports --submit   # spends money
```

Set `DISCOVERY_USER_AGENT` to something that identifies *you* with a contact
path — an honest UA is part of being a polite bot; the default names only the
toolkit.

Dry run is the default and screens everything without POSTing, so you can read a
morning report and tune the funnel before letting it spend.

| flag | default | |
|---|---|---|
| `--gates` | `$GATES_PATH` | **required** — your hard requirements, see below |
| `--out` | — | report directory; keep it on durable storage |
| `--submit` | off | actually pay for authoritative scoring |
| `--max-submit` | 25 | hard cap on paid calls per night |
| `--sources` | `hn,wwr` | which collectors to run |
| `--max-per-source` | 400 | cap postings pulled per source |
| `--triage-workers` | 8 | match your triage model's parallel slots (`-np`) |
| `--gate-workers` | 1 | match your gate model's parallel slots — more just queues |
| `--triage-model` | `$TRIAGE_MODEL` / `scorer` | model alias for the triage stage |
| `--gate-model` | `$GATE_MODEL` / `coder` | model alias for the gates+fit stage |
| `--title-drops-extra` | `$TITLE_DROPS_EXTRA` | comma-separated title words to drop on sight. The built-in list drops only clearly-entry-level titles; **your** band/family exclusions (staff, manager, frontend, …) go here or in the gates file — they're personal config, not toolkit code |
| `--backend` | `$JOB_STORE` | job-store base URL (**required**) |
| `--llm` | `$LLAMA_SWAP` | OpenAI-compatible LLM base URL |

## The gates file

job-store keeps preferences server-side (`PREFERENCES_PATH`) and exposes no
endpoint to read them, so the local prescreen needs its own copy. Plain text,
one requirement per line — level band, location/timezone, excluded role
families, comp floor. It is personal data: keep it on a LAN machine and **never
commit it**. See `gates.example.txt`.

## Sources

| source | mechanism | why |
|---|---|---|
| `hn` | [HN Algolia API](https://hn.algolia.com/api) over the monthly *Ask HN: Who is hiring?* thread | ~440 postings/month, heavily remote senior infra, companies that never touch job boards. Official API, no scraping |
| `wwr` | WeWorkRemotely category RSS | Official feeds. `robots.txt` allows `/` except admin/account paths (checked 2026-08-02) |

**RemoteOK is deliberately not implemented.** Its `robots.txt` disallows
`/*?action=get_jobs` — the JSON jobs API — for all user agents, and individually
blocks every major AI crawler (ClaudeBot, GPTBot, CCBot, Google-Extended, …).
The site has said no.

Only **top-level** HN comments are treated as postings; replies are discussion.
Without that filter roughly half of what you collect is people arguing about
compensation.

**HN postings are lower-fidelity than board postings, by nature.** The
"description" is the comment text (sometimes a blurb, not a JD) and the URL is
a best-effort pick from the comment's links — occasionally a company homepage
rather than the posting. That's an accepted trade for HN's unique signal, but
it means an HN submission can put a homepage-URL row with a blurb-quality
description on the board; the morning report is where you catch those.
Comments under the backend's minimum description length upsert unscored.

## Things that will bite you

**`chat_template_kwargs={"enable_thinking": false}` is mandatory.** Without it
the Qwen models emit a `reasoning_content` block, spend the entire token budget
on it, and return `content: ""` with `finish_reason: "length"`. It fails as an
empty string rather than an error, so it reads like a bad prompt.

**Schema descriptions are not instructions.** With the rubric only in the JSON
schema's `description` fields, `fit_sketch` came back `0` on every posting and
`pace_signals` was always empty. Moving explicit range anchors into the system
prompt fixed it — same posting, three pinned seeds: `0,0,0` → `92,92,92`. If a
numeric field looks dead, put the rubric in the prompt.

**Field order in the schema is load-bearing.** Grammar-constrained decoding emits
properties in schema order, so `evidence`/`gaps` must come before `score` to
force reasoning ahead of the number. Keep score fields last.

**The gate model is slow on full JDs** — expect single-digit postings per
minute cold on a CPU-offloaded MoE. Fine overnight, but it means the free
rules stage has to do the heavy filtering. Order is deliberate: regex →
small GPU-resident model → big model.

**The prescreen fails open.** If the model errors, the posting is kept and the
error recorded. A model problem must never silently shrink the funnel.

## Guardrails

- Nothing is POSTed without `--submit`; `--max-submit` caps the spend.
- **Fail-open never reaches the paid stage**: a posting kept because a model
  errored (`stage=error`) appears in the report for human review but is
  excluded from submission — a gate-model outage cannot convert the submit
  cap into unscreened paid calls.
- `force` is never sent, so re-POSTing a known URL returns the cached analysis
  free of charge.
- The seen-set is fetched before anything else and every known dedupe key skipped.
- Sources use official APIs/feeds, send an honest User-Agent, and are rate-limited
  per host. `fetch.PoliteFetcher` also drops a host for the night after repeated
  403/429 rather than backing off into a block.
- Companies are **never** auto-added to the watch list. The report lists the ones
  that produced keeps; adding them stays a human decision.
- Keep reports on durable storage — if your LLM host's disks are scratch
  (a common shape for homelab inference boxes), sync the report directory
  somewhere that survives.

## Layout

| file | |
|---|---|
| `discover.py` | orchestrator + CLI |
| `gates.example.txt` | template for the private `--gates` file |
| `sources/` | `hn.py`, `wwr.py` — pluggable collectors returning a common shape |
| `prescreen.py` | rules → triage → gates+fit funnel |
| `llm.py` | llama-swap client, grammar-constrained JSON, schemas |
| `fetch.py` | rate limiting + circuit breaker; reuses `job-store/adapters` |
| `report.py` | CSV + morning markdown, including the rejection log |

## Scheduling

Any scheduler works; the agent is a single nightly invocation. A crontab
example (systemd-timer equivalents apply):

```cron
30 2 * * *  JOB_STORE=... LLAMA_SWAP=... GATES_PATH=... /usr/bin/python3 /path/to/discover.py --out /srv/reports --submit >> /srv/reports/run.log 2>&1
```

Start with a few dry-run nights and read the rejection log before adding
`--submit`. If your LLM host swaps models on demand with an idle TTL, mind
that long gaps between stages can pay a model reload.

The rejection log is the point of the CSV: every drop records which stage killed
it and why, so "what did it throw away, and was it right?" is answerable in the
morning. That question is what makes the thresholds tunable instead of folklore.
