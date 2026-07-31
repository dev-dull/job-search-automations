# candidate-agent

An access-code-gated AI agent that potential employers can query about a job
candidate. Three front doors, one server-side answer engine (design and
verified constraints: [PLAN.md](PLAN.md)):

1. **Browser chat** — send a recruiter `https://<host>/`; they enter their
   access code and talk to the agent.
2. **Zero-install fetch** — the recruiter pastes `https://<host>/c/<code>`
   into claude.ai / ChatGPT / Gemini and asks their assistant to interview
   the agent; it makes plain GETs to `/c/<code>/ask?q=…`. Nothing installed.
3. **Remote MCP** — `claude mcp add --transport http --header
   "Authorization: Bearer <code>" https://<host>/mcp` (Claude Desktop and
   ChatGPT developer mode work too). `https://<host>/mcp/<code>` serves
   clients that can't send headers (claude.ai custom connectors) — allowed
   only for codes flagged `url_auth`.

Like everything in this repo, **no personal data lives here**. The agent's
knowledge is an operator-supplied corpus mounted at runtime, and answers are
always generated server-side — corpus documents never leave the server.

## Configuration (env)

| var | required | notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | set a provider-side spend limit too |
| `CORPUS_PATH` | yes | directory of curated `.md`/`.txt` docs with front-matter (see PLAN.md for layout); the service refuses to start without it |
| `ACCESS_CODES_PATH` | yes | the code file (format below) |
| `REDACTION_DENYLIST_PATH` | recommended | strings that must never appear in the corpus; a hit fails startup, and a hit arriving via live content sync is rejected while the last-good corpus keeps serving |
| `ANTHROPIC_MODEL` | no | default `claude-sonnet-5` |
| `CONTACT_EMAIL` | no | shown when a code has expired |
| `RATE_LIMIT_PER_HOUR` | no | per-code request cap (default 60) |
| `DAILY_BUDGET_USD_PER_CODE` / `DAILY_BUDGET_USD_GLOBAL` | no | cost-weighted daily budgets (defaults 5 / 25) |
| `MAX_ANSWER_TOKENS`, `MAX_TURNS` | no | answer and conversation caps |

## Access codes

One per line at `ACCESS_CODES_PATH`, pipe-separated:

```
# code            | label       | options...
maple-K7RT-hazel  | Acme Corp   | expires=2026-08-30
birch-Q2ZX-otter  | Beta Search | url_auth | note=met at conf
```

`url_auth` lets the code travel in URLs (`/c/<code>` fetch surface and the
`/mcp/<code>` claude.ai form) — issue it deliberately and prefer shorter
expiries there. Revoke by deleting the line (or adding `revoked`); revocation
takes effect within your content-sync interval + ~1 minute, including for
already-open browser sessions. All counters (rate, budget, failed-attempt
limits) are in-memory phase-1: they reset on restart.

## Run it

```
docker build -t candidate-agent .
docker run --rm -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e CORPUS_PATH=/content/corpus \
  -e ACCESS_CODES_PATH=/content/codes.txt \
  -e REDACTION_DENYLIST_PATH=/content/denylist.txt \
  -v "$PWD/private:/content:ro" candidate-agent
```

`docker-compose.example.yaml` shows the same with a mount layout;
`helm/` deploys it to Kubernetes with the host/ingress/content-sync details
left as values. Whatever fronts it must terminate TLS, pass streaming
responses unbuffered, and be reachable from the public internet — see
PLAN.md's deployment requirements. Run single-process only
(`uvicorn --workers 1`; the Dockerfile already does).

## Development

```
pip install -r requirements.txt httpx
python -m unittest discover -s tests
```

Tests use synthetic fixtures only; the HTTP-surface tests skip automatically
when fastapi/fastmcp/httpx aren't installed (the pure-stdlib ones always run).
