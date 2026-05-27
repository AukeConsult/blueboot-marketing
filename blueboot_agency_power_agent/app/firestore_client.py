import firebase_admin
from firebase_admin import credentials, firestore

from blueboot_secrets import fireBaseAdminKey

_db = None


def get_firestore():
    global _db

    if _db:
        return _db

    if not firebase_admin._apps:
        cred = credentials.Certificate(fireBaseAdminKey)
        firebase_admin.initialize_app(cred)

    _db = firestore.client()

    return _db