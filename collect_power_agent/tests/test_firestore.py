import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import _pathsetup
from firestore_client import get_firestore

db = get_firestore()
docs = list(db.collection('site_leads').limit(1).stream())
print(f"  Project:          {db.project}")
print(f"  site_leads readable: OK ({len(docs)} doc)")
