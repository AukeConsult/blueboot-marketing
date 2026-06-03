@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM ============================================================
REM  SITE PIPELINE — End-User Company Discovery
REM  Edit COUNTRIES and CAMPAIGN before running
REM ============================================================

set COUNTRIES=DK
set CAMPAIGN=DK_jun02

echo.
echo ============================================================
echo  SITE PIPELINE  ^|  Countries: %COUNTRIES%
echo ============================================================

REM ── Step 5: Classify email type + contact role ───────────────
echo.
echo [5/6] Classifying email types and contact roles...
python app\site_email_check.py --countries %COUNTRIES%
if %errorlevel% neq 0 ( echo ERROR in site_email_check.py & pause & exit /b 1 )

REM ── Step 6: Export to Excel and write to email_contacts ──────
echo.
echo [6/7] Exporting and writing to email_contacts...
python app\site_smart_export.py --countries %COUNTRIES% --write-contacts --campaign %CAMPAIGN%
if %errorlevel% neq 0 ( echo ERROR in site_smart_export.py & pause & exit /b 1 )

REM ── Step 7: Export unified review Excel ──────────────────────
echo.
echo [7/7] Exporting unified review Excel...
python app\email_contacts_export.py --countries %COUNTRIES% --campaign %CAMPAIGN% --status pending
if %errorlevel% neq 0 ( echo ERROR in email_contacts_export.py & pause & exit /b 1 )

echo.
echo ============================================================
echo  SITE PIPELINE DONE
echo  Review Excel saved to exports\email_contacts_%COUNTRIES%_%CAMPAIGN%_pending_*.xlsx
echo ============================================================
pause


