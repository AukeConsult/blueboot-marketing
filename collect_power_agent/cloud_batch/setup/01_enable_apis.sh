#!/bin/bash
# 01_enable_apis.sh — Enable all GCP APIs needed for cloud_batch
set -euo pipefail

PROJECT="${GCP_PROJECT:-$(sed -n 's/^GCP_PROJECT=\([^ #]*\).*/\1/p' "$(dirname "$0")/../../.env" 2>/dev/null | head -1)}"

echo "[1/1] Enabling APIs for project: $PROJECT"
gcloud config set project "$PROJECT"

gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  --project "$PROJECT"

echo "APIs enabled."
