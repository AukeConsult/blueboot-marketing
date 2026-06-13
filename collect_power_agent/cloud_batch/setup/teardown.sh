#!/bin/bash
# teardown.sh — Remove Cloud Run service and Cloud Scheduler jobs (keeps Firestore data)
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"

echo "WARNING: This will delete the batch-runner Cloud Run service and all"
echo "         Cloud Scheduler jobs with the 'batch-' prefix."
echo "         Firestore data in gcloud-batch-jobs/ is NOT deleted."
echo ""
read -rp "Type 'yes' to continue: " CONFIRM
[ "$CONFIRM" != "yes" ] && echo "Aborted." && exit 0

echo ""
echo "[1/2] Deleting Cloud Scheduler jobs (batch-*)..."
gcloud scheduler jobs list --project "$PROJECT" --location "$LOCATION" \
  --format "value(name)" \
  | grep "/batch-" \
  | while read -r job; do
      echo "  deleting $job"
      gcloud scheduler jobs delete "$job" --project "$PROJECT" \
        --location "$LOCATION" --quiet || true
    done

echo "[2/2] Deleting Cloud Run service: batch-runner"
gcloud run services delete batch-runner \
  --platform managed --region "$LOCATION" --project "$PROJECT" \
  --quiet || echo "  (not found — skipping)"

echo ""
echo "Teardown complete. Firestore data preserved."
