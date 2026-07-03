# -*- coding: utf-8 -*-
"""Website channel-point redeemer: config, persistence and helpers.

A visitor clicking a reward card on the public redeem website triggers a real
channel-point redemption of the mapped reward — same machinery as the chat
command redeemer, but driven by HTTP instead of IRC. The points are spent by
one of the accounts flagged ``web_redeemer`` (richest free one pays), selected
independently from the chat_redeemer set.

This module mirrors ``backend/chat_redeem.py``: it only holds config + small
helpers; the long-lived coordinator lives in ``backend/web_redeem_manager.py``
and reuses ``backend/redeem.py`` for the GraphQL redemption itself. The public
website (webredeem/ container) talks to the token-protected endpoints in
``backend/routers/public_redeem.py``.
"""
import json
import logging

from sqlmodel import Session, select

from backend import redeem
from backend.models import Account, AppSetting, Proxy
from backend.proxy_util import to_engine_proxy

logger = logging.getLogger("backend.web_redeem")

# ---- persisted config (AppSetting keys) ----
ENABLED_KEY = "WEBREDEEM_ENABLED"    # "0"/"1": module on/off
CHANNEL_KEY = "WEBREDEEM_CHANNEL"    # streamer login: channel to redeem in
ITEMS_KEY = "WEBREDEEM_ITEMS"        # JSON list of website item -> reward mappings
TITLE_KEY = "WEBREDEEM_TITLE"        # public page headline
TAGLINE_KEY = "WEBREDEEM_TAGLINE"    # public page subline
OFFLINE_TEXT_KEY = "WEBREDEEM_OFFLINE_TEXT"  # shown on the page while disabled
ANNOUNCE_KEY = "WEBREDEEM_ANNOUNCE"          # "0"/"1": post web redeems in chat
ANNOUNCER_KEY = "WEBREDEEM_ANNOUNCER"        # account username that posts them
ANNOUNCE_TEXT_KEY = "WEBREDEEM_ANNOUNCE_TEXT"  # template: {user} {reward} {cost}

DEFAULT_CHANNEL = "j4nkttv"
DEFAULT_TITLE = "j4nkTTV Redeems"
DEFAULT_TAGLINE = ("Kanalpunkte-Belohnungen für den Stream von j4nkttv — "
                   "direkt hier auslösen, ganz ohne Chat.")
DEFAULT_OFFLINE_TEXT = ("Die Web-Redeems sind gerade pausiert. "
                        "Schau später nochmal vorbei!")
DEFAULT_ANNOUNCE_TEXT = ('🌐 {user} hat gerade „{reward}" über die Webseite '
                         'eingelöst!')

_DEFAULTS = {ENABLED_KEY: "0", CHANNEL_KEY: DEFAULT_CHANNEL, ITEMS_KEY: "[]",
             TITLE_KEY: DEFAULT_TITLE, TAGLINE_KEY: DEFAULT_TAGLINE,
             OFFLINE_TEXT_KEY: DEFAULT_OFFLINE_TEXT,
             ANNOUNCE_KEY: "0", ANNOUNCER_KEY: "",
             ANNOUNCE_TEXT_KEY: DEFAULT_ANNOUNCE_TEXT}

# Default per-item cooldown (seconds) when a mapping doesn't specify one. Keeps
# a single reward from being fired on every click (anti-spam / points protection).
DEFAULT_COOLDOWN = 60.0
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


def normalize_items(items) -> list:
    """Clean + de-duplicate the item list (one item per reward, invalid dropped)."""
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        rid = (it.get("reward_id") or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        try:
            cd = float(it.get("cooldown"))
        except (TypeError, ValueError):
            cd = DEFAULT_COOLDOWN
        out.append({
            "reward_id": rid,
            "label": (it.get("label") or "").strip(),
            "reward_title": (it.get("reward_title") or "").strip(),
            "description": (it.get("description") or "").strip(),
            "cooldown": max(0.0, cd),
            "enabled": bool(it.get("enabled", True)),
        })
    return out


def get_config(session: Session) -> dict:
    raw = _get_setting(session, ITEMS_KEY)
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        data = []
    items = normalize_items(data if isinstance(data, list) else [])
    return {
        "enabled": _get_setting(session, ENABLED_KEY).strip().lower()
        in ("1", "true", "yes", "on"),
        "channel": (_get_setting(session, CHANNEL_KEY) or "").strip().lower(),
        "items": items,
        "title": _get_setting(session, TITLE_KEY) or DEFAULT_TITLE,
        "tagline": _get_setting(session, TAGLINE_KEY) or DEFAULT_TAGLINE,
        "offline_text": _get_setting(session, OFFLINE_TEXT_KEY) or DEFAULT_OFFLINE_TEXT,
        "announce": _get_setting(session, ANNOUNCE_KEY).strip().lower()
        in ("1", "true", "yes", "on"),
        "announcer": (_get_setting(session, ANNOUNCER_KEY) or "").strip().lower(),
        "announce_text": _get_setting(session, ANNOUNCE_TEXT_KEY)
        or DEFAULT_ANNOUNCE_TEXT,
    }


def render_announce_text(template: str, user: str, reward_title: str,
                         cost: "int | None") -> str:
    """Fill the chat announcement template's placeholders."""
    tpl = template or DEFAULT_ANNOUNCE_TEXT
    return (tpl.replace("{user}", user or "jemand")
            .replace("{reward}", reward_title)
            .replace("{cost}", str(cost) if cost is not None else "?"))


# ---- account credentials (token from cookie + engine proxy) ----
def _creds(session: Session, a: Account) -> dict:
    token = redeem.account_auth_token(a.username)
    ep = to_engine_proxy(session.get(Proxy, a.proxy_id)) if a.proxy_id else None
    return {"id": a.id, "username": a.username, "token": token, "proxy": ep,
            "logged_in": token is not None}


def load_redeemer_accounts(session: Session) -> list:
    """All accounts flagged web_redeemer, each with token/proxy + login flag."""
    return [
        _creds(session, a)
        for a in session.exec(
            select(Account).where(Account.web_redeemer == True)  # noqa: E712
        ).all()
    ]
