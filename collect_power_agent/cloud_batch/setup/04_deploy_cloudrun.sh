#!/bin/bash
# 04_deploy_cloudrun.sh — Deploy the batch runner as a Cloud Run service
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
LOCATION="${GCP_LOCATION:-us-central1}"
REPO="batch-runner"
SERVICE_NAME="batch-runner"
IMAGE="${BATCH_IMAGE:-${LOCATION}-docker.pkg.dev/${PROJECT}/${REPO}/batch-runner:latest}"
SA_EMAIL="${BATCH_SA:-${SERVICE_NAME}@${PROJECT}.iam.gserviceaccount.com}"

echo "Deploying Cloud Run service: $SERVICE_NAME"
echo "  Image:   $IMAGE"
echo "  Project: $PROJECT / $LOCATION"
echo ""

gcloud run deploy "$SERVICE_NAME" \
  --image "$IMAGE" \
  --platform managed \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --service-account "$SA_EMAIL" \
  --min-instances 1 \
  --max-instances 1 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --concurrency 4 \
  --no-allow-unauthenticated \
  --set-secrets="FIREBASE_KEY_JSON=firebase-key-json:latest,OPENAI_API_KEY=openai-key:latest,BRAVE_API_KEY=brave-key:latest,BING_API_KEY=bing-key:latest,GITHUB_TOKEN=github-token:latest,SMTP_PASSWORD=smtp-password:latest,BATCH_SECRET=batch-secret:latest" \
  --set-env-vars "GCP_PROJECT=${PROJECT},GCP_LOCATION=${LOCATION}"

echo ""
RUNNER_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --platform managed --region "$LOCATION" --project "$PROJECT" \
  --format "value(status.url)")
echo "Deployed: $RUNNER_URL"
echo ""
echo "  Set in environment for scheduler setup:"
echo "  export BATCH_RUNNER_URL=$RUNNER_URL"
echo "  export BATCH_SA=$SA_EMAIL"
