# -*- coding: utf-8 -*-
"""Global settings: the shared streamer list and generic key/value settings."""
from fastapi import APIRouter, Depends
from sqlmodel import Session

from backend.db import get_session
from backend.models import AppSetting
from backend.schemas import SettingWrite

router = APIRouter(prefix="/api/settings", tags=["settings"])

STREAMERS_KEY = "STREAMERS"


def _get(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _set(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key)
    if s is None:
        session.add(AppSetting(key=key, value=value))
    else:
        s.value = value
        session.add(s)
    session.commit()


@router.get("/streamers")
def get_streamers(session: Session = Depends(get_session)):
    raw = _get(session, STREAMERS_KEY)
    streamers = [l.strip() for l in raw.splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    return {"streamers": streamers, "raw": raw}


@router.put("/streamers")
def put_streamers(payload: SettingWrite, session: Session = Depends(get_session)):
    _set(session, STREAMERS_KEY, payload.value)
    return {"ok": True}


@router.get("/{key}")
def get_setting(key: str, session: Session = Depends(get_session)):
    return {"key": key, "value": _get(session, key)}


@router.put("/{key}")
def put_setting(key: str, payload: SettingWrite, session: Session = Depends(get_session)):
    _set(session, key, payload.value)
    return {"ok": True}
