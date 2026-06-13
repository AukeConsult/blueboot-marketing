#!/bin/bash
# setup_all.sh — Full first-time GCP setup for cloud_batch
# Run from project root:  bash cloud_batch/setup/setup_all.sh
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "$0")" && pwd)"

export GCP_PROJECT="${GCP_PROJECT:-blueboot-market}"
export GCP_LOCATION="${GCP_LOCATION:-us-central1}"

echo "========================================"
echo "  cloud_batch GCP setup"
echo "  Project:  $GCP_PROJECT"
echo "  Location: $GCP_LOCATION"
echo "========================================"
echo ""

echo ">>> Step 1: Enable APIs"
bash "$SETUP_DIR/01_enable_apis.sh"
echo ""

echo ">>> Step 2: Service account"
bash "$SETUP_DIR/02_service_account.sh"
export BATCH_SA="batch-runner@${GCP_PROJECT}.iam.gserviceaccount.com"
echo ""

echo ">>> Step 3: Secrets"
bash "$SETUP_DIR/06_secrets.sh"
echo ""

echo ">>> Step 4: Build & push image"
bash "$SETUP_DIR/03_artifact_registry.sh"
export BATCH_IMAGE="${GCP_LOCATION}-docker.pkg.dev/${GCP_PROJECT}/batch-runner/batch-runner:latest"
echo ""

echo ">>> Step 5: Deploy Cloud Run"
bash "$SETUP_DIR/04_deploy_cloudrun.sh"
export BATCH_RUNNER_URL=$(gcloud run services describe batch-runner \
  --platform managed --region "$GCP_LOCATION" --project "$GCP_PROJECT" \
  --format "value(status.url)")
echo ""

echo ">>> Step 6: Create Cloud Scheduler jobs"
bash "$SETUP_DIR/05_setup_scheduler.sh"
echo ""

echo "========================================"
echo "  Setup complete!"
echo "  Runner URL: $BATCH_RUNNER_URL"
echo "========================================"
