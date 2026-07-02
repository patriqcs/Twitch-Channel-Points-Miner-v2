# -*- coding: utf-8 -*-
"""Website login accounts: password hashing, sessions and login throttling.

Users of the PUBLIC redeem website (webredeem/ container) authenticate with a
username + password created in the manager UI. This module holds the pieces
the routers share:

  * scrypt password hashing (stdlib only, no new dependency);
  * an in-memory session store (token -> user id) with a sliding TTL — sessions
    reset on a manager restart, users simply log in again;
  * a small per-username failed-login throttle so the public site cannot be
    used to brute-force passwords even below the website's per-IP rate limit.
"""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,24}$")
MIN_PASSWORD_LEN = 8

SESSION_TTL = 7 * 24 * 3600.0     # sliding: refreshed on every authenticated call
LOGIN_MAX_FAILS = 5               # per username ...
LOGIN_LOCK_SECONDS = 60.0         # ... then locked for this long

# scrypt parameters (n, r, p): interactive-login strength, ~16 MiB memory
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username or ""))


def password_problem(password: str) -> "str | None":
    """Why a password is not acceptable (None = fine)."""
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen haben"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"),
                            salt=base64.b64decode(salt_b64),
                            n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


class SessionStore:
    """Token -> user id with a sliding TTL. In-memory (resets on restart)."""

    def __init__(self, ttl: float = SESSION_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._sessions: dict = {}     # token -> {"user_id": int, "expires": float}

    def create(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._prune(now)
            self._sessions[token] = {"user_id": user_id, "expires": now + self.ttl}
        return token

    def get_user_id(self, token: str) -> "int | None":
        """Resolve a session token, refreshing its TTL (sliding expiry)."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            rec = self._sessions.get(token)
            if rec is None or rec["expires"] < now:
                self._sessions.pop(token, None)
                return None
            rec["expires"] = now + self.ttl
            return rec["user_id"]

    def drop(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def drop_user(self, user_id: int) -> None:
        """Kill all sessions of one user (delete / password reset)."""
        with self._lock:
            for t in [t for t, r in self._sessions.items() if r["user_id"] == user_id]:
                self._sessions.pop(t, None)

    def _prune(self, now: float) -> None:
        for t in [t for t, r in self._sessions.items() if r["expires"] < now]:
            self._sessions.pop(t, None)


class LoginThrottle:
    """Per-username failed-attempt lockout (anti brute force)."""

    def __init__(self, max_fails: int = LOGIN_MAX_FAILS,
                 lock_seconds: float = LOGIN_LOCK_SECONDS):
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._lock = threading.Lock()
        self._fails: dict = {}        # username -> {"count": int, "locked_until": float}

    def locked_for(self, username: str) -> float:
        """Remaining lock seconds for a username (0 = free)."""
        with self._lock:
            rec = self._fails.get(username.lower())
            if rec is None:
                return 0.0
            return max(0.0, rec["locked_until"] - time.time())

    def note_failure(self, username: str) -> None:
        with self._lock:
            rec = self._fails.setdefault(username.lower(),
                                         {"count": 0, "locked_until": 0.0})
            rec["count"] += 1
            if rec["count"] >= self.max_fails:
                rec["count"] = 0
                rec["locked_until"] = time.time() + self.lock_seconds

    def note_success(self, username: str) -> None:
        with self._lock:
            self._fails.pop(username.lower(), None)


# Module-level singletons shared by the public-redeem router.
sessions = SessionStore()
login_throttle = LoginThrottle()
