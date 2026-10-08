@echo off
REM run_prospects_import.bat -- sync the BlueSearch prospect catalogue into campaigns.
REM Dry run by default.  Add --apply to write.  Other options: see app\prospects_import.py
REM   run_prospects_import.bat                    preview
REM   run_prospects_import.bat --apply            write
REM   run_prospects_import.bat --country UK,DK    only some countries
setlocal
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=python)
%PY% app\prospects_import.py %*
endlocal
