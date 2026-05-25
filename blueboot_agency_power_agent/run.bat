@echo off
:: Run the BlueBoot Agency Power Agent directly with Python.
:: Usage: run.bat [lead_agent.py arguments]
::   e.g. run.bat --countries NO,SE --mode both --max-country 100
cd /d "%~dp0"
python app\lead_agent.py %*
pause
