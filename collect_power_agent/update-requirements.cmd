py -3.13 -m venv .venv
call .venv\Scripts\activate.bat

pip install pip-tools
pip freeze --local > requirements.txt
pip install -U -r requirements.txt


