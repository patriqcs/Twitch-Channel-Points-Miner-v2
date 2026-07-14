# -*- coding: utf-8 -*-
"""Tag/Nacht-Rhythmus pro Account (Anti-Bot).

Ein Account, der rund um die Uhr — auch um 4 Uhr morgens — online/präsent ist,
sieht wie ein Bot aus. Echte Zuschauer schlafen. Dieses Modul gibt jedem Account
ein **stabiles, pro Account leicht versetztes Schlaf-Fenster** (tiefe Nacht),
während dem er nicht hochgefahren wird und nicht als Offline-Präsenz dient.

Eigenschaften:
  * Deterministisch aus der account_id abgeleitet -> über Neustarts stabil und je
    Account leicht anders (nicht alle schlafen exakt gleich).
  * Fenster liegt in der tiefen Nacht (Start ~23:00–02:00 Europe/Berlin), sodass
    normale Abend-Streams (dt. Primetime) nicht beeinträchtigt werden — nur echte
    Nacht-Präsenz wird ausgedünnt.
  * Zeitzone Europe/Berlin (DE-Accounts). Fällt zoneinfo aus -> fe ster UTC+1.
"""
import hashlib
from datetime import datetime, timezone, timedelta

try:  # Python 3.9+; Container hat i.d.R. tzdata
    from zoneinfo import ZoneInfo
    _BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001
    _BERLIN = timezone(timedelta(hours=1))  # Fallback: fixes MEZ

from sqlmodel import Session

from backend.models import AppSetting

DIURNAL_ENABLED_KEY = "DIURNAL_ENABLED"
DIURNAL_SLEEP_HOURS_KEY = "DIURNAL_SLEEP_HOURS"

DEFAULT_SLEEP_HOURS = 7.0
MIN_SLEEP_HOURS = 0.0
MAX_SLEEP_HOURS = 12.0


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


def get_config(session: Session) -> dict:
    enabled = (_get(session, DIURNAL_ENABLED_KEY, "1") or "1") == "1"
    try:
        hours = float(_get(session, DIURNAL_SLEEP_HOURS_KEY, DEFAULT_SLEEP_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_SLEEP_HOURS
    hours = max(MIN_SLEEP_HOURS, min(MAX_SLEEP_HOURS, hours))
    return {"enabled": enabled, "sleep_hours": hours}


def _night_seed(local: datetime) -> int:
    """Stabile Kennung der aktuellen Nacht: lokale Zeit minus 12 h, als Tages-
    Ordinalzahl. So fällt ein ganzes Schlaf-Fenster (~23:00–06:00) in EINE
    Kennung — der Tages-Jitter kippt nicht mitten in der Nacht um Mitternacht,
    driftet aber von Nacht zu Nacht."""
    return (local - timedelta(hours=12)).toordinal()


def _sleep_start_hour(account_id: int, night: int) -> float:
    """Schlaf-Beginn (Stunde, Europe/Berlin) je Account: Basis 23:00–01:59 plus
    ±25 min Tages-Jitter, damit das Fenster nicht über Monate sekundengenau
    reproduzierbar ist (null Drift = Verhaltens-Hash)."""
    h = int(hashlib.md5(f"sleep:{account_id}".encode()).hexdigest(), 16)
    base = 23 + (h % 3) + ((h >> 4) % 60) / 60.0
    j = int(hashlib.md5(f"sleepjit:{account_id}:{night}".encode()).hexdigest(), 16)
    offset = ((j % 51) - 25) / 60.0  # -25 .. +25 min
    return (base + offset) % 24.0


def is_awake(account_id: int, cfg: dict, now: "datetime | None" = None) -> bool:
    """True, wenn der Account gerade wach ist (nicht in seinem Schlaf-Fenster)."""
    if not cfg.get("enabled", True):
        return True
    hours = cfg.get("sleep_hours", DEFAULT_SLEEP_HOURS)
    if hours <= 0:
        return True
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_BERLIN)
    cur = local.hour + local.minute / 60.0
    night = _night_seed(local)
    start = _sleep_start_hour(account_id, night)
    # Fensterlänge ±20 min Tages-Jitter (gleiche Nacht-Kennung → stabil in der Nacht).
    hj = int(hashlib.md5(f"durjit:{account_id}:{night}".encode()).hexdigest(), 16)
    hours = max(0.0, hours + ((hj % 41) - 20) / 60.0)  # ±20 min
    end = start + hours  # kann > 24 laufen (Wrap über Mitternacht)
    # Ist cur im [start, end) mod 24?
    if end <= 24.0:
        asleep = start <= cur < end
    else:  # Wrap-around
        asleep = cur >= start or cur < (end - 24.0)
    return not asleep
