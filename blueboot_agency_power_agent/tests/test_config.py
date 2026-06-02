import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import _pathsetup
from functions.config import cfg

cfg.validate_firebase()
cfg.validate_openai()
cfg.validate_brave()
print(f"  Firebase key:  {'OK' if cfg.FIREBASE_KEY_JSON else 'MISSING'}")
print(f"  OpenAI key:    {'OK' if cfg.OPENAI_API_KEY else 'MISSING'}")
print(f"  Brave key:     {'OK' if cfg.BRAVE_API_KEY else 'MISSING'}")
print(f"  Model:         {cfg.OPENAI_MODEL}")
