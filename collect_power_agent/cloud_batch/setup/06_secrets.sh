#!/bin/bash
# 06_secrets.sh — Push secrets from .env into Secret Manager
# Run once. Re-run to rotate (adds a new version automatically).
#
# Reads values from .env at the project root, then pushes each one to
# Secret Manager so Cloud Run can inject them as env vars at runtime.
# The .env file itself is excluded from the Docker image via .dockerignore.
set -euo pipefail

PROJECT="${GCP_PROJECT:-blueboot-market}"
ENV_FILE="$(cd "$(dirname "$0")/../.." && pwd)/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: .env not found at $ENV_FILE"
  exit 1
fi

echo "=== Secret Manager setup for cloud_batch ==="
echo "Project:  $PROJECT"
echo "Env file: $ENV_FILE"
echo ""

# ── Helper: read a value from .env ───────────────────────────────────────────
_read_env() {
  local KEY="$1"
  # strips quotes, handles KEY=value and KEY="value"
  grep -E "^${KEY}=" "$ENV_FILE" | head -1 | sed "s/^${KEY}=//;s/^['\"]//;s/['\"]$//"
}

# ── Helper: create or update a secret ────────────────────────────────────────
_push_secret() {
  local NAME="$1"
  local VALUE="$2"
  if [ -z "$VALUE" ]; then
    # No value in .env — create a placeholder so Cloud Run deploy doesn't fail.
    # The placeholder is only written if the secret doesn't already exist.
    if gcloud secrets describe "$NAME" --project "$PROJECT" &>/dev/null; then
      echo "  SKIP $NAME (empty in .env, secret already exists)"
    else
      echo "  PLACEHOLDER $NAME (not in .env — creating empty placeholder)"
      echo -n "placeholder" | gcloud secrets create "$NAME" \
        --data-file=- --replication-policy automatic \
        --project "$PROJECT" --quiet
    fi
    return
  fi
  if gcloud secrets describe "$NAME" --project "$PROJECT" &>/dev/null; then
    echo "  UPDATE $NAME"
    echo -n "$VALUE" | gcloud secrets versions add "$NAME" \
      --data-file=- --project "$PROJECT" --quiet
  else
    echo "  CREATE $NAME"
    echo -n "$VALUE" | gcloud secrets create "$NAME" \
      --data-file=- --replication-policy automatic \
      --project "$PROJECT" --quiet
  fi
}

# ── Push each secret ──────────────────────────────────────────────────────────

_push_secret "firebase-key-json" "$(_read_env FIREBASE_KEY_JSON)"
_push_secret "openai-key"        "$(_read_env OPENAI_API_KEY)"
_push_secret "brave-key"         "$(_read_env BRAVE_API_KEY)"
_push_secret "bing-key"          "$(_read_env BING_API_KEY)"
_push_secret "github-token"      "$(_read_env GITHUB_TOKEN)"
_push_secret "smtp-password"     "$(_read_env SMTP_PASSWORD)"

# BATCH_SECRET — generate one if not already in .env and save it automatically
BATCH_SECRET="$(_read_env BATCH_SECRET)"
if [ -z "$BATCH_SECRET" ]; then
  BATCH_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  echo "  Generated BATCH_SECRET — saving to .env"
  echo "BATCH_SECRET=${BATCH_SECRET}" >> "$ENV_FILE"
  echo "  (deploy_crm.sh will pick it up automatically on next CRM deploy)"
  export BATCH_SECRET
fi
_push_secret "batch-secret" "$BATCH_SECRET"

echo ""
echo "Done. Secrets in Secret Manager:"
echo "  firebase-key-json, openai-key, brave-key, github-token, smtp-password, batch-secret"
echo ""
echo "Next: bash $(dirname "$0")/04_deploy_cloudrun.sh"
