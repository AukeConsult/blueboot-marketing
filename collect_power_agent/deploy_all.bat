@echo off
REM deploy_all.bat — Deploy everything in one command:
REM   1. CRM Firebase Cloud Function + Hosting
REM   2. Batch runner image (Cloud Build) + Cloud Run
REM
REM Usage:
REM   deploy_all.bat              (deploy both)
REM   deploy_all.bat --skip-crm  (batch only)
REM   deploy_all.bat --skip-batch (CRM only)

set SKIP_CRM=0
set SKIP_BATCH=0

for %%A in (%*) do (
  if "%%A"=="--skip-crm"   set SKIP_CRM=1
  if "%%A"=="--skip-batch" set SKIP_BATCH=1
)

echo ========================================
echo   Full Deploy
echo ========================================
echo.

if %SKIP_CRM%==0 (
  echo ^>^>^> [1/2] CRM - Firebase Function + Hosting
  call deploy_crm.bat
  if errorlevel 1 (
    echo ERROR: CRM deploy failed.
    exit /b 1
  )
  echo.
)

if %SKIP_BATCH%==0 (
  echo ^>^>^> [2/2] Batch Runner - Cloud Build + Cloud Run
  REM Cloud Build uses .gcloudignore to limit upload to app/, config/, cloud_batch/ only
  call gcloud builds submit --project blueboot-market .
  if errorlevel 1 (
    echo ERROR: Cloud Build failed.
    exit /b 1
  )
  call gcloud run deploy batch-runner --image us-central1-docker.pkg.dev/blueboot-market/batch-runner/batch-runner:latest --platform managed --region us-central1 --project blueboot-market --quiet
  if errorlevel 1 (
    echo ERROR: Cloud Run deploy failed.
    exit /b 1
  )
  echo.
)

echo ========================================
echo   All done!
echo   Dashboard: https://blueboot-market.web.app/
echo ========================================
