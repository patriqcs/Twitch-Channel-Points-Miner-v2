# -*- coding: utf-8 -*-
"""Endpoints consumed by the PUBLIC redeem website container (webredeem/).

Protected by a shared token (X-Redeem-Token header) so the manager API can stay
off the public internet: only the small website container knows the token and
proxies exactly these calls. Never expose anything here beyond what an
anonymous visitor may see (no miner usernames, no per-account balances).

Visitors additionally authenticate as website users (WebUser, created in the
manager UI) — the session token travels in the X-Session header. The reward
catalogue and the trigger are only served to a logged-in user, so the public
page shows nothing but branding + login until then. Exception: with the
"open access" checkbox on (WEBREDEEM_PUBLIC), catalog and trigger work
without a session too — anonymous redeems are logged/announced as "Gast".

  POST /api/public-redeem/auth/register         -> account request (needs approval)
  POST /api/public-redeem/auth/login            -> session token
  POST /api/public-redeem/auth/logout
  POST /api/public-redeem/auth/change-password
  GET  /api/public-redeem/catalog               -> branding + points + items
  POST /api/public-redeem/trigger               -> fire one item's redemption
"""
import math
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import config, web_redeem, web_users
from backend.db import get_session
from backend.models import WebUser
from backend.web_redeem_manager import web_redeem_manager

router = APIRouter(prefix="/api/public-redeem", tags=["public-redeem"])


def require_token(x_redeem_token: str = Header(default="")):
    if not secrets.compare_digest(x_redeem_token, config.get_webredeem_token()):
        raise HTTPException(status_code=401, detail="bad redeem token")


def _session_user(session: Session, token: str) -> "WebUser | None":
    user_id = web_users.sessions.get_user_id(token)
    if user_id is None:
        return None
    return session.get(WebUser, user_id)


# Flood guard: at most this many unapproved requests may sit in the queue; new
# registrations are rejected beyond it (an attacker can then no longer grow the
# table, and real users get a clear "try later" message).
MAX_PENDING = 50


# ------------------------------------------------------------------ auth
class RegisterIn(BaseModel):
    username: str
    password: str


@router.post("/auth/register", dependencies=[Depends(require_token)])
def register(body: RegisterIn, session: Session = Depends(get_session)):
    username = (body.username or "").strip()
    if not web_users.valid_username(username):
        return {"ok": False,
                "message": "Benutzername: 3-24 Zeichen, nur a-z, 0-9, _ . -"}
    problem = web_users.password_problem(body.password or "")
    if problem:
        return {"ok": False, "message": problem}
    from sqlalchemy import func
    if session.exec(select(WebUser).where(
            func.lower(WebUser.username) == username.lower())).first():
        return {"ok": False, "message": "Benutzername ist schon vergeben."}
    pending = len(session.exec(select(WebUser).where(
        WebUser.approved == False)).all())  # noqa: E712
    if pending >= MAX_PENDING:
        return {"ok": False, "message": "Gerade zu viele offene Anfragen — "
                                        "bitte später nochmal versuchen."}
    session.add(WebUser(username=username,
                        password_hash=web_users.hash_password(body.password),
                        approved=False))
    session.commit()
    return {"ok": True,
            "message": "Anfrage gesendet! Dein Konto muss noch freigeschaltet "
                       "werden — versuch es später mit dem Login."}


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login", dependencies=[Depends(require_token)])
def login(body: LoginIn, session: Session = Depends(get_session)):
    username = (body.username or "").strip()
    locked = web_users.login_throttle.locked_for(username)
    if locked > 0:
        return {"ok": False, "retry_in": math.ceil(locked),
                "message": f"Zu viele Fehlversuche — gesperrt für {math.ceil(locked)}s."}
    from sqlalchemy import func
    user = session.exec(select(WebUser).where(
        func.lower(WebUser.username) == username.lower())).first()
    if user is None or not web_users.verify_password(body.password or "",
                                                     user.password_hash):
        web_users.login_throttle.note_failure(username)
        return {"ok": False, "message": "Benutzername oder Passwort falsch."}
    if not user.approved:
        # correct password, but the request has not been approved yet
        web_users.login_throttle.note_success(username)
        return {"ok": False,
                "message": "Dein Konto wurde noch nicht freigeschaltet — "
                           "bitte etwas Geduld."}
    web_users.login_throttle.note_success(username)
    user.last_seen_at = datetime.now(timezone.utc)
    session.add(user)
    session.commit()
    return {
        "ok": True,
        "token": web_users.sessions.create(user.id),
        "username": user.username,
        "must_change_password": user.must_change_password,
    }


class LogoutIn(BaseModel):
    token: str | None = None


@router.post("/auth/logout", dependencies=[Depends(require_token)])
def logout(body: LogoutIn):
    if body.token:
        web_users.sessions.drop(body.token)
    return {"ok": True}


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/auth/change-password", dependencies=[Depends(require_token)])
def change_password(body: ChangePasswordIn,
                    x_session: str = Header(default=""),
                    session: Session = Depends(get_session)):
    user = _session_user(session, x_session)
    if user is None:
        raise HTTPException(401, "Sitzung abgelaufen — bitte neu einloggen.")
    if not web_users.verify_password(body.old_password or "", user.password_hash):
        return {"ok": False, "message": "Aktuelles Passwort ist falsch."}
    problem = web_users.password_problem(body.new_password or "")
    if problem:
        return {"ok": False, "message": problem}
    user.password_hash = web_users.hash_password(body.new_password)
    user.must_change_password = False
    session.add(user)
    session.commit()
    # other devices/sessions of this user must log in again with the new password
    web_users.sessions.drop_user(user.id)
    return {"ok": True, "token": web_users.sessions.create(user.id),
            "message": "Passwort geändert."}


# ------------------------------------------------------------------ catalog / trigger
@router.get("/catalog", dependencies=[Depends(require_token)])
def catalog(x_session: str = Header(default=""),
            session: Session = Depends(get_session)):
    data = web_redeem_manager.catalog()
    data["public"] = web_redeem.get_config(session)["public"]
    user = _session_user(session, x_session)
    if user is None:
        data["user"] = None
        if not data["public"]:
            # not logged in: branding only — no rewards, no balances
            data["items"] = []
            data["points_total"] = None
        return data
    data["user"] = {"username": user.username,
                    "must_change_password": user.must_change_password}
    return data


class TriggerIn(BaseModel):
    reward_id: str


@router.post("/trigger", dependencies=[Depends(require_token)])
def trigger(body: TriggerIn, x_session: str = Header(default=""),
            session: Session = Depends(get_session)):
    user = _session_user(session, x_session)
    if user is None:
        if not web_redeem.get_config(session)["public"]:
            raise HTTPException(401, "Sitzung abgelaufen — bitte neu einloggen.")
        # open access: anonymous visitors may redeem, attributed as guest
        return web_redeem_manager.trigger(body.reward_id, "Gast")
    result = web_redeem_manager.trigger(body.reward_id, user.username)
    if result.get("ok"):
        user.last_seen_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
    return result
