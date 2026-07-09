# -*- coding: utf-8 -*-
"""Global settings: the shared streamer list and generic key/value settings."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from backend import cover
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


class CoverWrite(BaseModel):
    enabled: bool | None = None
    raw: str | None = None            # Pool als Text (eine Kanal-Login pro Zeile)
    count: int | None = None          # Tarn-Kanäle pro Account
    offline_presence: int | None = None  # Accounts (rotierend) bei Farm-Offline
    offline_hours: float | None = None    # Fensterlänge (Stunden)


@router.get("/cover")
def get_cover(session: Session = Depends(get_session)):
    """Tarn-Streamer-Konfiguration (Pool + Anzahl + Offline-Präsenz)."""
    cfg = cover.get_config(session)
    return {
        "enabled": cfg["enabled"],
        "pool": cfg["pool"],
        "raw": "\n".join(cfg["pool"]),
        "count": cfg["count"],
        "max_count": cover.MAX_COVER_COUNT,
        "default_pool": cover.DEFAULT_COVER_POOL,
        "offline_presence": cfg["offline_presence"],
        "offline_hours": cfg["offline_hours"],
        "max_offline_presence": cover.MAX_OFFLINE_PRESENCE,
        "max_offline_hours": cover.MAX_OFFLINE_HOURS,
    }


@router.put("/cover")
def put_cover(payload: CoverWrite, session: Session = Depends(get_session)):
    if payload.enabled is not None:
        cover.set_setting(session, cover.COVER_ENABLED_KEY,
                          "1" if payload.enabled else "0")
    if payload.raw is not None:
        # normalisiert speichern (eine Login pro Zeile, dedupliziert)
        cover.set_setting(session, cover.COVER_POOL_KEY,
                          "\n".join(cover.parse_pool(payload.raw)))
    if payload.count is not None:
        c = max(0, min(cover.MAX_COVER_COUNT, int(payload.count)))
        cover.set_setting(session, cover.COVER_COUNT_KEY, str(c))
    if payload.offline_presence is not None:
        p = max(0, min(cover.MAX_OFFLINE_PRESENCE, int(payload.offline_presence)))
        cover.set_setting(session, cover.COVER_OFFLINE_KEY, str(p))
    if payload.offline_hours is not None:
        h = max(0.0, min(cover.MAX_OFFLINE_HOURS, float(payload.offline_hours)))
        cover.set_setting(session, cover.COVER_OFFLINE_HOURS_KEY, str(h))
    session.commit()
    return get_cover(session)


@router.get("/{key}")
def get_setting(key: str, session: Session = Depends(get_session)):
    return {"key": key, "value": _get(session, key)}


@router.put("/{key}")
def put_setting(key: str, payload: SettingWrite, session: Session = Depends(get_session)):
    _set(session, key, payload.value)
    return {"ok": True}
