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
MEMORY="${BATCH_MEMORY:-4Gi}"
CPU="${BATCH_CPU:-2}"
TIMEOUT="${BATCH_TIMEOUT:-3600}"
MIN_INSTANCES="${BATCH_MIN_INSTANCES:-1}"
MAX_INSTANCES="${BATCH_MAX_INSTANCES:-3}"
SA="${BATCH_SA:-batch-runner@${PROJECT}.iam.gserviceaccount.com}"

echo "=== Batch Runner Deploy ==="
echo "  Project:     $PROJECT"
echo "  Location:    $LOCATION"
echo "  Memory:      $MEMORY"
echo "  CPU:         $CPU"
echo "  Timeout:     ${TIMEOUT}s"
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
  --memory "$MEMORY" \
  --cpu "$CPU" \
  --timeout "$TIMEOUT" \
  --no-cpu-throttling \
  --min-instances "$MIN_INSTANCES" \
  --max-instances "$MAX_INSTANCES" \
  --quiet
# Note: --concurrency is intentionally omitted. /run returns 202 immediately and
# jobs run in background threads, so Cloud Run never sees concurrent requests.
# Job concurrency is controlled by the is_running() dedup guard in entrypoint.py.

# Resolve the runner URL right after deployment
RUNNER_URL=$(gcloud run services describe batch-runner \
  --platform managed --region "$LOCATION" --project "$PROJECT" \
  --format "value(status.url)")

echo ""
echo "[3/4] Configuring env vars and IAM on the Cloud Run service..."
# BATCH_RUNNER_URL — scheduler_sync.py uses this to build Cloud Scheduler HTTP targets.
# BATCH_SA         — scheduler_sync.py sets this as the OIDC token SA on each scheduler job,
#                    allowing Cloud Scheduler to call the authenticated Cloud Run endpoint.
gcloud run services update batch-runner \
  --update-env-vars "BATCH_RUNNER_URL=${RUNNER_URL},BATCH_SA=${SA}" \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --quiet
echo "  BATCH_RUNNER_URL=${RUNNER_URL}"
echo "  BATCH_SA=${SA}"

# Grant the SA permission to invoke the Cloud Run service (idempotent).
echo "  Granting run.invoker to ${SA}..."
gcloud run services add-iam-policy-binding batch-runner \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --member "serviceAccount:${SA}" \
  --role "roles/run.invoker" \
  --quiet

# Grant the SA permission to act as itself when creating scheduler jobs with OIDC token.
# Required by scheduler_sync.py: creating a Cloud Scheduler job with an OIDC token
# pointing to this SA requires iam.serviceAccounts.actAs on the SA itself.
echo "  Granting serviceAccountUser to ${SA} (actAs itself)..."
gcloud iam service-accounts add-iam-policy-binding "${SA}" \
  --member "serviceAccount:${SA}" \
  --role "roles/iam.serviceAccountUser" \
  --project "$PROJECT"

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
echo "  SA:        $SA"
echo "  Dashboard: https://blueboot-market.web.app/cloud-batch.html"
