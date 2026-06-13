#!/bin/bash
set -e

echo "=== CRM + Batch Runner GCP Setup ==="
echo "Project: blueboot-market"
echo ""

# ── APIs ──────────────────────────────────────────────────────────────────────
echo "[1/8] Setting project..."
gcloud config set project blueboot-market

echo "[2/8] Enabling Cloud Tasks API..."
gcloud services enable cloudtasks.googleapis.com

echo "[3/8] Enabling Cloud Functions API..."
gcloud services enable cloudfunctions.googleapis.com

echo "[4/8] Enabling Cloud Build API..."
gcloud services enable cloudbuild.googleapis.com

echo "[5/8] Enabling Cloud Scheduler API..."
gcloud services enable cloudscheduler.googleapis.com

echo "[6/8] Enabling Artifact Registry API..."
gcloud services enable artifactregistry.googleapis.com

# ── Infrastructure ────────────────────────────────────────────────────────────
echo "[7/8] Creating Cloud Tasks queue 'crm-queue'..."
gcloud tasks queues create crm-queue --location=us-central1 || echo "  Queue already exists -- skipping"

# ── IAM ───────────────────────────────────────────────────────────────────────
echo "[8/8] Granting IAM roles to service accounts..."

SA_APP="blueboot-market@appspot.gserviceaccount.com"

# CRM / Cloud Tasks
gcloud projects add-iam-policy-binding blueboot-market \
    --member="serviceAccount:${SA_APP}" \
    --role="roles/cloudtasks.enqueuer"

gcloud projects add-iam-policy-binding blueboot-market \
    --member="serviceAccount:${SA_APP}" \
    --role="roles/run.invoker"

# Batch runner — Cloud Run default service account
# Compute SA is used by Cloud Run unless a custom SA is set.
PROJECT_NUMBER=$(gcloud projects describe blueboot-market --format="value(projectNumber)")
SA_COMPUTE="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Allows the batch-runner Cloud Run service to manage Cloud Scheduler jobs
# (needed for the /sync-schedulers endpoint that creates/updates cron jobs).
gcloud projects add-iam-policy-binding blueboot-market \
    --member="serviceAccount:${SA_COMPUTE}" \
    --role="roles/cloudscheduler.admin"

# Allows Cloud Scheduler to invoke the batch-runner Cloud Run service
gcloud projects add-iam-policy-binding blueboot-market \
    --member="serviceAccount:${SA_COMPUTE}" \
    --role="roles/run.invoker"

echo ""
echo "=== GCP Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Share Google Sheets with: ${SA_APP}"
echo "  2. Run: bash deploy_crm.sh"
echo "  3. Run: bash deploy_batch.sh   (builds, deploys, seeds job defs)"
echo ""
