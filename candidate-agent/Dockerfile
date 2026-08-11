# syntax=docker/dockerfile:1
#
# candidate-agent: code-gated agent employers can query about a candidate.
# Build context is this directory (candidate-agent). All personal content
# (corpus, access codes, denylist) is mounted at runtime — never baked in.
#
#   docker build -t candidate-agent .
#   docker run --rm -p 8000:8000 \
#     -e ANTHROPIC_API_KEY=sk-ant-... \
#     -e CORPUS_PATH=/content/corpus -e ACCESS_CODES_PATH=/content/codes.txt \
#     -v "$PWD/private:/content:ro" candidate-agent
#
# python:3.13-slim is REQUIRED (not 3.12): its bundled SQLite includes FTS5,
# which retrieval rung 2 depends on. Asserted at build and at startup.

# ---- build stage ----------------------------------------------------------
FROM python:3.13-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
RUN python -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(c)')"

# ---- runtime stage --------------------------------------------------------
FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/tmp \
    AGENT_DB_PATH=/data/agent.db
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY *.py ./
COPY templates ./templates
RUN useradd --uid 1000 --no-create-home agent && \
    mkdir -p /data && chown 1000 /data
USER 1000
# Phase-2 DB lives on a mounted volume (rootfs is read-only in k8s).
VOLUME /data
EXPOSE 8000
# FTS5 assert + single worker (SQLite one-writer rule, phase 2).
CMD ["sh", "-c", "python -c \"import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(c)')\" && exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
