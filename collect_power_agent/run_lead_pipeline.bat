@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM ============================================================
REM  LEAD PIPELINE — Web Agency / Reseller Discovery
REM  Edit COUNTRIES and CAMPAIGN before running
REM ============================================================

set COUNTRIES=FI
set CAMPAIGN=FI_jun03

echo.
echo ============================================================
echo  LEAD PIPELINE  ^|  Countries: %COUNTRIES%
echo ============================================================

REM ── Step 1: Discover agency leads ───────────────────────────
echo.
echo [1/5] Discovering agency leads...
python app\lead_agent.py --countries %COUNTRIES% --mode both
if %errorlevel% neq 0 ( echo ERROR in lead_agent.py & pause & exit /b 1 )

REM ── Step 2: AI classify each lead ───────────────────────────
echo.
echo [2/5] AI classifying leads...
python app\lead_enrich_agent.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in lead_enrich_agent.py & pause & exit /b 1 )

REM ── Step 3: Enrich contacts with social profiles ─────────────
echo.
echo [3/5] Enriching contacts...
python app\lead_enrich_contacts.py --countries %COUNTRIES% --skip-enriched
if %errorlevel% neq 0 ( echo ERROR in lead_enrich_contacts.py & pause & exit /b 1 )

REM ── Step 4: Classify email type + contact role ───────────────
echo.
echo [4/5] Classifying email types and contact roles...
python app\leads_email_check.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in leads_email_check.py & pause & exit /b 1 )

REM ── Step 5: Export to Excel and write to email_contacts ──────
echo.
echo [5/6] Exporting and writing to email_contacts...
python app\leads_smart_export.py --countries %COUNTRIES% --write-contacts --campaign %CAMPAIGN%
if %errorlevel% neq 0 ( echo ERROR in leads_smart_export.py & pause & exit /b 1 )

REM ── Step 6: Export unified review Excel ──────────────────────
echo.
echo [6/6] Exporting unified review Excel...
python app\email_contacts_export.py --countries %COUNTRIES% --campaign %CAMPAIGN% --status pending
if %errorlevel% neq 0 ( echo ERROR in email_contacts_export.py & pause & exit /b 1 )

echo.
echo ============================================================
echo  LEAD PIPELINE DONE
echo  Review Excel saved to exports\email_contacts_%COUNTRIES%_%CAMPAIGN%_pending_*.xlsx
echo ============================================================
pause
