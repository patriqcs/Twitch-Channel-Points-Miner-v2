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
    # Ausgeschlossene Accounts (z.B. der echte Hauptaccount patriqcs) bekommen
    # KEINE Tarn-Kanäle — ihr Verhalten bleibt sauber/real.
    is_clean = cover.is_excluded(acc.username, cover_cfg)
    if not is_clean:
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
        # Farm-Streamer (Login-Namen), die im Miner immer einen Watch-Slot
        # behalten müssen — Tarn-Kanäle dürfen sie nie verdrängen.
        "farm_streamers": sorted(farm_lower),
        "proxy": proxy,
        # Persistent per-account client fingerprint (see backend/models.py).
        "device_id": acc.device_id,
        "ua_app": acc.ua_app,
        "ua_web": acc.ua_web,
        "account_age_days": age_days,
        # True für den echten Hauptaccount (cover-excluded): der Miner lässt dann
        # die Feature-Flags auf sauberem Default und variiert sie NICHT.
        "clean": is_clean,
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
    # Auto-Pull NACH dem Commit: erst den Miner-Prozess stoppen (bricht die
    # endlose Reconnect-Schleife), DANN den dauerhaften Zustand setzen — so
    # gewinnt unser Status gegen das „stopped", das manager.stop() schreibt.
    if payload.type == "ban_signal" and config.AUTO_PULL_ENABLED:
        _auto_pull(payload)
    return {"ok": True}


def _auto_pull(payload: EventIn) -> None:
    """Ban-Signal: Prozess stoppen + Account je nach Signal deaktivieren
    ('security' = Sperre) bzw. needs_login setzen ('badauth' = Token ungültig).
    Best-effort — darf den Event-Endpoint nie scheitern lassen."""
    username = payload.username
    reason = (payload.reason or "").lower()
    # 1) Prozess stoppen (setzt Status 'stopped')
    try:
        from backend.manager import manager
        manager.stop(username)
    except Exception:  # noqa: BLE001
        pass
    # 2) dauerhaften Zustand als LETZTEN Schritt setzen (frische Session)
    try:
        from backend.db import engine
        with Session(engine) as s:
            acc = s.exec(select(Account).where(Account.username == username)).first()
            if acc is None:
                return
            if "security" in reason:
                acc.enabled = False
                acc.status = "error"
                msg = "Ban-Signal (PubSub 'security') -> Account automatisch deaktiviert"
            else:  # badauth
                acc.status = "needs_login"
                msg = "ERR_BADAUTH -> gestoppt, Login erforderlich"
            s.add(acc)
            s.add(Event(account_id=acc.id, type="ban_signal", reason=reason,
                        message=msg))
            s.commit()
    except Exception:  # noqa: BLE001
        pass
