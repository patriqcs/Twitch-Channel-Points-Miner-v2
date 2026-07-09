# -*- coding: utf-8 -*-
"""FastAPI application entrypoint.

Phase 2 wires up the app, database and CORS. Account/Proxy/Settings routers
and the internal miner endpoints are added in later phases.
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.db import init_db
from backend.manager import manager
from backend.proxy_monitor import ProxyHealthMonitor
from backend.watch_monitor import WatchHealthMonitor
from backend.stream_gate import StreamGateMonitor
from backend.heist_manager import heist_manager
from backend.chat_redeem_manager import chat_redeem_manager
from backend.web_redeem_manager import web_redeem_manager
from backend.routers import (
    accounts, chat_redeem, heist, internal, metrics, predictions, proxies,
    public_redeem, redeem, settings, system, web_redeem, ws,
)

FRONTEND_DIR = Path(
    os.environ.get("FRONTEND_DIR", config.PROJECT_ROOT / "frontend" / "dist")
)

logger = logging.getLogger("backend")

proxy_monitor = ProxyHealthMonitor(manager)
watch_monitor = WatchHealthMonitor(manager)
stream_gate = StreamGateMonitor(manager)


def _reset_statuses_to_stopped() -> None:
    """On boot no miner process is running yet; clear any stale 'running' state."""
    from sqlmodel import Session, select
    from backend.db import engine
    from backend.models import Account
    with Session(engine) as session:
        for acc in session.exec(select(Account)).all():
            if acc.status != "stopped":
                acc.status = "stopped"
                session.add(acc)
        session.commit()


def _autostart_when_ready() -> None:
    """Start enabled accounts as soon as their assigned proxies are reachable.

    Polls the distinct assigned proxies; once at least one responds (the shared
    tunnel is up) we start. If no account uses a proxy, we start right away. A
    hard cap (AUTOSTART_MAX_WAIT) prevents hanging — after it we start anyway and
    the proxy monitor handles any proxy that is still down.
    """
    import threading
    import time

    from sqlmodel import Session, select
    from backend.db import engine
    from backend.models import Account, Proxy
    from backend.proxy_util import to_engine_proxy

    def _proxies_ready() -> bool:
        # Build detached engine proxies INSIDE the session, then probe OUTSIDE it
        # (don't hold a pooled connection during network I/O; avoid detached-load).
        with Session(engine) as session:
            pids = {
                a.proxy_id for a in session.exec(
                    select(Account).where(Account.enabled == True)  # noqa: E712
                ).all() if a.proxy_id is not None
            }
            eps = [to_engine_proxy(session.get(Proxy, pid)) for pid in pids]
        eps = [e for e in eps if e is not None]
        if not eps:
            return True  # direct mining: nothing to wait for
        for ep in eps:
            try:
                if ep.test_proxy(timeout=6).get("ok"):
                    return True  # shared tunnel up -> relays reachable
            except Exception:  # noqa: BLE001
                pass
        return False

    def _run():
        deadline = time.monotonic() + max(0, config.AUTOSTART_MAX_WAIT)
        waited = False
        while time.monotonic() < deadline:
            if _proxies_ready():
                break
            waited = True
            time.sleep(4)
        started = manager.start_all()
        logger.info("Auto-start: launched %d account(s)%s",
                    len(started), " (proxies ready)" if waited else "")

    threading.Thread(target=_run, name="autostart", daemon=True).start()


def _start_event_pruner() -> None:
    """Hourly: delete high-volume points_snapshot events older than the retention."""
    import threading
    import time
    from datetime import timedelta

    from sqlalchemy import delete
    from sqlmodel import Session
    from backend.db import engine
    from backend.models import Event, utcnow

    if config.EVENT_RETENTION_DAYS <= 0:
        return

    def _run():
        while True:
            try:
                cutoff = utcnow() - timedelta(days=config.EVENT_RETENTION_DAYS)
                with Session(engine) as session:
                    res = session.execute(
                        delete(Event).where(
                            Event.type == "points_snapshot", Event.ts < cutoff
                        )
                    )
                    session.commit()
                    if res.rowcount:
                        logger.info("Pruned %d old points_snapshot events", res.rowcount)
            except Exception:  # noqa: BLE001
                logger.exception("event pruning failed")
            time.sleep(3600)

    threading.Thread(target=_run, name="event-pruner", daemon=True).start()


def _setup_logging() -> None:
    """Route backend logs (monitors, stream-gate, manager, ...) to stdout.

    uvicorn configures only its own loggers, so our getLogger(...) INFO messages
    would otherwise go nowhere — watchdog/gate decisions stay invisible in
    `docker logs`. Called from lifespan (AFTER uvicorn's own logging setup) so
    our handler isn't overwritten. uvicorn's access/error loggers keep
    propagate=False, so their lines are not duplicated onto this handler.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(getattr(h, "_backend_stdout", False) for h in root.handlers):
        return  # idempotent across reloads
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                          "%Y-%m-%d %H:%M:%S")
    )
    handler._backend_stdout = True
    root.addHandler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    config.ensure_dirs()
    init_db()
    from backend import redeem
    redeem.load_cooldowns()  # restore persisted redeem cooldowns across restarts
    _reset_statuses_to_stopped()
    manager.start_reaper()
    if config.PROXY_MONITOR_ENABLED:
        proxy_monitor.start()
    if config.WATCH_MONITOR_ENABLED:
        watch_monitor.start()
    if config.HEIST_COORDINATOR_ENABLED:
        heist_manager.start()
    if config.CHATREDEEM_COORDINATOR_ENABLED:
        chat_redeem_manager.start()
    if config.WEBREDEEM_COORDINATOR_ENABLED:
        web_redeem_manager.start()
    # The stream-gate REPLACES the plain boot auto-start: it runs the accounts
    # only while a streamer is live (staggering them up/down). Fall back to the
    # unconditional auto-start only when the gate is disabled.
    if config.STREAM_GATE_ENABLED:
        stream_gate.start()
        logger.info(
            "Stream-gate armed: accounts run only while a configured streamer is live."
        )
    elif config.AUTOSTART_ENABLED:
        _autostart_when_ready()
        logger.info("Auto-start armed (waits until proxies reachable, max %ss).",
                    config.AUTOSTART_MAX_WAIT)
    _start_event_pruner()
    logger.info("Backend ready. Data dir: %s", config.DATA_DIR)
    try:
        yield
    finally:
        if config.WEBREDEEM_COORDINATOR_ENABLED:
            web_redeem_manager.stop()
        if config.CHATREDEEM_COORDINATOR_ENABLED:
            chat_redeem_manager.stop()
        if config.HEIST_COORDINATOR_ENABLED:
            heist_manager.stop()
        if config.STREAM_GATE_ENABLED:
            stream_gate.stop()
        watch_monitor.stop()
        proxy_monitor.stop()
        manager.shutdown()


app = FastAPI(title="Twitch Miner Manager", version="0.1.0", lifespan=lifespan)

# Frontend is served same-origin in production; the Vite dev server is the only
# cross-origin caller (CORS_ORIGINS). The API carries no browser credentials, so
# allow_credentials stays False — this also avoids the invalid "wildcard +
# credentials" combo that browsers reject anyway.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(internal.router)
app.include_router(accounts.router)
app.include_router(proxies.router)
app.include_router(redeem.router)
app.include_router(heist.router)
app.include_router(predictions.router)
app.include_router(chat_redeem.router)
app.include_router(web_redeem.router)
app.include_router(public_redeem.router)
app.include_router(settings.router)
app.include_router(system.router)
app.include_router(metrics.router)
app.include_router(ws.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the built frontend (production). The dev server uses Vite instead.
if FRONTEND_DIR.exists():
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # API/WS namespaces are handled by their routers; everything else is the SPA.
        if full_path.startswith(("api/", "ws/", "internal/")):
            raise HTTPException(status_code=404)
        return FileResponse(FRONTEND_DIR / "index.html")
