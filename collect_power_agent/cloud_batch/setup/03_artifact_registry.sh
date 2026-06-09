#!/bin/bash
# 03_artifact_registry.sh — Create Artifact Registry repo and build+push the image
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"
REPO="batch-runner"
IMAGE="${LOCATION}-docker.pkg.dev/${PROJECT}/${REPO}/batch-runner"

echo "[1/3] Creating Artifact Registry repository: $REPO"
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$LOCATION" \
  --description="Batch job runner images" \
  --project "$PROJECT" \
  || echo "  (already exists — skipping)"

echo "[2/3] Configuring Docker auth..."
gcloud auth configure-docker "${LOCATION}-docker.pkg.dev" --quiet

echo "[3/3] Building and pushing image..."
# Must be run from project root
cd "$(dirname "$0")/../.."
docker build -f cloud_batch/Dockerfile -t "${IMAGE}:latest" .
docker push "${IMAGE}:latest"

echo ""
echo "Image pushed: ${IMAGE}:latest"
echo "  Set in environment for deploy step:"
echo "  export BATCH_IMAGE=${IMAGE}:latest"
