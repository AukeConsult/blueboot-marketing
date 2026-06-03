@echo off
echo === CRM Firebase Function Deploy ===
echo.

REM Step 1: setup venv if not exists
if not exist "functions-crm\venv\Scripts\activate.bat" (
    echo [1/3] Creating venv in functions-crm\venv...
    python -m venv functions-crm\venv
) else (
    echo [1/3] venv already exists
)

REM Step 2: install/update requirements
echo [2/3] Installing requirements...
functions-crm\venv\Scripts\pip.exe install -r functions-crm\requirements.txt -q

REM Step 3: deploy both functions
echo [3/3] Deploying to Firebase...
firebase deploy --only functions:crm

echo.
echo === Deploy Complete ===
echo.
echo Endpoints:
echo   Trigger: https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/contact-sync?countries=NO
echo   Status:  https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/status/JOB_ID
echo   Jobs:    https://us-central1-blueboot-market.cloudfunctions.net/crmApi/api/crm/jobs
echo.
