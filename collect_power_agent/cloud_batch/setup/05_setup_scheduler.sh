#!/bin/bash
# 05_setup_scheduler.sh — Create Cloud Scheduler jobs from job_definitions/*.json
# Delegates to scheduler_setup.py which reads the JSON files and calls gcloud.
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"
SA_EMAIL="${BATCH_SA:-batch-runner@${PROJECT}.iam.gserviceaccount.com}"

if [ -z "${BATCH_RUNNER_URL:-}" ]; then
  echo "ERROR: BATCH_RUNNER_URL is not set."
  echo "  Run 04_deploy_cloudrun.sh first and export the printed URL."
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
