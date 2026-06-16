# -*- coding: utf-8 -*-
"""Central configuration for the web backend.

All paths default under a single DATA_DIR so the whole app maps to one
persistent volume on Unraid. Override any of them via environment variables.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Single persistent data directory (cookies, logs, db, secret key).
DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data")).resolve()
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "app.db"))
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", DATA_DIR / "cookies"))
LOGS_DIR = Path(os.environ.get("LOGS_DIR", DATA_DIR / "logs"))
SECRET_KEY_FILE = Path(os.environ.get("SECRET_KEY_FILE", DATA_DIR / "secret.key"))
INTERNAL_TOKEN_FILE = Path(
    os.environ.get("INTERNAL_TOKEN_FILE", DATA_DIR / "internal_token")
)

# Shared secret for the internal miner_runner <-> backend endpoints.
# If unset, auto-generated once into INTERNAL_TOKEN_FILE.
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")

# Port the web UI / API (uvicorn) listens on inside the container.
# Accepts WEB_PORT (preferred) or PORT; defaults to 8000.
WEB_PORT = int(os.environ.get("WEB_PORT", os.environ.get("PORT", "8000")))

# Where the backend reaches itself / the miner runner reaches the backend.
# Defaults to the local web port so changing WEB_PORT keeps the internal
# miner_runner <-> backend connection working without extra config.
BACKEND_URL = os.environ.get("BACKEND_URL", f"http://127.0.0.1:{WEB_PORT}")

# Business rule: how many accounts may share one proxy (Phase 4 enforces it).
MAX_ACCOUNTS_PER_PROXY = int(os.environ.get("MAX_ACCOUNTS_PER_PROXY", "5"))


def _bool_env(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ---- Proxy auto-failover (health monitor) ----
# Periodically connectivity-tests each running account's proxy. A proxy that
# fails PROXY_FAIL_THRESHOLD checks in a row is treated as dead: the account is
# moved to another working proxy, or (if none free and PROXY_ALLOW_DIRECT) kept
# mining without a proxy as a last resort.
PROXY_MONITOR_ENABLED = _bool_env("PROXY_MONITOR_ENABLED", True)
PROXY_CHECK_INTERVAL = int(os.environ.get("PROXY_CHECK_INTERVAL", "120"))  # seconds
PROXY_FAIL_THRESHOLD = int(os.environ.get("PROXY_FAIL_THRESHOLD", "2"))
PROXY_ALLOW_DIRECT = _bool_env("PROXY_ALLOW_DIRECT", True)

# ---- Auto-start on boot ----
# On container start, (re)start the enabled accounts automatically — but wait
# until their assigned proxies are actually reachable first (no fixed delay).
# AUTOSTART_MAX_WAIT caps that wait so boot can't hang forever; if it's hit, we
# start anyway and the proxy monitor sorts out any still-dead proxy.
AUTOSTART_ENABLED = _bool_env("AUTOSTART_ENABLED", True)
AUTOSTART_MAX_WAIT = int(os.environ.get("AUTOSTART_MAX_WAIT", "180"))  # seconds


def ensure_dirs() -> None:
    """Create all required directories (idempotent)."""
    for d in (DATA_DIR, COOKIES_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def get_internal_token() -> str:
    """Return the internal API token (env > file > freshly generated)."""
    if INTERNAL_TOKEN:
        return INTERNAL_TOKEN
    ensure_dirs()
    if INTERNAL_TOKEN_FILE.exists():
        return INTERNAL_TOKEN_FILE.read_text(encoding="utf-8").strip()
    import secrets

    token = secrets.token_urlsafe(32)
    INTERNAL_TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(INTERNAL_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token
