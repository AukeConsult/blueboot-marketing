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
  --memory "${BATCH_MEMORY:-4Gi}" \
  --cpu "${BATCH_CPU:-4}" \
  --timeout "${BATCH_TIMEOUT:-3600}" \
  --no-cpu-throttling \
  --min-instances "${BATCH_MIN_INSTANCES:-1}" \
  --max-instances "${BATCH_MAX_INSTANCES:-3}" \
  --no-allow-unauthenticated \
  --set-secrets="FIREBASE_KEY_JSON=firebase-key-json:latest,OPENAI_API_KEY=openai-key:latest,BRAVE_API_KEY=brave-key:latest,BING_API_KEY=bing-key:latest,GITHUB_TOKEN=github-token:latest,SMTP_PASSWORD=smtp-password:latest,BATCH_SECRET=batch-secret:latest" \
  --set-env-vars "GCP_PROJECT=${PROJECT},GCP_LOCATION=${LOCATION}"
# Note: --concurrency is intentionally omitted. /run returns 202 immediately and
# jobs run in background threads so Cloud Run never sees concurrent requests.
# --no-cpu-throttling is required so background threads get full CPU after 202 response.

# Resolve URL and set BATCH_RUNNER_URL + BATCH_SA as env vars on the service
RUNNER_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --platform managed --region "$LOCATION" --project "$PROJECT" \
  --format "value(status.url)")

echo ""
echo "Setting BATCH_RUNNER_URL and BATCH_SA env vars..."
gcloud run services update "$SERVICE_NAME" \
  --update-env-vars "BATCH_RUNNER_URL=${RUNNER_URL},BATCH_SA=${SA_EMAIL}" \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --quiet

# Grant SA permission to invoke the Cloud Run service
echo "Granting run.invoker to ${SA_EMAIL}..."
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region "$LOCATION" \
  --project "$PROJECT" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role "roles/run.invoker" \
  --quiet

echo ""
echo "Deployed: $RUNNER_URL"
echo "  BATCH_RUNNER_URL=$RUNNER_URL"
echo "  BATCH_SA=$SA_EMAIL"
