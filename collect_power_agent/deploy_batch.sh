#!/bin/bash
# deploy_batch.sh — Rebuild, redeploy, and seed the Cloud Run batch runner.
# Run from project root whenever you change cloud_batch/ or app/ scripts.
#
# Steps:
#   1. Build image via Cloud Build
#   2. Deploy to Cloud Run
#   3. Set BATCH_RUNNER_URL env var on the service (needed by scheduler_sync.py)
#   4. Seed job definitions into Firestore  (python app/seed_batch_jobs.py)
#
# After deploying, use the "Sync schedules" button in the frontend
# (Batch Services → Cloud Batch) to wire up Cloud Scheduler cron jobs.
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"

echo "=== Batch Runner Deploy ==="
echo "  Project:  $PROJECT"
echo "  Location: $LOCATION"
echo ""

echo "[1/4] Building image via Cloud Build..."
# .gcloudignore limits the upload to app/, config/, cloud_batch/ only.
# .venv/, public/, functions-crm/, exports/ etc. are excluded — keeps upload small.
gcloud builds submit \
  --project "$PROJECT" \
  .

echo ""
echo "[2/4] Deploying to Cloud Run..."
IMAGE="${LOCATION}-docker.pkg.dev/${PROJECT}/batch-runner/batch-runner:latest"
gcloud run deploy batch-runner \
  --image "$IMAGE" \
  --platform managed \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --quiet

# Resolve the runner URL right after deployment
RUNNER_URL=$(gcloud run services describe batch-runner \
  --platform managed --region "$LOCATION" --project "$PROJECT" \
  --format "value(status.url)")

echo ""
echo "[3/4] Setting BATCH_RUNNER_URL on the Cloud Run service..."
# scheduler_sync.py reads this at import time to build Cloud Scheduler HTTP targets.
# The service needs to know its own URL so it can point cron jobs back to itself.
gcloud run services update batch-runner \
  --update-env-vars "BATCH_RUNNER_URL=${RUNNER_URL}" \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --quiet
echo "  BATCH_RUNNER_URL=${RUNNER_URL}"

echo ""
echo "[4/4] Seeding job definitions into Firestore..."
# Activates .venv if present so the script can import firebase_admin etc.
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python app/seed_batch_jobs.py

echo ""
echo "=== Done ==="
echo "  Runner:    $RUNNER_URL"
echo "  Dashboard: https://blueboot-market.web.app/cloud-batch.html"
