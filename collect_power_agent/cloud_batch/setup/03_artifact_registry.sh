#!/bin/bash
# 03_artifact_registry.sh — Create Artifact Registry repo and build+push the image via Cloud Build
# No local Docker required — image is built and pushed entirely on GCP.
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"
REPO="batch-runner"
IMAGE="${LOCATION}-docker.pkg.dev/${PROJECT}/${REPO}/batch-runner"

echo "[1/2] Creating Artifact Registry repository: $REPO"
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$LOCATION" \
  --description="Batch job runner images" \
  --project "$PROJECT" \
  || echo "  (already exists — skipping)"

echo "[2/2] Building and pushing image via Cloud Build..."
# .gcloudignore at project root limits upload to app/, config/, cloud_batch/ only.
# cloudbuild.yaml at project root is picked up automatically — no --config needed.
cd "$(dirname "$0")/../.."
gcloud builds submit \
  --project "$PROJECT" \
  .

echo ""
echo "Image pushed: ${IMAGE}:latest"
echo "  Set in environment for deploy step:"
echo "  export BATCH_IMAGE=${IMAGE}:latest"
