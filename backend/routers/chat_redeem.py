# -*- coding: utf-8 -*-
"""Chat-command redeemer: config, live status and a reward-catalogue helper.

The heavy lifting (reading chat, firing redemptions, on/off announcements) runs
in the background ChatRedeemManager (backend/chat_redeem_manager.py); these
endpoints expose its configuration and state to the UI. Per-account selection
(which accounts may spend points) is the ``chat_redeemer`` flag on the account,
toggled via the normal PATCH /api/accounts/{id}.
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import chat_redeem, redeem
from backend.chat_redeem_manager import chat_redeem_manager
from backend.db import get_session
from backend.models import Account

router = APIRouter(prefix="/api/chat-redeem", tags=["chat-redeem"])


class CommandIn(BaseModel):
    command: str
    reward_id: str
    reward_title: str | None = None
    cooldown: float | None = None
    enabled: bool = True


class ChatRedeemConfig(BaseModel):
    enabled: bool | None = None
    channel: str | None = None
    announcer: str | None = None
    commands: list[CommandIn] | None = None
    on_text: str | None = None
    off_text: str | None = None


@router.get("/config")
def get_config(session: Session = Depends(get_session)):
    return chat_redeem.get_config(session)


@router.put("/config")
def put_config(body: ChatRedeemConfig, session: Session = Depends(get_session)):
    if body.enabled is not None:
        chat_redeem.set_setting(session, chat_redeem.ENABLED_KEY,
                                "1" if body.enabled else "0")
    if body.channel is not None:
        chat_redeem.set_setting(session, chat_redeem.CHANNEL_KEY,
                                body.channel.strip().lower())
    if body.announcer is not None:
        chat_redeem.set_setting(session, chat_redeem.ANNOUNCER_KEY,
                                body.announcer.strip().lower())
    if body.commands is not None:
        clean = chat_redeem.normalize_commands([c.model_dump() for c in body.commands])
        chat_redeem.set_setting(session, chat_redeem.COMMANDS_KEY, json.dumps(clean))
    if body.on_text is not None:
        chat_redeem.set_setting(session, chat_redeem.ON_TEXT_KEY, body.on_text.strip())
    if body.off_text is not None:
        chat_redeem.set_setting(session, chat_redeem.OFF_TEXT_KEY, body.off_text.strip())
    session.commit()
    return chat_redeem.get_config(session)


@router.get("/status")
def get_status(session: Session = Depends(get_session)):
    """Live coordinator state + the chat_redeemer accounts (login + balance)."""
    runtime = chat_redeem_manager.status()
    balances = runtime.get("balances", {})
    redeemers = []
    for a in session.exec(
        select(Account).where(Account.chat_redeemer == True)  # noqa: E712
    ).all():
        logged_in = redeem.account_auth_token(a.username) is not None
        redeemers.append({
            "id": a.id, "username": a.username, "logged_in": logged_in,
            # balances keys are ints in-process but become strings over JSON
            "balance": balances.get(a.id, balances.get(str(a.id))),
        })
    return {
        "runtime": runtime,
        "config": chat_redeem.get_config(session),
        "redeemers": redeemers,
    }


@router.post("/announce")
def announce_now():
    """Re-post the ON announcement now (with the current saved commands)."""
    res = chat_redeem_manager.announce_now()
    if not res.get("ok"):
        raise HTTPException(400, "Modul läuft nicht / Ansage-Account nicht "
                                 f"verbunden ({res.get('reason') or 'aus'})")
    return res


class ChatTest(BaseModel):
    message: str | None = None


@router.post("/test")
def test_connection(body: ChatTest, session: Session = Depends(get_session)):
    """Connect as the announcer through its proxy and post a test line in chat.

    Synchronous (waits ~up to 17s) — a manual button, not the live loop. Returns
    a precise diagnostic so the user can see whether the proxied login + write
    works, or exactly why it doesn't.
    """
    cfg = chat_redeem.get_config(session)
    if not cfg["channel"]:
        raise HTTPException(400, "kein Channel konfiguriert")
    if not cfg["announcer"]:
        raise HTTPException(400, "kein Ansage-Account gewählt")
    rec = chat_redeem.announcer_creds(session, cfg["announcer"])
    if rec is None:
        raise HTTPException(400, f"Ansage-Account „{cfg['announcer']}\" nicht gefunden")
    if not rec["logged_in"]:
        raise HTTPException(400, f"Ansage-Account „{rec['username']}\" hat in dieser "
                                 "App keinen Login-Cookie")
    message = (body.message or "🔌 Chat-Verbindungstest").strip() or None
    result = chat_redeem.probe_announcer(cfg["channel"], rec, message)
    result["channel"] = cfg["channel"]
    result["announcer"] = rec["username"]
    return result


@router.get("/rewards")
def rewards(channel: str, session: Session = Depends(get_session)):
    """The channel's custom rewards, fetched via any usable account.

    Lets the UI populate the command->reward dropdowns. Prefers a logged-in
    chat_redeemer account, falling back to the announcer.
    """
    ch = channel.strip().lower()
    if not ch:
        raise HTTPException(400, "channel required")
    cands = [r for r in chat_redeem.load_redeemer_accounts(session) if r["logged_in"]]
    cfg = chat_redeem.get_config(session)
    ann = chat_redeem.announcer_creds(session, cfg["announcer"])
    if ann is not None and ann["logged_in"] and not any(r["id"] == ann["id"] for r in cands):
        cands.append(ann)
    for r in cands:
        proxies = r["proxy"].requests_proxies if r["proxy"] else None
        try:
            return redeem.fetch_channel_points(
                r["token"], proxies, ch,
                extra_headers=redeem.fp_for_username(r["username"]))
        except redeem.RedeemError:
            continue
    raise HTTPException(400, "kein eingeloggter Account zum Laden der Belohnungen "
                             "(Chat-Einlöser oder Ansage-Account)")
