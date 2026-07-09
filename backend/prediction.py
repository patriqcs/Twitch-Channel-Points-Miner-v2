# -*- coding: utf-8 -*-
"""Kanalwetten (Twitch Predictions): manueller All-in auf EIN Ergebnis.

Der Miner selbst wettet nie (make_predictions=False in miner_runner). Dieses
Modul lässt den Operator im Web-UI ein Ergebnis der aktiven Kanalwette wählen
und setzt dann mit allen wettberechtigten Accounts (alle aktivierten Accounts
außer einer Ausschlussliste, Default: patriqcs) ALLE Kanalpunkte auf genau
dieses Ergebnis — direkt aus dem Backend per OAuth-Token + Proxy je Account,
nach dem Muster von redeem.py/heist.py, ohne die Miner-Prozesse anzufassen.

Ablauf einer Wett-Runde (start_run):
  * Accounts werden gemischt und nacheinander mit zufälligem Abstand
    (spacing_min..spacing_max) abgearbeitet, damit nicht alle Alts in derselben
    Sekunde identische Einsätze abfeuern;
  * pro Account wird der Punktestand live geholt und min(balance, MAX_BET)
    gesetzt (Twitch-Limit: 250k pro Wette, Minimum 10);
  * läuft das Zeitfenster der Wette ab oder wird abgebrochen, werden die
    restlichen Accounts übersprungen.

Es läuft höchstens EINE Runde gleichzeitig; der Zustand der letzten Runde
bleibt für das UI abrufbar, bis eine neue gestartet wird.
"""
import logging
import random
import threading
import time
import uuid
from datetime import datetime
from secrets import token_hex

import requests
from sqlmodel import Session, select

from backend import redeem
from backend.db import engine
from backend.models import Account, AppSetting, Event, Proxy
from backend.proxy_util import to_engine_proxy

logger = logging.getLogger("backend.prediction")

CHANNEL_KEY = "PREDICTION_CHANNEL"
EXCLUDE_KEY = "PREDICTION_EXCLUDE"
SPACING_MIN_KEY = "PREDICTION_SPACING_MIN"
SPACING_MAX_KEY = "PREDICTION_SPACING_MAX"

# Der Haupt-Account wettet nie mit (kommasepariert erweiterbar im UI).
DEFAULT_EXCLUDE = "patriqcs"

# Twitch-Limits pro Wette und Account
MIN_BET = 10
MAX_BET = 250_000

# Puffer vor dem Sperrzeitpunkt: so kurz vor knapp feuern wir nicht mehr.
LOCK_SAFETY_SECONDS = 3.0


# ------------------------------------------------------------------ settings
def _get_setting(session: Session, key: str, default=None):
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
    def _num(key, default):
        try:
            return max(0.0, float(_get_setting(session, key, default) or default))
        except (TypeError, ValueError):
            return float(default)
    return {
        "channel": (_get_setting(session, CHANNEL_KEY, "") or "").strip().lower(),
        "exclude": _get_setting(session, EXCLUDE_KEY, DEFAULT_EXCLUDE) or "",
        "spacing_min": _num(SPACING_MIN_KEY, 1.0),
        "spacing_max": _num(SPACING_MAX_KEY, 4.0),
    }


def excluded_usernames(cfg: dict) -> set:
    return {u.strip().lower() for u in cfg["exclude"].split(",") if u.strip()}


def eligible_accounts(session: Session, cfg: "dict | None" = None) -> list:
    """Alle aktivierten Accounts außer der Ausschlussliste, mit Token+Proxy.

    Accounts ohne Login (kein Cookie/Token) bleiben in der Liste (logged_in
    False), damit das UI sie anzeigen kann; wetten können nur die mit Token.
    """
    cfg = cfg or get_config(session)
    excluded = excluded_usernames(cfg)
    out = []
    for acc in session.exec(select(Account).where(Account.enabled == True)).all():  # noqa: E712
        if acc.username.lower() in excluded:
            continue
        token = redeem.account_auth_token(acc.username)
        proxies = None
        if acc.proxy_id is not None:
            ep = to_engine_proxy(session.get(Proxy, acc.proxy_id))
            proxies = ep.requests_proxies if ep is not None else None
        out.append({
            "id": acc.id, "username": acc.username,
            "logged_in": token is not None,
            "token": token, "proxies": proxies,
        })
    out.sort(key=lambda r: r["username"].lower())
    return out


