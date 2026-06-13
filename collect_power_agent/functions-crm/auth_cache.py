"""Small in-memory auth caches for warm Firebase Function instances."""
from __future__ import annotations

import threading
import time
from typing import Callable


ROLE_CACHE_TTL_SECONDS = 300

_role_cache_lock = threading.Lock()
_role_cache: dict[str, tuple[str, float]] = {}


def get_user_role_cached(db, email: str, fetch_role: Callable[[object, str], str]) -> str:
    """Return a cached CRM role for a Firebase user email.

    The cache is per warm function instance. Cold starts begin empty, and role
    changes may take up to ROLE_CACHE_TTL_SECONDS to appear on a warm instance.
    """
    key = (email or "").strip().lower()
    if not key:
        return "guest"

    now = time.monotonic()
    with _role_cache_lock:
        cached = _role_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

    role = fetch_role(db, key)
    expires_at = now + ROLE_CACHE_TTL_SECONDS
    with _role_cache_lock:
        _role_cache[key] = (role, expires_at)
    return role


def clear_user_role_cache(email: str | None = None) -> None:
    """Clear all cached roles or one user's cached role."""
    with _role_cache_lock:
        if email:
            _role_cache.pop(email.strip().lower(), None)
        else:
            _role_cache.clear()
