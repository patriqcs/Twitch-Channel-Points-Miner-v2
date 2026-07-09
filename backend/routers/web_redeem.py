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
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import config, redeem, web_redeem, web_users
from backend.db import get_session
from backend.models import Account, WebUser
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
    public: bool | None = None
    channel: str | None = None
    items: list[ItemIn] | None = None
    title: str | None = None
    tagline: str | None = None
    offline_text: str | None = None
    announce: bool | None = None
    announcer: str | None = None
    announce_text: str | None = None


@router.get("/config")
def get_config(session: Session = Depends(get_session)):
    return web_redeem.get_config(session)


@router.put("/config")
def put_config(body: WebRedeemConfig, session: Session = Depends(get_session)):
    if body.enabled is not None:
        web_redeem.set_setting(session, web_redeem.ENABLED_KEY,
                               "1" if body.enabled else "0")
    if body.public is not None:
        web_redeem.set_setting(session, web_redeem.PUBLIC_KEY,
                               "1" if body.public else "0")
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
    if body.announce is not None:
        web_redeem.set_setting(session, web_redeem.ANNOUNCE_KEY,
                               "1" if body.announce else "0")
    if body.announcer is not None:
        web_redeem.set_setting(session, web_redeem.ANNOUNCER_KEY,
                               body.announcer.strip().lower())
    if body.announce_text is not None:
        web_redeem.set_setting(session, web_redeem.ANNOUNCE_TEXT_KEY,
                               body.announce_text.strip())
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


# ---- website login users (username/password accounts for the public site) ----
def _user_read(u: WebUser) -> dict:
    return {"id": u.id, "username": u.username,
            "must_change_password": u.must_change_password,
            "approved": u.approved,
            "created_at": u.created_at, "last_seen_at": u.last_seen_at}


class WebUserCreate(BaseModel):
    username: str
    password: str | None = None   # omitted -> a random one is generated + returned


class WebUserReset(BaseModel):
    password: str | None = None   # omitted -> a random one is generated + returned


@router.get("/users")
def list_users(session: Session = Depends(get_session)):
    return [_user_read(u) for u in session.exec(select(WebUser)).all()]


@router.post("/users", status_code=201)
def create_user(body: WebUserCreate, session: Session = Depends(get_session)):
    username = (body.username or "").strip()
    if not web_users.valid_username(username):
        raise HTTPException(400, "Benutzername: 3-24 Zeichen, nur a-z, 0-9, _ . -")
    from sqlalchemy import func
    if session.exec(select(WebUser).where(
            func.lower(WebUser.username) == username.lower())).first():
        raise HTTPException(409, "Benutzername existiert bereits")
    password = body.password or secrets.token_urlsafe(9)
    problem = web_users.password_problem(password)
    if problem:
        raise HTTPException(400, problem)
    user = WebUser(username=username,
                   password_hash=web_users.hash_password(password),
                   # generated passwords are meant to be handed out -> force change
                   must_change_password=body.password is None)
    session.add(user)
    session.commit()
    session.refresh(user)
    out = _user_read(user)
    if body.password is None:
        out["generated_password"] = password
    return out


@router.post("/users/{user_id}/approve")
def approve_user(user_id: int, session: Session = Depends(get_session)):
    """Approve a self-registered account request (it can log in afterwards)."""
    user = session.get(WebUser, user_id)
    if user is None:
        raise HTTPException(404, "Benutzer nicht gefunden")
    user.approved = True
    session.add(user)
    session.commit()
    return _user_read(user)


@router.post("/users/{user_id}/reset")
def reset_password(user_id: int, body: WebUserReset,
                   session: Session = Depends(get_session)):
    user = session.get(WebUser, user_id)
    if user is None:
        raise HTTPException(404, "Benutzer nicht gefunden")
    password = body.password or secrets.token_urlsafe(9)
    problem = web_users.password_problem(password)
    if problem:
        raise HTTPException(400, problem)
    user.password_hash = web_users.hash_password(password)
    user.must_change_password = True   # the user picks their own on next login
    session.add(user)
    session.commit()
    web_users.sessions.drop_user(user_id)   # a reset logs the user out everywhere
    out = _user_read(user)
    if body.password is None:
        out["generated_password"] = password
    return out


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, session: Session = Depends(get_session)):
    user = session.get(WebUser, user_id)
    if user is None:
        raise HTTPException(404, "Benutzer nicht gefunden")
    session.delete(user)
    session.commit()
    web_users.sessions.drop_user(user_id)


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
            return redeem.fetch_channel_points(
                r["token"], proxies, ch,
                extra_headers=redeem.fp_for_username(r["username"]))
        except redeem.RedeemError:
            continue
    raise HTTPException(400, "kein eingeloggter Account zum Laden der Belohnungen")
