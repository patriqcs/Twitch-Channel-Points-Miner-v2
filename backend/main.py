# -*- coding: utf-8 -*-
"""FastAPI application entrypoint.

Phase 2 wires up the app, database and CORS. Account/Proxy/Settings routers
and the internal miner endpoints are added in later phases.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend import config
from backend.db import init_db

logger = logging.getLogger("backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    init_db()
    logger.info("Backend ready. Data dir: %s", config.DATA_DIR)
    yield


app = FastAPI(title="Twitch Miner Manager", version="0.1.0", lifespan=lifespan)

# Frontend is served same-origin in production; permissive in dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}
