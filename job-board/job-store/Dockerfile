# syntax=docker/dockerfile:1
#
# job-store: Flask inbox + server-side scoring, served via gunicorn.
# Build context is this directory (job-board/job-store).
#
#   docker build -t job-store .
#   docker run --rm -p 5000:5000 \
#     -e ANTHROPIC_API_KEY=sk-ant-... \
#     -e RESUME_PATH=/etc/job-store/resume.yaml -v "$PWD/resume.yaml:/etc/job-store/resume.yaml:ro" \
#     -v job-store-data:/data \
#     job-store
#
# The DB lives at JOBS_DB_PATH (/data/jobs.db by default here) so it can sit on
# a mounted volume; the resume and API key are injected at runtime, never baked
# into the image.

# ---- build stage: install deps into an isolated venv --------------------------
FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"
RUN python -m venv /opt/venv
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# ---- runtime stage ------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    JOBS_DB_PATH=/data/jobs.db \
    EXTENSION_DIST_DIR=/app/extension

COPY --from=build /opt/venv /opt/venv

# Non-root runtime user. /data is the writable mount for the SQLite DB and its
# WAL/SHM siblings — everything else can run read-only (see the Helm chart's
# readOnlyRootFilesystem securityContext).
RUN useradd --system --uid 1000 --create-home appuser \
    && mkdir -p /data && chown appuser:appuser /data

WORKDIR /app
# CI drops the signed Firefox .xpi into ./extension/ before building so it's
# baked into the image and served at /extension (EXTENSION_DIST_DIR above) —
# works under both `docker run` and Helm. Local builds without it just omit the
# install link (the route 404s cleanly). The dir is created so COPY/chown work
# even when no .xpi is present.
RUN mkdir -p /app/extension
COPY . /app
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# Single worker: SQLite tolerates exactly one writer; WAL mode handles read
# concurrency within the process. Do not scale workers without moving off SQLite.
ENTRYPOINT ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "app:app"]
