@echo off
REM Run / verify the filter-facets count job.
REM   run_filter_count.bat --facet site_leads
REM   run_filter_count.bat --facet NO_ecom --compare
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python app\filter_count.py %*
if errorlevel 1 exit /b 1
