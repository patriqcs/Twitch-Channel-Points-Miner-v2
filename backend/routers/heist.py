# -*- coding: utf-8 -*-
"""Heist module: config, live status and a manual test trigger.

The heavy lifting (observing chat, opening/joining heists) runs in the
background HeistManager (backend/heist_manager.py); these endpoints just expose
its configuration and state to the UI.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import heist
from backend.db import get_session
from backend.heist_manager import heist_manager
from backend.models import Account, Proxy
from backend.proxy_util import to_engine_proxy

router = APIRouter(prefix="/api/heist", tags=["heist"])


class HeistConfig(BaseModel):
    enabled: bool | None = None
    channel: str | None = None
    bot: str | None = None
    trigger_regex: str | None = None
    end_regex: str | None = None
    start_command: str | None = None
    join_command: str | None = None
    start_cooldown: float | None = None
    spacing_min: float | None = None
    spacing_max: float | None = None
    join_delay_ms: float | None = None


@router.get("/config")
def get_config(session: Session = Depends(get_session)):
    return heist.get_config(session)


@router.put("/config")
def put_config(body: HeistConfig, session: Session = Depends(get_session)):
    def _set_bool(key, val):
        heist.set_setting(session, key, "1" if val else "0")

    def _set_str(key, val):
        heist.set_setting(session, key, str(val))

    def _set_num(key, val, minimum=0.0):
        heist.set_setting(session, key, str(max(minimum, float(val))))

    if body.enabled is not None:
        _set_bool(heist.ENABLED_KEY, body.enabled)
    if body.channel is not None:
        _set_str(heist.CHANNEL_KEY, body.channel.strip().lower())
    if body.bot is not None:
        _set_str(heist.BOT_KEY, body.bot.strip().lower())
    if body.trigger_regex is not None:
        _set_str(heist.TRIGGER_KEY, body.trigger_regex)
    if body.end_regex is not None:
        _set_str(heist.END_KEY, body.end_regex)
    if body.start_command is not None:
        _set_str(heist.START_CMD_KEY, body.start_command.strip())
    if body.join_command is not None:
        _set_str(heist.JOIN_CMD_KEY, body.join_command.strip())
    if body.start_cooldown is not None:
        _set_num(heist.START_COOLDOWN_KEY, body.start_cooldown)
    if body.spacing_min is not None:
        _set_num(heist.SPACING_MIN_KEY, body.spacing_min)
    if body.spacing_max is not None:
        _set_num(heist.SPACING_MAX_KEY, body.spacing_max)
    if body.join_delay_ms is not None:
        _set_num(heist.JOIN_DELAY_KEY, body.join_delay_ms)
    session.commit()
    return heist.get_config(session)


@router.get("/status")
def get_status(session: Session = Depends(get_session)):
    """Live coordinator state + which accounts have which role (and a login flag)."""
    openers, joiners = [], []
    for a in session.exec(select(Account)).all():
        if not (a.heist_opener or a.heist_joiner):
            continue
        logged_in = heist.redeem.account_auth_token(a.username) is not None
        entry = {"id": a.id, "username": a.username, "logged_in": logged_in}
        if a.heist_opener:
            openers.append(entry)
        if a.heist_joiner:
            joiners.append(entry)
    return {
        "runtime": heist_manager.status(),
        "config": heist.get_config(session),
        "openers": openers,
        "joiners": joiners,
    }


class HeistTest(BaseModel):
    command: str | None = None  # defaults to the configured start command


@router.post("/test/{account_id}")
def test_fire(account_id: int, body: HeistTest,
              session: Session = Depends(get_session)):
    """Manually fire a single command (default: !heist) with one account.

    Bypasses cooldowns/scheduling — for verifying chat write access works.
    """
    acc = session.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    cfg = heist.get_config(session)
    if not cfg["channel"]:
        raise HTTPException(400, "no HEIST_CHANNEL configured")
    token = heist.redeem.account_auth_token(acc.username)
    if not token:
        raise HTTPException(400, "no auth-token - login required")
    ep = to_engine_proxy(session.get(Proxy, acc.proxy_id)) if acc.proxy_id else None
    rec = {"id": acc.id, "username": acc.username, "token": token, "proxy": ep}
    command = (body.command or cfg["start_command"]).strip()
    ok = heist.fire_heist(rec, cfg["channel"], command)
    # A successful !heist really consumes the bot's per-account cooldown, so
    # record it (only for the start command, not a join test).
    if ok and command == cfg["start_command"]:
        heist.set_cooldown(acc.id, cfg["start_cooldown"])
    return {"ok": ok, "username": acc.username, "channel": cfg["channel"],
            "command": command}


class PlayAll(BaseModel):
    command: str | None = None   # default "!play"
    delay: float | None = None   # seconds between accounts (anti-spam stagger)


@router.post("/play-all")
def play_all(body: PlayAll, session: Session = Depends(get_session)):
    """Fire a chat command (default !play) once from every logged-in account.

    Runs in the background (one short-lived proxy-routed IRC connection per
    account, small stagger between them). Uses the configured heist channel.
    """
    cfg = heist.get_config(session)
    if not cfg["channel"]:
        raise HTTPException(400, "no HEIST_CHANNEL configured")
    command = (body.command or "!play").strip()
    delay = body.delay if body.delay is not None else 1.0
    records = []
    for a in session.exec(select(Account)).all():
        token = heist.redeem.account_auth_token(a.username)
        if not token:
            continue
        ep = to_engine_proxy(session.get(Proxy, a.proxy_id)) if a.proxy_id else None
        records.append({"id": a.id, "username": a.username, "token": token,
                        "proxy": ep})
    if not records:
        raise HTTPException(400, "no logged-in accounts")
    heist.broadcast_command(records, cfg["channel"], command, delay)
    return {"scheduled": len(records), "command": command,
            "channel": cfg["channel"]}


class CooldownSet(BaseModel):
    seconds: float | None = None  # default: the configured start cooldown


@router.post("/cooldown/{account_id}")
def set_account_cooldown(account_id: int, body: CooldownSet,
                         session: Session = Depends(get_session)):
    """Mark an account on the !heist start cooldown (live, in-process + persisted).

    For accounts that fired a heist outside the scheduler (manual chat / test on
    an older build) so the scheduler stops trying to open with them too early.
    """
    acc = session.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    cfg = heist.get_config(session)
    seconds = body.seconds if body.seconds is not None else cfg["start_cooldown"]
    heist.set_cooldown(account_id, float(seconds))
    return {"account_id": account_id,
            "remaining": round(heist.cooldown_remaining(account_id), 1)}
