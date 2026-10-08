@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM ============================================================
REM  OUTREACH SEND
REM  Sends campaign outreach mail (intro / follow-up).
REM
REM  Default is a DRY RUN with body previews: nothing is sent or
REM  written. Real mail is sent only when --send is passed (you
REM  are asked to confirm).
REM
REM  Examples:
REM    run_outreach_send.bat --list-campaigns
REM    run_outreach_send.bat --campaigns NO_jun --limit 5
REM    run_outreach_send.bat --send --campaigns NO_jun --limit 5
REM    run_outreach_send.bat --send --mode both --campaigns NO_jun,SE_jun
REM    run_outreach_send.bat --mode followup --campaigns NO_jun
REM
REM  Options (passed through to app\outreach_send.py):
REM    --mode intro^|followup^|both   default: intro
REM    --campaigns ID [ID ...]       omit = all campaigns
REM    --limit N                     max contacts per mode (default 500)
REM    --send                        send real mail and write confirmations
REM ============================================================

set LIVE=0
set LIST=0
for %%A in (%*) do (
  if /i "%%~A"=="--send" set LIVE=1
  if /i "%%~A"=="--list-campaigns" set LIST=1
)

echo.
echo ============================================================
if "%LIVE%"=="1" (
  echo  OUTREACH SEND  ^|  LIVE - real mail will be sent
) else if "%LIST%"=="1" (
  echo  OUTREACH SEND  ^|  campaign list
) else (
  echo  OUTREACH SEND  ^|  DRY RUN - nothing is sent or written
)
echo ============================================================

if "%LIVE%"=="1" (
  set /p CONFIRM=Type YES to send real mail: 
  if /i not "!CONFIRM!"=="YES" (
    echo Cancelled.
    pause
    exit /b 1
  )
  python app\outreach_send.py %*
) else if "%LIST%"=="1" (
  python app\outreach_send.py %*
) else (
  python app\outreach_send.py --preview %*
)
if %errorlevel% neq 0 ( echo ERROR in outreach_send.py & pause & exit /b 1 )

pause
