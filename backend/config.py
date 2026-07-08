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
WEBREDEEM_TOKEN_FILE = Path(
    os.environ.get("WEBREDEEM_TOKEN_FILE", DATA_DIR / "webredeem_token")
)

# Shared secret for the internal miner_runner <-> backend endpoints.
# If unset, auto-generated once into INTERNAL_TOKEN_FILE.
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")

# Shared secret for the public redeem website container <-> backend endpoints
# (/api/public-redeem/*). If unset, auto-generated once into WEBREDEEM_TOKEN_FILE.
WEBREDEEM_TOKEN = os.environ.get("WEBREDEEM_TOKEN", "")

# Port the web UI / API (uvicorn) listens on inside the container.
# Accepts WEB_PORT (preferred) or PORT; defaults to 8000.
WEB_PORT = int(os.environ.get("WEB_PORT", os.environ.get("PORT", "8000")))

# Where the backend reaches itself / the miner runner reaches the backend.
# Defaults to the local web port so changing WEB_PORT keeps the internal
# miner_runner <-> backend connection working without extra config.
BACKEND_URL = os.environ.get("BACKEND_URL", f"http://127.0.0.1:{WEB_PORT}")

# Business rule: how many accounts may share one proxy (Phase 4 enforces it).
MAX_ACCOUNTS_PER_PROXY = int(os.environ.get("MAX_ACCOUNTS_PER_PROXY", "5"))

# CORS: the SPA is served same-origin in production, so cross-origin access is
# only needed for the Vite dev server. Override with a comma-separated list (or
# "*" to allow any origin). The API uses no browser credentials, so credentials
# are never echoed — keeping the wildcard a valid, safe combination.
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if o.strip()
]


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

# ---- Stream-live gate (anti-detection) ----
# Every account mines the SAME streamer list, and channel points only accrue
# while a streamer is actually live. Keeping 25 accounts connected 24/7 while the
# channel is offline earns nothing and is a bot tell (real viewers are only
# present during a broadcast). This gate polls the live status centrally (ONE
# request, not one per account) and runs the accounts ONLY while a streamer is
# live: it staggers them up when the stream starts and drains them down when it
# ends. When enabled it REPLACES the plain boot auto-start above.
#
# Hysteresis is deliberately asymmetric:
#   * online  -> react immediately (viewers show up right when the stream starts)
#   * offline -> only after OFFLINE_CONFIRM consecutive offline polls, so a brief
#     encoder/stream drop doesn't cycle all accounts.
#   * poll error -> after FAILOPEN_AFTER consecutive failures, treat as ONLINE
#     (fail-open): a broken live-check must never silently halt all mining.
# Start/stop are spread out with a random per-account gap (STEP_MIN..STEP_MAX)
# so accounts trickle in/out over minutes instead of all switching at once.
STREAM_GATE_ENABLED = _bool_env("STREAM_GATE_ENABLED", True)
STREAM_GATE_CHECK_INTERVAL = int(os.environ.get("STREAM_GATE_CHECK_INTERVAL", "60"))    # s
STREAM_GATE_OFFLINE_CONFIRM = int(os.environ.get("STREAM_GATE_OFFLINE_CONFIRM", "3"))
STREAM_GATE_FAILOPEN_AFTER = int(os.environ.get("STREAM_GATE_FAILOPEN_AFTER", "3"))
STREAM_GATE_HTTP_TIMEOUT = int(os.environ.get("STREAM_GATE_HTTP_TIMEOUT", "10"))        # s
STREAM_GATE_RAMP_STEP_MIN = int(os.environ.get("STREAM_GATE_RAMP_STEP_MIN", "20"))      # s
STREAM_GATE_RAMP_STEP_MAX = int(os.environ.get("STREAM_GATE_RAMP_STEP_MAX", "90"))      # s
STREAM_GATE_DRAIN_STEP_MIN = int(os.environ.get("STREAM_GATE_DRAIN_STEP_MIN", "10"))    # s
STREAM_GATE_DRAIN_STEP_MAX = int(os.environ.get("STREAM_GATE_DRAIN_STEP_MAX", "45"))    # s

