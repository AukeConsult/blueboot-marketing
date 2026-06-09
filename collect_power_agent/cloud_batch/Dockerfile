# cloud_batch/Dockerfile
# Build context: project root  (docker build -f cloud_batch/Dockerfile .)
FROM python:3.11-slim

WORKDIR /workspace

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── Install Python dependencies ───────────────────────────────────────────────
# Install deps before copying source so Docker can cache this layer.
# Re-runs only when a requirements file changes, not on every code change.

# 1. Main project deps — needed by app/*.py pipeline scripts at runtime
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 2. cloud_batch service deps — Flask, gunicorn, firebase-admin
COPY cloud_batch/requirements.txt ./cloud_batch_requirements.txt
RUN pip install --no-cache-dir -r cloud_batch_requirements.txt

# ── Copy pipeline scripts ─────────────────────────────────────────────────────
# These are the command-line scripts that job_runner.py calls as subprocesses.
# Each pipeline step runs as:  python -m app.<script> --countries NO ...
#
# app/                  — pipeline scripts (site_agent.py, lead_agent.py, etc.)
# app/functions/        — shared helpers: config.py, utils.py, models.py, etc.
# app/collect-functions/— lead_agent deps: catalog_scrapers.py, search_runner.py
# config/               — query/country JSON files (site_agent_queries.json, etc.)
#
# NOT needed in this image:
#   functions-crm/      — CRM API Cloud Function (deployed separately)
#   crm/                — CRM sheet-sync scripts (run locally only)
#   public/             — Firebase Hosting frontend (deployed separately)

COPY app/                   ./app/
COPY config/                ./config/

# ── Copy cloud_batch framework ────────────────────────────────────────────────
COPY cloud_batch/           ./cloud_batch/

# ── Runtime config ────────────────────────────────────────────────────────────
# Secrets (FIREBASE_KEY_JSON, OPENAI_API_KEY, etc.) are NOT in the image.
# They are injected by Cloud Run from Secret Manager at startup via --set-secrets.
# See setup/06_secrets.sh and setup/04_deploy_cloudrun.sh.

ENV PYTHONPATH=/workspace
ENV PORT=8080

# 1 worker so all background job threads share the same process state.
# --timeout 0 disables gunicorn's worker timeout (jobs run for hours).
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", \
     "--timeout", "0", "cloud_batch.entrypoint:app"]
