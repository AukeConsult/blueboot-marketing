@echo off
setlocal
call .venv\Scripts\activate.bat
python app\check_double_sends.py %*
endlocal
