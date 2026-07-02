# -*- coding: utf-8 -*-
"""Public redeem website: tiny FastAPI proxy in front of the miner manager.

This is the ONLY internet-facing piece. It serves the static page and forwards
a handful of API calls to the (non-public) manager backend, adding the shared
X-Redeem-Token — the token never reaches the browser, and the manager itself
stays unreachable from outside. Per-IP and global rate limits keep brute force
and spam away from the manager.

Environment:
  MANAGER_URL        e.g. http://twitch-miner-manager:8000 (required)
  REDEEM_TOKEN       shared secret (manager UI -> "Token anzeigen"), or
  REDEEM_TOKEN_FILE  path to a file containing it
  PORT               listen port (default 8080)
  TRUST_PROXY        1 = use X-Forwarded-For for the client IP (default 1;
                     correct behind Cloudflare Tunnel / a reverse proxy)
"""
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("webredeem")

MANAGER_URL = os.environ.get("MANAGER_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1").strip().lower() in ("1", "true", "yes", "on")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _redeem_token() -> str:
    token = os.environ.get("REDEEM_TOKEN", "").strip()
    if token:
        return token
    path = os.environ.get("REDEEM_TOKEN_FILE", "").strip()
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return ""


REDEEM_TOKEN = _redeem_token()
if not MANAGER_URL or not REDEEM_TOKEN:
    # Fail fast: without both the site can only ever return errors.
    raise SystemExit("MANAGER_URL and REDEEM_TOKEN (or REDEEM_TOKEN_FILE) are required")


class RateLimiter:
    """Sliding-window limiter: at most `limit` events per `window` seconds/key."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._events: dict = {}      # key -> deque[timestamps]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._events.setdefault(key, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            # opportunistic cleanup so idle keys don't accumulate forever
            if len(self._events) > 10_000:
                for k in [k for k, v in self._events.items() if not v]:
                    self._events.pop(k, None)
            return True


# generous for reads, tight for writes; redeem also has a short per-IP spacing
catalog_limit = RateLimiter(limit=60, window=60.0)
login_limit = RateLimiter(limit=10, window=60.0)
register_limit = RateLimiter(limit=3, window=3600.0)   # 3 Anfragen/Stunde/IP
redeem_limit = RateLimiter(limit=10, window=60.0)
redeem_spacing = RateLimiter(limit=1, window=2.0)
redeem_global = RateLimiter(limit=60, window=60.0)

app = FastAPI(title="Redeem Website", docs_url=None, redoc_url=None, openapi_url=None)
client = httpx.AsyncClient(base_url=MANAGER_URL, timeout=25.0)


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _headers(request: Request) -> dict:
    headers = {"X-Redeem-Token": REDEEM_TOKEN}
    session = request.headers.get("x-session", "")
    if session:
        headers["X-Session"] = session
    return headers


async def _forward(request: Request, method: str, path: str, body=None):
    try:
        resp = await client.request(method, path, json=body, headers=_headers(request))
    except httpx.HTTPError as e:
        logger.warning("manager unreachable: %s", e)
        raise HTTPException(502, "Server gerade nicht erreichbar — später nochmal versuchen.")
    if resp.status_code >= 500:
        raise HTTPException(502, "Server-Fehler — später nochmal versuchen.")
    if resp.status_code >= 400:
        detail = None
        try:
            detail = resp.json().get("detail")
        except ValueError:
            pass
        raise HTTPException(resp.status_code, detail or "Anfrage fehlgeschlagen.")
    return resp.json()


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/catalog")
async def catalog(request: Request):
    if not catalog_limit.allow(client_ip(request)):
        raise HTTPException(429, "Zu viele Anfragen.")
    return await _forward(request, "GET", "/api/public-redeem/catalog")


@app.post("/api/login")
async def login(request: Request):
    if not login_limit.allow(client_ip(request)):
        raise HTTPException(429, "Zu viele Login-Versuche — kurz warten.")
    body = await request.json()
    return await _forward(request, "POST", "/api/public-redeem/auth/login", {
        "username": str(body.get("username", ""))[:64],
        "password": str(body.get("password", ""))[:128],
    })


@app.post("/api/register")
async def register(request: Request):
    ip = client_ip(request)
    if not login_limit.allow(ip) or not register_limit.allow(ip):
        raise HTTPException(429, "Zu viele Anfragen — bitte später nochmal.")
    body = await request.json()
    return await _forward(request, "POST", "/api/public-redeem/auth/register", {
        "username": str(body.get("username", ""))[:64],
        "password": str(body.get("password", ""))[:128],
    })


@app.post("/api/logout")
async def logout(request: Request):
    body = await request.json()
    return await _forward(request, "POST", "/api/public-redeem/auth/logout", {
        "token": str(body.get("token", ""))[:128],
    })


@app.post("/api/change-password")
async def change_password(request: Request):
    if not login_limit.allow(client_ip(request)):
        raise HTTPException(429, "Zu viele Versuche — kurz warten.")
    body = await request.json()
    return await _forward(request, "POST", "/api/public-redeem/auth/change-password", {
        "old_password": str(body.get("old_password", ""))[:128],
        "new_password": str(body.get("new_password", ""))[:128],
    })


@app.post("/api/redeem")
async def redeem(request: Request):
    ip = client_ip(request)
    if not redeem_spacing.allow(ip):
        raise HTTPException(429, "Langsam! Maximal ein Klick alle 2 Sekunden.")
    if not redeem_limit.allow(ip) or not redeem_global.allow("all"):
        raise HTTPException(429, "Zu viele Einlösungen gerade — kurz warten.")
    body = await request.json()
    result = await _forward(request, "POST", "/api/public-redeem/trigger", {
        "reward_id": str(body.get("reward_id", ""))[:64],
    })
    logger.info("redeem from %s: %s -> %s", ip, body.get("reward_id"),
                "ok" if result.get("ok") else result.get("reason"))
    return result


# Static page (after the API routes so /api/* wins).
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/{full_path:path}")
async def index(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
