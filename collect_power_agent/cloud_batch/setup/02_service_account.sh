#!/bin/bash
# 02_service_account.sh — Create service account for cloud_batch runner
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
SA_NAME="batch-runner"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "[1/3] Creating service account: $SA_EMAIL"
gcloud iam service-accounts create "$SA_NAME" \
  --display-name "Batch Job Runner" \
  --project "$PROJECT" \
  || echo "  (already exists — skipping)"

echo "[2/3] Granting roles..."
for ROLE in \
  roles/datastore.user \
  roles/secretmanager.secretAccessor \
  roles/run.invoker \
  roles/cloudscheduler.admin \
  roles/logging.logWriter
do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE" \
    --condition=None \
    --quiet
  echo "  granted $ROLE"
done

echo "[3/3] Granting Cloud Build service account Artifact Registry write access..."
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer" \
  --condition=None \
  --quiet
echo "  granted roles/artifactregistry.writer to Cloud Build SA"

echo "[4/4] Service account ready: $SA_EMAIL"
echo ""
echo "  Set in environment or pass to 04_deploy_cloudrun.sh:"
echo "  export BATCH_SA=${SA_EMAIL}"
