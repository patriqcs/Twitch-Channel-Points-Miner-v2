# -*- coding: utf-8 -*-
"""Website redeemer: config + live status for the manager UI.

The heavy lifting (balance/catalogue cache, firing redemptions) runs in the
background WebRedeemManager (backend/web_redeem_manager.py); these endpoints
expose its configuration and state to the manager UI. Per-account selection
(which accounts may spend points) is the ``web_redeemer`` flag on the account,
toggled via the normal PATCH /api/accounts/{id}.

The PUBLIC endpoints the website container calls live in
backend/routers/public_redeem.py (token-protected).
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import config, redeem, web_redeem
from backend.db import get_session
from backend.models import Account
from backend.web_redeem_manager import web_redeem_manager

router = APIRouter(prefix="/api/web-redeem", tags=["web-redeem"])


class ItemIn(BaseModel):
    reward_id: str
    label: str | None = None
    reward_title: str | None = None
    description: str | None = None
    cooldown: float | None = None
    enabled: bool = True


class WebRedeemConfig(BaseModel):
    enabled: bool | None = None
    channel: str | None = None
    items: list[ItemIn] | None = None
    title: str | None = None
    tagline: str | None = None
    offline_text: str | None = None


@router.get("/config")
def get_config(session: Session = Depends(get_session)):
    return web_redeem.get_config(session)


@router.put("/config")
def put_config(body: WebRedeemConfig, session: Session = Depends(get_session)):
    if body.enabled is not None:
        web_redeem.set_setting(session, web_redeem.ENABLED_KEY,
                               "1" if body.enabled else "0")
    if body.channel is not None:
        web_redeem.set_setting(session, web_redeem.CHANNEL_KEY,
                               body.channel.strip().lower())
    if body.items is not None:
        clean = web_redeem.normalize_items([i.model_dump() for i in body.items])
        web_redeem.set_setting(session, web_redeem.ITEMS_KEY, json.dumps(clean))
    if body.title is not None:
        web_redeem.set_setting(session, web_redeem.TITLE_KEY, body.title.strip())
    if body.tagline is not None:
        web_redeem.set_setting(session, web_redeem.TAGLINE_KEY, body.tagline.strip())
    if body.offline_text is not None:
        web_redeem.set_setting(session, web_redeem.OFFLINE_TEXT_KEY,
                               body.offline_text.strip())
    session.commit()
    return web_redeem.get_config(session)


@router.get("/status")
def get_status(session: Session = Depends(get_session)):
    """Live coordinator state + the web_redeemer accounts (login + balance)."""
    runtime = web_redeem_manager.status()
    balances = runtime.get("balances", {})
    redeemers = []
    for a in session.exec(
        select(Account).where(Account.web_redeemer == True)  # noqa: E712
    ).all():
        logged_in = redeem.account_auth_token(a.username) is not None
        redeemers.append({
            "id": a.id, "username": a.username, "logged_in": logged_in,
            # balances keys are ints in-process but become strings over JSON
            "balance": balances.get(a.id, balances.get(str(a.id))),
        })
    return {
        "runtime": runtime,
        "config": web_redeem.get_config(session),
        "redeemers": redeemers,
    }


@router.get("/token")
def get_token():
    """Shared secret for the public website container (X-Redeem-Token header).

    The manager UI shows this once during setup; the public container passes it
    via the REDEEM_TOKEN env var. Exposed here because the manager UI itself is
    the trusted, non-public admin surface.
    """
    return {"token": config.get_webredeem_token()}


@router.get("/rewards")
def rewards(channel: str, session: Session = Depends(get_session)):
    """The channel's custom rewards, fetched via any usable account.

    Lets the UI populate the item->reward dropdowns. Prefers a logged-in
    web_redeemer account, falling back to any logged-in account.
    """
    ch = channel.strip().lower()
    if not ch:
        raise HTTPException(400, "channel required")
    cands = [r for r in web_redeem.load_redeemer_accounts(session) if r["logged_in"]]
    if not cands:
        for a in session.exec(select(Account)).all():
            rec = web_redeem._creds(session, a)  # noqa: SLF001 (same package)
            if rec["logged_in"]:
                cands.append(rec)
    for r in cands:
        proxies = r["proxy"].requests_proxies if r["proxy"] else None
        try:
            return redeem.fetch_channel_points(r["token"], proxies, ch)
        except redeem.RedeemError:
            continue
    raise HTTPException(400, "kein eingeloggter Account zum Laden der Belohnungen")
