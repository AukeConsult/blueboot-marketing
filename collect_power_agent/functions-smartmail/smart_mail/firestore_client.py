# functions-smartmail/smart_mail/firestore_client.py
"""
Firestore client for the deployed smart-mail Cloud Function.

Mirrors functions-crm/main.py's `_get_db()` bootstrap: inside the Cloud
Functions runtime there is no service-account JSON file to load -- the
platform's default service account is used via ApplicationDefault
credentials. This is the deploy-time counterpart of app/firestore_client.py
(which loads config/serviceAccountKey.json for local runs); the public
get_firestore() name is kept identical so every smart_mail/* module needs no
change beyond a relative import.

Double-checked locking guards the singleton exactly like app/firestore_client.py
and functions-crm/main.py's _get_db -- concurrent requests must never double-init.
"""
import os
import threading

import firebase_admin
from firebase_admin import credentials, firestore

GCP_PROJECT = os.getenv("GCP_PROJECT", "blueboot-market")

_db = None
_lock = threading.Lock()


def get_firestore():
    global _db
    if _db is not None:
        return _db
    with _lock:
        if _db is not None:
            return _db
        if not firebase_admin._apps:
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {"projectId": GCP_PROJECT})
        _db = firestore.client()
    return _db
