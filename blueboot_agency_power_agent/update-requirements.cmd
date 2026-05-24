python3 -m venv .venv
.venv/bin/activate

pip install pip-tools
pip freeze --local > requirements.txt
pip install -U -r requirements.txt


