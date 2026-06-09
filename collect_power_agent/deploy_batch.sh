#!/bin/bash
# deploy_batch.sh — Rebuild, redeploy, and seed the Cloud Run batch runner.
# Run from project root whenever you change cloud_batch/ or app/ scripts.
#
# Steps:
#   1. Build image via Cloud Build
#   2. Deploy to Cloud Run
#   3. Seed job definitions into Firestore  (python app/seed_batch_jobs.py)
#
# Scheduler jobs are NOT auto-synced here — use the "Sync schedules" button
# in the frontend (google-job.html) after editing task cron expressions.
# Or run manually:  python -m cloud_batch.scheduler_setup --runner-url <URL>
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"

echo "=== Batch Runner Deploy ==="
echo "  Project:  $PROJECT"
echo "  Location: $LOCATION"
echo ""

echo "[1/3] Building image via Cloud Build..."
# .gcloudignore limits the upload to app/, config/, cloud_batch/ only.
# .venv/, public/, functions-crm/, exports/ etc. are excluded — keeps upload small.
gcloud builds submit \
  --project "$PROJECT" \
  .

echo ""
echo "[2/3] Deploying to Cloud Run..."
IMAGE="${LOCATION}-docker.pkg.dev/${PROJECT}/batch-runner/batch-runner:latest"
gcloud run deploy batch-runner \
  --image "$IMAGE" \
  --platform managed \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --quiet

echo ""
echo "[3/3] Seeding job definitions into Firestore..."
# Activates .venv if present so the script can import firebase_admin etc.
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python app/seed_batch_jobs.py

echo ""
echo "=== Done ==="
RUNNER_URL=$(gcloud run services describe batch-runner \
  --platform managed --region "$LOCATION" --project "$PROJECT" \
  --format "value(status.url)")
echo "  Runner: $RUNNER_URL"
echo "  Dashboard: https://blueboot-market.web.app/google-job.html"
