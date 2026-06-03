#!/bin/bash
set -e

echo "=== CRM GCP Setup ==="
echo "Project: blueboot-market"
echo ""

echo "[1/6] Setting project..."
gcloud config set project blueboot-market

echo "[2/6] Enabling Cloud Tasks API..."
gcloud services enable cloudtasks.googleapis.com

echo "[3/6] Enabling Cloud Functions API..."
gcloud services enable cloudfunctions.googleapis.com

echo "[4/6] Enabling Cloud Build API..."
gcloud services enable cloudbuild.googleapis.com

echo "[5/6] Creating Cloud Tasks queue 'crm-queue'..."
gcloud tasks queues create crm-queue --location=us-central1 || echo "  Queue already exists -- skipping"

echo "[6/6] Granting roles to service account..."
gcloud projects add-iam-policy-binding blueboot-market \
    --member="serviceAccount:blueboot-market@appspot.gserviceaccount.com" \
    --role="roles/cloudtasks.enqueuer"

gcloud projects add-iam-policy-binding blueboot-market \
    --member="serviceAccount:blueboot-market@appspot.gserviceaccount.com" \
    --role="roles/run.invoker"

echo ""
echo "=== GCP Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Share both Google Sheets with: blueboot-market@appspot.gserviceaccount.com"
echo "  2. Run: ./deploy_crm.sh"
echo ""
