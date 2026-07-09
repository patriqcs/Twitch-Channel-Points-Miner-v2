# -*- coding: utf-8 -*-
"""Tarn-Streamer (cover channels) für die Anti-Bot-Tarnung.

Ein Account, der 24/7 ausschließlich EINEN Kanal (j4nkttv) beobachtet und nur
diesem folgt, ist ein klares Bot-Muster — echte Zuschauer folgen/schauen mehrere
Kanäle. Dieses Modul gibt jedem Account eine **stabile, pro Account
unterschiedliche** Teilmenge aus einem Pool großer deutscher Twitch-Kanäle, die
der Miner ZUSÄTZLICH zu den eigentlichen Farm-Streamern beobachtet (und dadurch
automatisch abonniert + ihnen folgt, siehe miner_runner._auto_follow_subscribed).

Wichtig:
  * Die Tarn-Kanäle erweitern nur die **Watch-Liste** der Accounts (via
    /internal/config), NICHT die Trigger-Liste des Stream-Gates — die Accounts
    laufen weiterhin nur, wenn ein echter Farm-Streamer live ist.
  * Farm-Streamer stehen in der Liste zuerst (Priority.ORDER) -> j4nkttv-Farming
    wird nicht beeinträchtigt; Tarn-Kanäle füllen nur den zweiten Watch-Slot bzw.
    diversifizieren Abos/Follows/Watch-Minuten.
  * Die Auswahl ist deterministisch (md5 über account_id) -> stabil über
    Neustarts (eine bei jedem Start wechselnde Kanalliste wäre selbst auffällig)
    und je Account verschieden (bricht das „alle schauen dasselbe"-Cluster).
"""
import hashlib

from sqlmodel import Session

from backend.models import AppSetting

COVER_ENABLED_KEY = "COVER_ENABLED"
COVER_POOL_KEY = "COVER_STREAMERS"
COVER_COUNT_KEY = "COVER_COUNT"

# Verifizierte, häufig live große deutsche Twitch-Kanäle (Login-Namen). Große
# Kanäle = die zusätzlichen Watch-Minuten/Follows gehen in der Masse unter.
DEFAULT_COVER_POOL = [
    "montanablack88", "trymacs", "papaplatte", "eligella", "amar", "knossi",
    "gronkh", "staiy", "rewinside", "rumathra", "shlorox", "standartskill",
    "letshugo", "marcelscorpion", "inscope21", "trilluxe", "pietsmiet",
    "reeventv", "useless_hd", "tolkin", "zarbex", "reved",
]
DEFAULT_COVER_COUNT = 3
MAX_COVER_COUNT = 8


def _get(session: Session, key: str, default=None):
    s = session.get(AppSetting, key)
    return s.value if s is not None else default


def set_setting(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key)
    if s is None:
        session.add(AppSetting(key=key, value=value))
    else:
        s.value = value
        session.add(s)


def parse_pool(raw: str) -> list:
    """Kanal-Logins aus einem Textblock (eine pro Zeile, # = Kommentar)."""
    out = []
    for line in (raw or "").splitlines():
        ch = line.strip().lower()
        if ch and not ch.startswith("#") and ch not in out:
            out.append(ch)
    return out


def get_config(session: Session) -> dict:
    enabled = (_get(session, COVER_ENABLED_KEY, "1") or "1") == "1"
    raw = _get(session, COVER_POOL_KEY, None)
    pool = parse_pool(raw) if raw is not None else list(DEFAULT_COVER_POOL)
    try:
        count = int(float(_get(session, COVER_COUNT_KEY, DEFAULT_COVER_COUNT)))
    except (TypeError, ValueError):
        count = DEFAULT_COVER_COUNT
    count = max(0, min(MAX_COVER_COUNT, count))
    return {"enabled": enabled, "pool": pool, "count": count}


def cover_for_account(account_id: int, cfg: "dict | None" = None,
                      exclude: "set | None" = None) -> list:
    """Stabile, pro Account verschiedene Teilmenge des Tarn-Pools.

    exclude: Logins, die schon in der Farm-Liste stehen (nicht doppeln).
    """
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return []
    pool = [c for c in cfg.get("pool", []) if not exclude or c not in exclude]
    count = cfg.get("count", DEFAULT_COVER_COUNT)
    if count <= 0 or not pool:
        return []
    # Deterministische Rangfolge pro Account (stabil + je Account verschieden).
    ranked = sorted(
        pool,
        key=lambda ch: hashlib.md5(f"{account_id}:{ch}".encode()).hexdigest(),
    )
    return ranked[:count]