# ---- Variable session presence (anti-detection) ----
# Identical watch-session lengths across many accounts is a bot tell — real
# viewers step away and come back at different times. While a streamer is live,
# occasionally pause a single account for a random while, then resume it, so the
# accounts' presence patterns differ instead of every account watching the exact
# same start-to-end window. Deliberately conservative: at most CONCURRENT paused
# at once and never below MIN_PRESENT running, so mining barely dips and the
# end-of-stream raid bonus (claimed within seconds, long before the gate's
# offline hysteresis stops anyone) is never at risk.
SESSION_CHURN_ENABLED = _bool_env("SESSION_CHURN_ENABLED", True)
SESSION_CHURN_INTERVAL = int(os.environ.get("SESSION_CHURN_INTERVAL", "300"))       # s, how often we roll
SESSION_CHURN_PROB = float(os.environ.get("SESSION_CHURN_PROB", "0.2"))            # chance per roll to start a pause
SESSION_CHURN_MIN_PRESENT = int(os.environ.get("SESSION_CHURN_MIN_PRESENT", "1"))  # keep >= this many running
SESSION_CHURN_MAX_CONCURRENT = int(os.environ.get("SESSION_CHURN_MAX_CONCURRENT", "1"))  # max paused at once
SESSION_PAUSE_MIN = int(os.environ.get("SESSION_PAUSE_MIN", "300"))                # s, shortest step-away
SESSION_PAUSE_MAX = int(os.environ.get("SESSION_PAUSE_MAX", "1500"))               # s, longest step-away

# ---- Miner self-repair (watchdog) ----
# When a miner subprocess for an *enabled* account exits unexpectedly, the
# reaper restarts it automatically with exponential backoff. A crash-loop guard
# grows the delay (BASE * 2**(n-1), capped at MAX) so a permanently-broken
# account (e.g. expired cookie) retries slowly instead of hot-looping. A run
# that stayed up at least HEALTHY_UPTIME seconds resets the failure streak.
MINER_AUTORESTART_ENABLED = _bool_env("MINER_AUTORESTART_ENABLED", True)
MINER_RESTART_BACKOFF_BASE = int(os.environ.get("MINER_RESTART_BACKOFF_BASE", "5"))     # s
MINER_RESTART_BACKOFF_MAX = int(os.environ.get("MINER_RESTART_BACKOFF_MAX", "300"))     # s
MINER_HEALTHY_UPTIME = int(os.environ.get("MINER_HEALTHY_UPTIME", "120"))               # s

# Heartbeat watchdog: a running miner posts a points_snapshot every ~60s. If the
# backend has seen NO event from a running account within HEARTBEAT_TIMEOUT, the
# process is considered hung (alive but not mining) and gets restarted. GRACE is
# the minimum uptime before a freshly-started miner is eligible (give it time to
# log in and produce its first event).
MINER_HEARTBEAT_ENABLED = _bool_env("MINER_HEARTBEAT_ENABLED", True)
MINER_HEARTBEAT_INTERVAL = int(os.environ.get("MINER_HEARTBEAT_INTERVAL", "30"))        # s
MINER_HEARTBEAT_TIMEOUT = int(os.environ.get("MINER_HEARTBEAT_TIMEOUT", "300"))         # s
MINER_HEARTBEAT_GRACE = int(os.environ.get("MINER_HEARTBEAT_GRACE", "240"))             # s

# Per-account subprocess log (LOGS_DIR/<username>.log) is rotated to <name>.log.1
# on each (re)start once it exceeds this size, so logs stay bounded (~2x this).
MINER_LOG_MAX_BYTES = int(os.environ.get("MINER_LOG_MAX_BYTES", str(20 * 1024 * 1024)))

