# -*- coding: utf-8 -*-
"""System-wide miner control: start/stop all enabled accounts."""
from fastapi import APIRouter

from backend.manager import manager

router = APIRouter(prefix="/api/system", tags=["system"])


@router.post("/start-all")
def start_all():
    return {"started": manager.start_all()}


@router.post("/stop-all")
def stop_all():
    return {"stopped": manager.stop_all()}


@router.get("/running")
def running():
    return {"running": manager.statuses()}
