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
ON_TEXT_KEY = "CHATREDEEM_ON_TEXT"      # chat message posted when the module is switched ON
OFF_TEXT_KEY = "CHATREDEEM_OFF_TEXT"    # chat message posted when the module is switched OFF

# {commands} in the ON text is replaced with the space-joined active commands.
DEFAULT_ON_TEXT = ("🎁 Chat-Redeems sind AN! Schreib einen dieser Commands, "
                   "um eine Belohnung auszulösen: {commands}")
DEFAULT_OFF_TEXT = "🛑 Chat-Redeems sind jetzt AUS."

_DEFAULTS = {ENABLED_KEY: "0", CHANNEL_KEY: "", ANNOUNCER_KEY: "", COMMANDS_KEY: "[]",
             ON_TEXT_KEY: DEFAULT_ON_TEXT, OFF_TEXT_KEY: DEFAULT_OFF_TEXT}

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
    """Canonical command token: lowercased, first word only, prefix kept as-is.

    The prefix sigil the user chose (``!``, ``?``, ``#`` …) is preserved, so
    "?flash" stays "?flash" — viewers must type it exactly. We do NOT force a
    "!" (doing so used to make a bare word like "flash" match "!flash" and spend
    points on ordinary chat).
    """
    c = (raw or "").strip().lower()
    if not c:
        return ""
    return c.split()[0]


def is_valid_command(cmd: str) -> bool:
    """A command must be a prefix sigil + at least one char (e.g. "!flash").

    Requiring a non-alphanumeric first char stops a bare word from accidentally
    triggering on normal chat messages.
    """
    return len(cmd) >= 2 and not cmd[0].isalnum()


def normalize_commands(items) -> list:
    """Clean + de-duplicate a list of command mappings (drops invalid ones)."""
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cmd = normalize_command(it.get("command", ""))
        rid = (it.get("reward_id") or "").strip()
        if not is_valid_command(cmd) or not rid or cmd in seen:
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
        data = json.loads(raw) if raw else []
    except ValueError:
        data = []
    cmds = normalize_commands(data if isinstance(data, list) else [])
    return {
        "enabled": _get_setting(session, ENABLED_KEY).strip().lower()
        in ("1", "true", "yes", "on"),
        "channel": (_get_setting(session, CHANNEL_KEY) or "").strip().lower(),
        "announcer": (_get_setting(session, ANNOUNCER_KEY) or "").strip().lower(),
        "commands": cmds,
        "on_text": _get_setting(session, ON_TEXT_KEY) or DEFAULT_ON_TEXT,
        "off_text": _get_setting(session, OFF_TEXT_KEY) or DEFAULT_OFF_TEXT,
    }


def render_on_text(template: str, commands: list) -> str:
    """Fill the ON-message template's {commands} placeholder with active commands."""
    cmds = [c["command"] for c in commands if c.get("enabled")]
    lst = " ".join(cmds) if cmds else "(keine)"
    tpl = template or DEFAULT_ON_TEXT
    return tpl.replace("{commands}", lst)


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
    """Creds for the announcer account (reads chat + posts on/off).

    Returns the record (with a ``logged_in`` flag) or None if no such account
    exists. The username is matched CASE-INSENSITIVELY: the announcer setting is
    stored lowercased but real usernames may contain capitals (the cookie file
    is keyed by the account's actual-case username, which ``_creds`` uses).
    """
    if not announcer:
        return None
    from sqlalchemy import func
    a = session.exec(
        select(Account).where(func.lower(Account.username) == announcer.lower())
    ).first()
    if a is None:
        return None
    return _creds(session, a)
