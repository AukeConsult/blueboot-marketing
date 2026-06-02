import threading

import firebase_admin
from firebase_admin import credentials, firestore

from blueboot_secrets import fireBaseAdminKey

_db = None
_lock = threading.Lock()   # protects _db initialisation across threads


def get_firestore():
    global _db

    # Fast path — already initialised (no lock needed for read once set)
    if _db is not None:
        return _db

    with _lock:
        # Re-check inside lock — another thread may have initialised while we waited
        if _db is not None:
            return _db

        if not firebase_admin._apps:
            cred = credentials.Certificate(fireBaseAdminKey)
            firebase_admin.initialize_app(cred)

        _db = firestore.client()

    return _db