# ------------------------------------------------------------------ GQL
# Volltext-Query (wie redeem.py): liefert die aktive(n) Kanalwette(n) samt
# Ergebnissen. Der Miner selbst bekommt Predictions nur per PubSub, daher gibt
# es dafür keinen persistierten Hash in constants.py.
_ACTIVE_PREDICTION_QUERY = """
query ActivePredictionEvents($channelLogin: String!) {
  community: user(login: $channelLogin) {
    id
    displayName
    channel {
      id
      activePredictionEvents {
        id
        title
        status
        createdAt
        predictionWindowSeconds
        outcomes { id title color totalPoints totalUsers }
      }
    }
  }
}"""

# Gleicher persistierter Hash wie der Miner (constants.py GQLOperations
# .MakePrediction) — der nachweislich funktionierende Weg für diese Mutation.
_MAKE_PREDICTION_HASH = (
    "b44682ecc88358817009f20e69d75081b1e58825bb40aa53d5dbadcc17c881d8"
)
# Volltext-Fallback, falls Twitch den persistierten Hash irgendwann verwirft.
_MAKE_PREDICTION_MUTATION = """
mutation MakePrediction($input: MakePredictionInput!) {
  makePrediction(input: $input) {
    prediction { id }
    error { code }
  }
}"""

# Vollständige Fehler-Codes aus dem Twitch-Web-Bundle (PredictionError-Enum).
_ERROR_MESSAGES = {
    # AGB der Kanalwetten nicht akzeptiert. Twitch bietet dafür KEINE API — die
    # Zustimmung geht nur einmalig über die offizielle Website/App (erster Tipp
    # auf eine Wette + Häkchen bei den „Predictions Terms"). Danach wettet der
    # Account dauerhaft per API. Nachgewiesen: MakePredictionInput hat kein
    # Zustimmungs-Feld, es gibt keine Accept-Mutation, ProductConsentType kennt
    # keinen Predictions-Typ.
    "MUST_ACCEPT_TOS": "AGB nicht akzeptiert — einmalig auf twitch.tv annehmen "
                       "(siehe Hinweis).",
    "NOT_ENOUGH_POINTS": "Nicht genug Punkte.",
    "EVENT_NOT_ACTIVE": "Wette ist nicht mehr offen.",
    "EVENT_LOCKED": "Wette ist bereits gesperrt.",
    "MAX_POINTS_PER_EVENT": "Über dem Twitch-Maximum pro Wette (250k).",
    "MAX_POINTS_EXCEEDED": "Über dem Twitch-Maximum pro Wette (250k).",
    "DUPLICATE_TRANSACTION": "Doppelte Transaktion.",
    "TRANSACTION_IN_PROGRESS": "Transaktion läuft bereits.",
    "RATE_LIMITED": "Von Twitch rate-limitiert — kurz warten.",
    "FORBIDDEN": "Nicht erlaubt (gesperrt/eingeschränkt).",
    "NOT_FOUND": "Wette/Ergebnis nicht gefunden.",
    "MULTIPLE_OUTCOMES": "Bereits auf ein anderes Ergebnis gesetzt.",
    "REGION_LOCKED": "In dieser Region gesperrt.",
    "CATEGORY_REGION_LOCKED": "Kategorie in dieser Region gesperrt.",
    "SPECTATOR_MODE_INELIGIBLE": "Account nicht wettberechtigt (Zuschauer-Modus).",
    "SPECTATOR_MODE_DUPLICATE": "Doppelte Wette (Zuschauer-Modus).",
    "EVENT_MANAGER": "Event-Manager-Fehler.",
    "UNKNOWN": "Unbekannter Twitch-Fehler.",
}

# Codes, bei denen der Account eine einmalige manuelle Freischaltung braucht
# (nicht durch Wiederholen lösbar) — fürs UI separat markiert.
TOS_BLOCKED_CODE = "MUST_ACCEPT_TOS"