# ---- Engine / entrypoint tunables (read OUTSIDE this module) ----
# These are consumed directly (via os.environ) by the miner library and the
# container entrypoint, which must stay independent of the backend package — so
# they are only DOCUMENTED here, not defined. All are optional with sane
# defaults; set them in the container environment to override.
#   MINER_HTTP_CONNECT_TIMEOUT / MINER_HTTP_READ_TIMEOUT  (default 7 / 20 s)
#       (connect, read) timeout for every engine HTTP call (Twitch.py,
#       TwitchLogin.py). Prevents a black-holed proxy from hanging a thread.
#   MINER_WS_PING_MIN / MINER_WS_PING_MAX                 (default 1.5 / 2.5 s)
#       PubSub WebSocket keep-alive ping cadence when proxied (WebSocketsPool.py);
#       tuned to stay below the Mullvad SOCKS relay's idle reaper.
#   HEIST_IRC_KEEPALIVE / HEIST_IRC_STALE                 (default 10 / 45 s)
#       Client-side IRC keep-alive interval and no-activity staleness timeout for
#       the heist/chat-redeem observer (backend/heist.py).
#   COOKIES_DIR (defined above) is now also honored by the engine and
#       miner_runner, so a custom cookie path is consistent everywhere.
#   MULLVAD_FULL_TUNNEL (docker-entrypoint-web.sh) accepts 1/true/yes/on.

# ---- Peer watch watchdog ----
# Every account watches the SAME streamers, so they form a control group: over a
# rolling WINDOW we compare each running account's point progress. If a healthy
# majority earns points (streamers are live & paying) but one account earns ~0,
# that account is half-broken (online via PubSub, but its watch POSTs fail) and
# gets failed over + restarted. Needs at least MIN_COHORT comparable accounts;
# acts only after STALL_STRIKES consecutive checks to avoid flapping.
WATCH_MONITOR_ENABLED = _bool_env("WATCH_MONITOR_ENABLED", True)
WATCH_CHECK_INTERVAL = int(os.environ.get("WATCH_CHECK_INTERVAL", "90"))    # s
WATCH_WINDOW = int(os.environ.get("WATCH_WINDOW", "600"))                   # s
WATCH_MIN_COHORT = int(os.environ.get("WATCH_MIN_COHORT", "3"))
WATCH_MIN_EARN = int(os.environ.get("WATCH_MIN_EARN", "10"))               # peer median pts
WATCH_STALL_STRIKES = int(os.environ.get("WATCH_STALL_STRIKES", "2"))

# ---- Heist module (chat mini-game coordinator) ----
# Runs a backend thread that opens heists with "opener" accounts and joins them
# with "joiner" accounts. The module itself is also gated by the DB setting
# HEIST_ENABLED (toggled from the UI); this env flag just disables the whole
# coordinator thread regardless of the DB setting.
HEIST_COORDINATOR_ENABLED = _bool_env("HEIST_COORDINATOR_ENABLED", True)

# ---- Chat-command redeemer ----
# Runs a backend thread that reads the configured channel's chat and redeems a
# mapped reward when a viewer types its command (e.g. "!flash"). Also gated by
# the DB setting CHATREDEEM_ENABLED (toggled from the UI, which triggers the
# on/off chat announcement); this env flag disables the whole thread regardless.
CHATREDEEM_COORDINATOR_ENABLED = _bool_env("CHATREDEEM_COORDINATOR_ENABLED", True)

# ---- Website redeemer ----
# Runs a backend thread that caches the web_redeemer accounts' balances + the
# channel's reward catalogue and fires redemptions triggered from the public
# redeem website (webredeem/ container, via /api/public-redeem/*). Also gated
# by the DB setting WEBREDEEM_ENABLED (toggled from the UI); this env flag
# disables the whole thread regardless.
WEBREDEEM_COORDINATOR_ENABLED = _bool_env("WEBREDEEM_COORDINATOR_ENABLED", True)


# ---- Event retention ----
# points_snapshot events are written every ~60s per account and would grow the
# DB forever; prune the high-volume ones older than this many days (0 = keep all).
EVENT_RETENTION_DAYS = int(os.environ.get("EVENT_RETENTION_DAYS", "14"))


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


def get_webredeem_token() -> str:
    """Return the public-website API token (env > file > freshly generated)."""
    if WEBREDEEM_TOKEN:
        return WEBREDEEM_TOKEN
    ensure_dirs()
    if WEBREDEEM_TOKEN_FILE.exists():
        return WEBREDEEM_TOKEN_FILE.read_text(encoding="utf-8").strip()
    import secrets

    token = secrets.token_urlsafe(32)
    WEBREDEEM_TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(WEBREDEEM_TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token
