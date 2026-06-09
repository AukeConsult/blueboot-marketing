#!/bin/bash
# 05_setup_scheduler.sh — Create Cloud Scheduler jobs from job_definitions/*.json
# Delegates to scheduler_setup.py which reads the JSON files and calls gcloud.
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"
SA_EMAIL="${BATCH_SA:-batch-runner@${PROJECT}.iam.gserviceaccount.com}"

# Fall back to .env, then try fetching from gcloud
if [ -z "${BATCH_RUNNER_URL:-}" ]; then
  ENV_FILE="$(cd "$(dirname "$0")/../.." && pwd)/.env"
  BATCH_RUNNER_URL=$(grep -E "^BATCH_RUNNER_URL=" "$ENV_FILE" 2>/dev/null | head -1 | sed "s/^BATCH_RUNNER_URL=//;s/^['\"]//;s/['\"]$//")
fi
if [ -z "${BATCH_RUNNER_URL:-}" ]; then
  echo "  BATCH_RUNNER_URL not in .env — fetching from gcloud..."
  BATCH_RUNNER_URL=$(gcloud run services describe batch-runner \
    --platform managed --region "$LOCATION" --project "$PROJECT" \
    --format "value(status.url)" 2>/dev/null || true)
fi
if [ -z "${BATCH_RUNNER_URL:-}" ]; then
  echo "ERROR: Could not determine BATCH_RUNNER_URL."
  echo "  Add it to .env or run: export BATCH_RUNNER_URL=https://..."
  exit 1
fi

echo "Setting up Cloud Scheduler jobs..."
echo "  Runner URL: $BATCH_RUNNER_URL"
echo "  Project:    $PROJECT / $LOCATION"
echo ""

cd "$(dirname "$0")/../.."
python -m cloud_batch.scheduler_setup \
  --project    "$PROJECT" \
  --location   "$LOCATION" \
  --runner-url "$BATCH_RUNNER_URL" \
  --service-account "$SA_EMAIL" \
  ${BATCH_SECRET:+--secret "$BATCH_SECRET"}

echo ""
echo "Scheduler jobs created. View in:"
echo "  https://console.cloud.google.com/cloudscheduler?project=$PROJECT"
