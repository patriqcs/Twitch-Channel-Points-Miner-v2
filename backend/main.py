# -*- coding: utf-8 -*-
"""FastAPI application entrypoint.

Phase 2 wires up the app, database and CORS. Account/Proxy/Settings routers
and the internal miner endpoints are added in later phases.
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.db import init_db
from backend.manager import manager
from backend.routers import accounts, internal, metrics, proxies, settings, system, ws

FRONTEND_DIR = Path(
    os.environ.get("FRONTEND_DIR", config.PROJECT_ROOT / "frontend" / "dist")
)

logger = logging.getLogger("backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    init_db()
    manager.start_reaper()
    logger.info("Backend ready. Data dir: %s", config.DATA_DIR)
    try:
        yield
    finally:
        manager.shutdown()


app = FastAPI(title="Twitch Miner Manager", version="0.1.0", lifespan=lifespan)

# Frontend is served same-origin in production; permissive in dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(internal.router)
app.include_router(accounts.router)
app.include_router(proxies.router)
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