def _post_gql(token, proxies, body, timeout=15):
    """Wie redeem._gql, aber mit fertigem Request-Body (für persistedQuery)."""
    headers = {
        "Content-Type": "application/json",
        "Client-Id": redeem.TWITCH_WEB_CLIENT_ID,
        "Authorization": f"OAuth {token}",
    }
    try:
        resp = requests.post(redeem.GQL_ENDPOINT, json=body, headers=headers,
                             proxies=proxies, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise redeem.RedeemError(f"network error: {e}")
    if resp.status_code == 401:
        raise redeem.RedeemError("token rejected (401) - login expired/invalid")
    if not resp.ok:
        raise redeem.RedeemError(f"GraphQL HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        raise redeem.RedeemError("invalid JSON response")
    if payload.get("errors"):
        msgs = "; ".join(str(e.get("message", e)) for e in payload["errors"][:3])
        raise redeem.RedeemError(f"GraphQL error: {msgs}")
    data = payload.get("data")
    if data is None:
        raise redeem.RedeemError("GraphQL response without data")
    return data


def _parse_event(ev: dict) -> dict:
    created = ev.get("createdAt") or ""
    window = int(ev.get("predictionWindowSeconds") or 0)
    locks_at = None
    try:
        locks_at = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                    .timestamp() + window)
    except ValueError:
        pass
    return {
        "id": ev["id"],
        "title": ev.get("title") or "",
        "status": (ev.get("status") or "").upper(),
        "created_at": created,
        "window_seconds": window,
        # Sekunden bis zur Sperre (Snapshot); None wenn createdAt unlesbar
        "locks_in": (max(0.0, locks_at - time.time()) if locks_at else None),
        "locks_at_epoch": locks_at,
        "outcomes": [{
            "id": o["id"],
            "title": o.get("title") or "",
            "color": (o.get("color") or "").upper(),
            "total_points": int(o.get("totalPoints") or 0),
            "total_users": int(o.get("totalUsers") or 0),
        } for o in (ev.get("outcomes") or [])],
    }


def fetch_active_prediction(token, proxies, channel_login: str) -> dict:
    """Liefert {channel_id, display_name, event|None} für einen Kanal."""
    data = redeem._gql(token, proxies, "ActivePredictionEvents",
                       _ACTIVE_PREDICTION_QUERY, {"channelLogin": channel_login})
    community = data.get("community")
    if not community:
        raise redeem.RedeemError(f'Kanal "{channel_login}" nicht gefunden')
    channel = community.get("channel") or {}
    events = [_parse_event(e) for e in (channel.get("activePredictionEvents") or [])]
    # ACTIVE bevorzugen (nur darauf kann gesetzt werden), sonst LOCKED anzeigen
    event = next((e for e in events if e["status"] == "ACTIVE"),
                 events[0] if events else None)
    return {
        "channel_id": channel.get("id"),
        "display_name": community.get("displayName") or channel_login,
        "event": event,
    }


def make_prediction(token, proxies, event_id: str, outcome_id: str,
                    points: int) -> dict:
    """Setzt `points` auf ein Ergebnis. Returns {ok, message?, code?}."""
    variables = {"input": {
        "eventID": event_id,
        "outcomeID": outcome_id,
        "points": int(points),
        "transactionID": token_hex(16),
    }}
    body = {
        "operationName": "MakePrediction",
        "variables": variables,
        "extensions": {"persistedQuery": {
            "version": 1, "sha256Hash": _MAKE_PREDICTION_HASH,
        }},
    }
    try:
        data = _post_gql(token, proxies, body)
    except redeem.RedeemError as e:
        if "PersistedQueryNotFound" not in str(e):
            return {"ok": False, "message": str(e)}
        try:
            data = redeem._gql(token, proxies, "MakePrediction",
                               _MAKE_PREDICTION_MUTATION, variables)
        except redeem.RedeemError as e2:
            return {"ok": False, "message": str(e2)}
    result = (data or {}).get("makePrediction") or {}
    err = result.get("error")
    if err and err.get("code"):
        code = (err["code"] or "").upper()
        return {"ok": False, "code": code,
                "message": _ERROR_MESSAGES.get(code, f"abgelehnt: {code}")}
    return {"ok": True}


def fetch_balances(candidates: list, channel: str, max_workers: int = 8) -> dict:
    """Punktestände aller Kandidaten parallel holen: {account_id: (balance|None, err|None)}."""
    from concurrent.futures import ThreadPoolExecutor

    def one(c):
        if not c["token"]:
            return c["id"], None, "kein Login"
        try:
            state = redeem.fetch_channel_points(c["token"], c["proxies"], channel)
            return c["id"], int(state.get("balance") or 0), None
        except redeem.RedeemError as e:
            return c["id"], None, str(e)

    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for acc_id, balance, err in ex.map(one, candidates):
            out[acc_id] = (balance, err)
    return out


# ------------------------------------------------------------------ run state
_run_lock = threading.Lock()
_run: "dict | None" = None          # aktuelle ODER letzte Runde (fürs UI)
_cancel = threading.Event()


def run_active() -> bool:
    with _run_lock:
        return _run is not None and not _run["done"]


def run_status() -> "dict | None":
    with _run_lock:
        if _run is None:
            return None
        r = {k: v for k, v in _run.items() if not k.startswith("_")}
        r["results"] = [dict(x) for x in _run["results"]]
        return r


def cancel_run() -> bool:
    with _run_lock:
        if _run is None or _run["done"]:
            return False
    _cancel.set()
    return True


def _log_event(account_id: int, channel: str, points: "int | None",
               message: str) -> None:
    """Darf NIE raisen — läuft im Worker-Thread ohne umschließendes except."""
    try:
        with Session(engine) as s:
            s.add(Event(account_id=account_id, type="prediction",
                        streamer=channel, points=points, message=message))
            s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not log prediction event for account %s", account_id)


def start_run(channel: str, event: dict, outcome_id: str, candidates: list,
              spacing_min: float, spacing_max: float) -> str:
    """Startet die All-in-Runde im Hintergrund. Raises RuntimeError wenn belegt."""
    outcome = next((o for o in event["outcomes"] if o["id"] == outcome_id), None)
    if outcome is None:
        raise ValueError("outcome not found on event")
    candidates = [c for c in candidates if c["token"]]
    if not candidates:
        raise ValueError("no usable account login")
    random.shuffle(candidates)

    run = {
        "run_id": uuid.uuid4().hex[:12],
        "channel": channel,
        "event_id": event["id"],
        "event_title": event["title"],
        "outcome_id": outcome_id,
        "outcome_title": outcome["title"],
        "locks_at_epoch": event.get("locks_at_epoch"),
        "started_at": time.time(),
        "done": False,
        "cancelled": False,
        "results": [{
            "account_id": c["id"], "username": c["username"],
            "status": "waiting", "balance": None, "points": None,
            "message": "", "code": None,
        } for c in candidates],
        "_candidates": candidates,
        "_spacing": (min(spacing_min, spacing_max), max(spacing_min, spacing_max)),
    }
    global _run
    with _run_lock:
        if _run is not None and not _run["done"]:
            raise RuntimeError("a prediction run is already active")
        _cancel.clear()
        _run = run
    threading.Thread(target=_worker, args=(run,), daemon=True,
                     name="prediction-run").start()
    logger.info('prediction run %s: %d account(s) all-in on "%s" (%s, #%s)',
                run["run_id"], len(candidates), outcome["title"],
                event["title"], channel)
    return run["run_id"]


def _worker(run: dict) -> None:
    locks_at = run["locks_at_epoch"]
    lo, hi = run["_spacing"]
    last = len(run["_candidates"]) - 1
    for i, cand in enumerate(run["_candidates"]):
        res = run["results"][i]

        def _set(**kw):
            with _run_lock:
                res.update(**kw)

        if _cancel.is_set():
            _set(status="skipped", message="abgebrochen")
            continue
        if locks_at is not None and time.time() > locks_at - LOCK_SAFETY_SECONDS:
            _set(status="skipped", message="Wette gesperrt (Zeitfenster abgelaufen)")
            continue

        _set(status="betting")
        try:
            state = redeem.fetch_channel_points(cand["token"], cand["proxies"],
                                                run["channel"])
            balance = int(state.get("balance") or 0)
        except redeem.RedeemError as e:
            _set(status="failed", message=f"Punktestand: {e}")
            _log_event(cand["id"], run["channel"], None,
                       f"Wette fehlgeschlagen (Punktestand): {e}")
            continue

        amount = min(balance, MAX_BET)
        if amount < MIN_BET:
            _set(status="skipped", balance=balance,
                 message=f"unter Minimum ({MIN_BET} Punkte)")
            continue

        r = make_prediction(cand["token"], cand["proxies"], run["event_id"],
                            run["outcome_id"], amount)
        if r["ok"]:
            _set(status="ok", balance=balance, points=amount,
                 message=f"{amount:,} Punkte gesetzt".replace(",", "."))
            _log_event(cand["id"], run["channel"], amount,
                       f'Wette: {amount} Punkte auf "{run["outcome_title"]}" '
                       f'({run["event_title"]})')
            logger.info("prediction: %s bet %d on \"%s\"", cand["username"],
                        amount, run["outcome_title"])
        else:
            code = r.get("code")
            # AGB-Sperre ist kein „Fehlschlag" durch Wiederholen lösbar, sondern
            # eine einmalige manuelle Freischaltung -> eigener Status fürs UI.
            status = "tos_blocked" if code == TOS_BLOCKED_CODE else "failed"
            _set(status=status, balance=balance, message=r["message"], code=code)
            _log_event(cand["id"], run["channel"], None,
                       f'Wette fehlgeschlagen: {r["message"]}')
            logger.warning("prediction: %s failed: %s", cand["username"],
                           r["message"])

        if i < last:
            time.sleep(random.uniform(lo, hi))

    with _run_lock:
        run["cancelled"] = _cancel.is_set()
        run["done"] = True
    ok = sum(1 for x in run["results"] if x["status"] == "ok")
    total = sum(x["points"] or 0 for x in run["results"])
    logger.info("prediction run %s finished: %d/%d ok, %d points total",
                run["run_id"], ok, len(run["results"]), total)
