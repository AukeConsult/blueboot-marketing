@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM ============================================================
REM  FOLLOW-UP EMAIL SYNC
REM  Syncs inbox + sent emails into contact comment_history.
REM
REM  Options (pass as arguments or edit defaults below):
REM    --campaign NO_jun     Only sync one campaign
REM    --contact  doc_id     Only sync one contact (needs --campaign)
REM    --days 30             Lookback window (default: 7, 0 = all time)
REM    --dry-run             Preview matches without writing to Firestore
REM    --list-campaigns      Print all campaign IDs and exit
REM ============================================================

set DAYS=7

echo.
echo ============================================================
echo  FOLLOW-UP EMAIL SYNC  ^|  Last %DAYS% days
echo ============================================================

python app\followup_email_sync.py --days %DAYS% %*
if %errorlevel% neq 0 ( echo ERROR in followup_email_sync.py & pause & exit /b 1 )

pause
