# -*- coding: utf-8 -*-
"""Chat-command channel-point redeemer: config, persistence and helpers.

A viewer typing a configured command in the streamer's chat (e.g. ``!flash``)
triggers a real channel-point redemption of the mapped reward. The points are
spent by one of the accounts flagged ``chat_redeemer`` — specifically the
earliest-free one with the MOST points (so the richest free account pays).

This module mirrors ``backend/heist.py``: it only holds config + small helpers;
the long-lived coordinator that reads chat and fires redemptions lives in
``backend/chat_redeem_manager.py`` and reuses ``backend/redeem.py`` for the
GraphQL redemption itself and ``heist.HeistIRC`` for the chat connection.
"""
import json
import logging

from sqlmodel import Session, select

from backend import redeem
from backend.models import Account, AppSetting, Proxy
from backend.proxy_util import to_engine_proxy

logger = logging.getLogger("backend.chat_redeem")

# ---- persisted config (AppSetting keys) ----
ENABLED_KEY = "CHATREDEEM_ENABLED"      # "0"/"1": module on/off (announces on toggle)
CHANNEL_KEY = "CHATREDEEM_CHANNEL"      # streamer login: chat to read + channel to redeem in
ANNOUNCER_KEY = "CHATREDEEM_ANNOUNCER"  # account username that reads chat + posts on/off
COMMANDS_KEY = "CHATREDEEM_COMMANDS"    # JSON list of command->reward mappings

_DEFAULTS = {ENABLED_KEY: "0", CHANNEL_KEY: "", ANNOUNCER_KEY: "", COMMANDS_KEY: "[]"}

# Default per-command cooldown (seconds) when a mapping doesn't specify one. Keeps
# a single command from firing on every chat line (anti-spam / points protection).
DEFAULT_COOLDOWN = 30.0
# Short per-account cooldown set after a successful redeem so back-to-back fires
# of the same reward rotate off the just-used account if another is similarly rich.
ROTATE_COOLDOWN = 3.0


def _get_setting(session: Session, key: str) -> str:
    s = session.get(AppSetting, key)
    return s.value if s is not None else _DEFAULTS.get(key, "")


def set_setting(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key)
    if s is None:
        session.add(AppSetting(key=key, value=value))
    else:
        s.value = value
        session.add(s)


def normalize_command(raw: str) -> str:
    """Canonical command token: lowercased, single leading '!', first word only."""
    c = (raw or "").strip().lower()
    if not c:
        return ""
    c = c.split()[0]
    return "!" + c.lstrip("!")


def normalize_commands(items) -> list:
    """Clean + de-duplicate a list of command mappings (drops invalid ones)."""
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cmd = normalize_command(it.get("command", ""))
        rid = (it.get("reward_id") or "").strip()
        if not cmd or not rid or cmd in seen:
            continue
        seen.add(cmd)
        try:
            cd = float(it.get("cooldown"))
        except (TypeError, ValueError):
            cd = DEFAULT_COOLDOWN
        out.append({
            "command": cmd,
            "reward_id": rid,
            "reward_title": (it.get("reward_title") or "").strip(),
            "cooldown": max(0.0, cd),
            "enabled": bool(it.get("enabled", True)),
        })
    return out


def get_config(session: Session) -> dict:
    raw = _get_setting(session, COMMANDS_KEY)
    try:
        cmds = normalize_commands(json.loads(raw) if raw else [])
    except ValueError:
        cmds = []
    return {
        "enabled": _get_setting(session, ENABLED_KEY).strip().lower()
        in ("1", "true", "yes", "on"),
        "channel": (_get_setting(session, CHANNEL_KEY) or "").strip().lower(),
        "announcer": (_get_setting(session, ANNOUNCER_KEY) or "").strip().lower(),
        "commands": cmds,
    }


# ---- account credentials (token from cookie + engine proxy) ----
def _creds(session: Session, a: Account) -> dict:
    token = redeem.account_auth_token(a.username)
    ep = to_engine_proxy(session.get(Proxy, a.proxy_id)) if a.proxy_id else None
    return {"id": a.id, "username": a.username, "token": token, "proxy": ep,
            "logged_in": token is not None}


def load_redeemer_accounts(session: Session) -> list:
    """All accounts flagged chat_redeemer, each with token/proxy + login flag."""
    return [
        _creds(session, a)
        for a in session.exec(
            select(Account).where(Account.chat_redeemer == True)  # noqa: E712
        ).all()
    ]


def announcer_creds(session: Session, announcer: str) -> "dict | None":
    """Creds for the announcer account (reads chat + posts on/off), or None."""
    if not announcer:
        return None
    a = session.exec(
        select(Account).where(Account.username == announcer)
    ).first()
    if a is None:
        return None
    rec = _creds(session, a)
    return rec if rec["logged_in"] else None
