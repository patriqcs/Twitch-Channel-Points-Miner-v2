# -*- coding: utf-8 -*-
"""Badge-Quests (Pokémon-Reward-Drops): Konfiguration, Status, manuelle Lane-Wahl.

Die Arbeit macht der Hintergrund-Thread backend/quest_manager.py; hier nur die
Oberfläche dazu.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from backend import quest
from backend.db import get_session
from backend.quest_manager import quest_manager

router = APIRouter(prefix="/api/quest", tags=["quest"])


class QuestConfig(BaseModel):
    enabled: bool | None = None
    account: str | None = None
    priority_raw: str | None = None
    pool_raw: str | None = None
    max_minutes: int | None = None
    stall_minutes: int | None = None


class SelectBody(BaseModel):
    lane: str


@router.get("/config")
def get_config(session: Session = Depends(get_session)):
    return quest.get_config(session)


@router.put("/config")
def put_config(body: QuestConfig, session: Session = Depends(get_session)):
    if body.enabled is not None:
        quest.set_setting(session, quest.ENABLED_KEY, "1" if body.enabled else "0")
    if body.account is not None and body.account.strip():
        quest.set_setting(session, quest.ACCOUNT_KEY, body.account.strip())
    if body.priority_raw is not None:
        quest.set_setting(session, quest.PRIORITY_KEY, body.priority_raw)
    if body.pool_raw is not None:
        quest.set_setting(session, quest.POOL_KEY, body.pool_raw)
    if body.max_minutes is not None:
        quest.set_setting(session, quest.MAX_MINUTES_KEY, str(body.max_minutes))
    if body.stall_minutes is not None:
        quest.set_setting(session, quest.STALL_MINUTES_KEY, str(body.stall_minutes))
    session.commit()
    quest_manager.poke()
    return quest.get_config(session)


@router.get("/status")
def get_status():
    return quest_manager.status()


@router.post("/refresh")
def refresh():
    quest_manager.poke()
    return {"ok": True}


@router.post("/select")
def select_lane(body: SelectBody):
    res = quest_manager.select_lane_manual(body.lane)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Fehler")
    return res
