#!/bin/bash
# deploy_batch.sh — Rebuild and redeploy the Cloud Run batch runner
# Run from project root whenever you change cloud_batch/ or app/ scripts.
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"

echo "=== Batch Runner Deploy ==="
echo "  Project:  $PROJECT"
echo "  Location: $LOCATION"
echo ""

echo "[1/2] Building image via Cloud Build..."
# .gcloudignore limits the upload to app/, config/, cloud_batch/ only.
# .venv/, public/, functions-crm/, exports/ etc. are excluded — keeps upload small.
gcloud builds submit \
  --project "$PROJECT" \
  .

echo ""
echo "[2/2] Deploying to Cloud Run..."
IMAGE="${LOCATION}-docker.pkg.dev/${PROJECT}/batch-runner/batch-runner:latest"
gcloud run deploy batch-runner \
  --image "$IMAGE" \
  --platform managed \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --quiet

echo ""
echo "=== Done ==="
RUNNER_URL=$(gcloud run services describe batch-runner \
  --platform managed --region "$LOCATION" --project "$PROJECT" \
  --format "value(status.url)")
echo "  $RUNNER_URL"
