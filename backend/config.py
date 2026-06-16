# -*- coding: utf-8 -*-
"""Central configuration for the web backend.

All paths default under a single DATA_DIR so the whole app maps to one
persistent volume on Unraid. Override any of them via environment variables.
"""
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Single persistent data directory (cookies, logs, db, secret key).
DATA_DIR = Path(os.environ.get("DATA_DIR", _PROJECT_ROOT / "data")).resolve()
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "app.db"))
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", DATA_DIR / "cookies"))
LOGS_DIR = Path(os.environ.get("LOGS_DIR", DATA_DIR / "logs"))
SECRET_KEY_FILE = Path(os.environ.get("SECRET_KEY_FILE", DATA_DIR / "secret.key"))

# Shared secret for the internal miner_runner <-> backend endpoints (Phase 3).
# Auto-generated into DATA_DIR if unset.
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")

# Business rule: how many accounts may share one proxy (Phase 4 enforces it).
MAX_ACCOUNTS_PER_PROXY = int(os.environ.get("MAX_ACCOUNTS_PER_PROXY", "5"))


def ensure_dirs() -> None:
    """Create all required directories (idempotent)."""
    for d in (DATA_DIR, COOKIES_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
