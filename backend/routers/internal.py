# -*- coding: utf-8 -*-
"""Internal endpoints used only by the miner_runner subprocesses.

Protected by a shared token (X-Internal-Token header). Not for the browser.
  GET  /internal/config/{username}  -> streamers + decrypted proxy URL + settings
  POST /internal/events             -> record a points/status/login/error event
"""
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import config, cover
from backend.db import get_session
from backend.models import Account, AppSetting, Event, Proxy
from backend.proxy_util import proxy_url

router = APIRouter(prefix="/internal", tags=["internal"])

STREAMERS_KEY = "STREAMERS"


def require_token(x_internal_token: str = Header(default="")):
    if not secrets.compare_digest(x_internal_token, config.get_internal_token()):
        raise HTTPException(status_code=401, detail="bad internal token")


@router.get("/config/{username}", dependencies=[Depends(require_token)])
def get_config(username: str, session: Session = Depends(get_session)):
    acc = session.exec(select(Account).where(Account.username == username)).first()
    if acc is None:
        raise HTTPException(status_code=404, detail="unknown account")

    setting = session.get(AppSetting, STREAMERS_KEY)
    streamers = [
        line.strip()
        for line in (setting.value.splitlines() if setting else [])
        if line.strip() and not line.strip().startswith("#")
    ]

    # Anti-Bot-Tarnung: pro Account eine stabile, verschiedene Teilmenge großer
    # deutscher Kanäle ANHÄNGEN (Farm-Streamer bleiben zuerst = Priorität). Der
    # Miner beobachtet/abonniert/folgt diesen zusätzlich -> diversere Follows,
    # Abos und Watch-Minuten. Das Stream-Gate nutzt weiterhin NUR die
    # Farm-Streamer (STREAMERS), die Accounts laufen also unverändert nur bei
    # j4nkttv-Live; die Tarn-Kanäle diversifizieren innerhalb dieser Fenster.
    farm_lower = {s.lower() for s in streamers}
    cover_cfg = cover.get_config(session)
    for ch in cover.cover_for_account(acc.id, cover_cfg, exclude=farm_lower):
        streamers.append(ch)

    proxy = None
    if not acc.no_proxy and acc.proxy_id is not None:
        proxy = proxy_url(session.get(Proxy, acc.proxy_id))

    # Account age (days) drives the behavioural warm-up: a freshly added account
    # holds back (no predictions yet, later stream-gate ramp slot) and grows into
    # full behaviour. None if unknown (very old rows) -> treated as established.
    age_days = None
    if acc.created_at is not None:
        created = acc.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)

    return {
        "username": username,
        "streamers": streamers,
        "proxy": proxy,
        # Persistent per-account client fingerprint (see backend/models.py).
        "device_id": acc.device_id,
        "ua_app": acc.ua_app,
        "ua_web": acc.ua_web,
        "account_age_days": age_days,
    }


class EventIn(BaseModel):
    username: str
    type: str
    streamer: str | None = None
    points: int | None = None
    balance: int | None = None
    reason: str | None = None
    message: str | None = None


@router.post("/events", dependencies=[Depends(require_token)])
def post_event(payload: EventIn, session: Session = Depends(get_session)):
    acc = session.exec(
        select(Account).where(Account.username == payload.username)
    ).first()
    if acc is None:
        raise HTTPException(status_code=404, detail="unknown account")

    session.add(
        Event(
            account_id=acc.id,
            type=payload.type,
            streamer=payload.streamer,
            points=payload.points,
            balance=payload.balance,
            reason=payload.reason,
            message=payload.message,
        )
    )

    # Status events drive the account's live status field.
    if payload.type == "status" and payload.reason:
        acc.status = payload.reason
        session.add(acc)
    elif payload.type == "login":
        acc.last_login_at = datetime.now(timezone.utc)
        acc.status = "running"
        session.add(acc)

    session.commit()
    return {"ok": True}
