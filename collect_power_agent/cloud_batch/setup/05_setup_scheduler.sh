#!/bin/bash
# 05_setup_scheduler.sh — Trigger a full Cloud Scheduler sync via the batch runner API.
#
# NOTE: Scheduler jobs are now managed via tasks stored in Firestore, not from
# job_definitions/*.json. The sync is done by the Cloud Run service itself via
# POST /sync-schedulers (scheduler_sync.py). This script calls that endpoint.
#
# Preferred method: click "Sync schedules" in the frontend (Batch Services -> Cloud Batch).
# This script is provided for CI/CD or first-time setup after 04_deploy_cloudrun.sh.
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

echo "Syncing Cloud Scheduler jobs via batch runner..."
echo "  Runner URL: $BATCH_RUNNER_URL"
echo "  Project:    $PROJECT / $LOCATION"
echo ""

TOKEN=$(gcloud auth print-identity-token 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
  echo "ERROR: Could not get identity token. Run: gcloud auth login"
  exit 1
fi

RESPONSE=$(curl -s -X POST "${BATCH_RUNNER_URL}/sync-schedulers" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "${BATCH_SECRET:+{\"secret\":\"${BATCH_SECRET}\"}}" )

echo "$RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE"

echo ""
echo "View Cloud Scheduler jobs:"
echo "  https://console.cloud.google.com/cloudscheduler?project=$PROJECT"
