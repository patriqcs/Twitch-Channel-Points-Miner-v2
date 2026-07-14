# -*- coding: utf-8 -*-
"""Kanalwetten-Endpunkte: aktive Wette anzeigen, All-in-Runde starten/verfolgen.

Die eigentliche Arbeit (GQL, Hintergrund-Runde) liegt in backend/prediction.py;
diese Endpunkte sind nur die dünne API fürs Web-UI (Tab "Wetten").
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from backend import prediction, redeem
from backend.db import get_session

logger = logging.getLogger("backend.predictions")

router = APIRouter(prefix="/api/predictions", tags=["predictions"])


class PredictionConfig(BaseModel):
    channel: str | None = None
    exclude: str | None = None
    spacing_min: float | None = None
    spacing_max: float | None = None
    bet_pct_min: float | None = None
    bet_pct_max: float | None = None
    participation_pct: float | None = None
    counter_pct: float | None = None


@router.get("/config")
def get_config(session: Session = Depends(get_session)):
    return prediction.get_config(session)


@router.put("/config")
def put_config(body: PredictionConfig, session: Session = Depends(get_session)):
    if body.channel is not None:
        prediction.set_setting(session, prediction.CHANNEL_KEY,
                               body.channel.strip().lower())
    if body.exclude is not None:
        prediction.set_setting(session, prediction.EXCLUDE_KEY, body.exclude.strip())
    if body.spacing_min is not None:
        prediction.set_setting(session, prediction.SPACING_MIN_KEY,
                               str(max(0.0, float(body.spacing_min))))
    if body.spacing_max is not None:
        prediction.set_setting(session, prediction.SPACING_MAX_KEY,
                               str(max(0.0, float(body.spacing_max))))
    if body.bet_pct_min is not None:
        prediction.set_setting(session, prediction.BET_PCT_MIN_KEY,
                               str(max(1.0, min(100.0, float(body.bet_pct_min)))))
    if body.bet_pct_max is not None:
        prediction.set_setting(session, prediction.BET_PCT_MAX_KEY,
                               str(max(1.0, min(100.0, float(body.bet_pct_max)))))
    if body.participation_pct is not None:
        prediction.set_setting(session, prediction.PARTICIPATION_PCT_KEY,
                               str(max(1.0, min(100.0, float(body.participation_pct)))))
    if body.counter_pct is not None:
        prediction.set_setting(session, prediction.COUNTER_PCT_KEY,
                               str(max(0.0, min(100.0, float(body.counter_pct)))))
    session.commit()
    return prediction.get_config(session)


def _channel_or_400(channel: "str | None", session: Session) -> str:
    ch = (channel or "").strip().lower()
    if not ch:
        ch = prediction.get_config(session)["channel"]
    if not ch:
        raise HTTPException(400, "kein Kanal angegeben")
    return ch


def _scout(candidates: list):
    """Erster Account mit Login — holt die Wett-Daten für alle."""
    scout = next((c for c in candidates if c["token"]), None)
    if scout is None:
        raise HTTPException(400, "kein wettberechtigter Account mit Login")
    return scout


@router.get("/active")
def get_active(channel: str | None = None,
               session: Session = Depends(get_session)):
    """Aktive Kanalwette + wettberechtigte Accounts (ohne Punktestände)."""
    ch = _channel_or_400(channel, session)
    candidates = prediction.eligible_accounts(session)
    scout = _scout(candidates)
    try:
        data = prediction.fetch_active_prediction(
            scout["token"], scout["proxies"], ch,
            device_id=scout.get("device_id"), user_agent=scout.get("ua_app"))
    except redeem.RedeemError as e:
        raise HTTPException(400, str(e))
    return {
        "channel": ch,
        "channel_id": data["channel_id"],
        "display_name": data["display_name"],
        "event": data["event"],
        "accounts": [{"id": c["id"], "username": c["username"],
                      "logged_in": c["logged_in"]} for c in candidates],
    }


@router.get("/balances")
def get_balances(channel: str | None = None,
                 session: Session = Depends(get_session)):
    """Punktestände aller wettberechtigten Accounts auf dem Kanal (parallel)."""
    ch = _channel_or_400(channel, session)
    candidates = prediction.eligible_accounts(session)
    if not candidates:
        return {"channel": ch, "accounts": [], "total_balance": 0}
    balances = prediction.fetch_balances(candidates, ch)
    accounts = []
    total = 0
    for c in candidates:
        balance, err = balances.get(c["id"], (None, "nicht geprüft"))
        if balance is not None:
            total += balance
        accounts.append({"id": c["id"], "username": c["username"],
                         "logged_in": c["logged_in"],
                         "balance": balance, "error": err})
    return {"channel": ch, "accounts": accounts, "total_balance": total}


class BetRequest(BaseModel):
    channel: str
    event_id: str
    outcome_id: str


@router.post("/bet")
def start_bet(body: BetRequest, session: Session = Depends(get_session)):
    """All-in-Runde starten: alle wettberechtigten Accounts auf EIN Ergebnis."""
    if prediction.run_active():
        raise HTTPException(409, "es läuft bereits eine Wett-Runde")
    ch = _channel_or_400(body.channel, session)
    cfg = prediction.get_config(session)
    candidates = [c for c in prediction.eligible_accounts(session, cfg)
                  if c["token"]]
    if not candidates:
        raise HTTPException(400, "kein wettberechtigter Account mit Login")

    # Direkt vor dem Feuern verifizieren: Wette existiert noch, ist offen und
    # das gewählte Ergebnis gehört dazu (das UI kann veraltet sein).
    try:
        data = prediction.fetch_active_prediction(
            candidates[0]["token"], candidates[0]["proxies"], ch,
            device_id=candidates[0].get("device_id"),
            user_agent=candidates[0].get("ua_app"))
    except redeem.RedeemError as e:
        raise HTTPException(400, str(e))
    event = data["event"]
    if event is None:
        raise HTTPException(409, "keine aktive Wette auf diesem Kanal")
    if event["id"] != body.event_id:
        raise HTTPException(409, "die Wette hat inzwischen gewechselt — neu laden")
    if event["status"] != "ACTIVE":
        raise HTTPException(409, f"Wette ist nicht mehr offen ({event['status']})")
    if not any(o["id"] == body.outcome_id for o in event["outcomes"]):
        raise HTTPException(400, "Ergebnis gehört nicht zu dieser Wette")

    try:
        run_id = prediction.start_run(ch, event, body.outcome_id, candidates,
                                      cfg["spacing_min"], cfg["spacing_max"],
                                      cfg["bet_pct_min"], cfg["bet_pct_max"],
                                      cfg["participation_pct"], cfg["counter_pct"])
    except RuntimeError:
        raise HTTPException(409, "es läuft bereits eine Wett-Runde")
    outcome = next(o for o in event["outcomes"] if o["id"] == body.outcome_id)
    return {"run_id": run_id, "accounts": len(candidates),
            "outcome": outcome["title"], "event": event["title"]}


@router.get("/run")
def get_run():
    """Zustand der laufenden bzw. letzten Wett-Runde (null = noch keine)."""
    return prediction.run_status()


@router.post("/cancel")
def cancel():
    return {"cancelled": prediction.cancel_run()}
