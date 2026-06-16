# -*- coding: utf-8 -*-
"""REST history endpoints for charts (initial load before WS takes over)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, asc, desc, select

from backend.db import get_session
from backend.models import Account, Event

router = APIRouter(prefix="/api/accounts", tags=["metrics"])


@router.get("/{account_id}/events")
def account_events(account_id: int, limit: int = 200,
                   session: Session = Depends(get_session)):
    if session.get(Account, account_id) is None:
        raise HTTPException(404, "account not found")
    rows = session.exec(
        select(Event).where(Event.account_id == account_id)
        .order_by(desc(Event.id)).limit(min(limit, 1000))
    ).all()
    rows.reverse()  # chronological
    return [
        {"id": e.id, "type": e.type, "streamer": e.streamer, "points": e.points,
         "balance": e.balance, "reason": e.reason, "message": e.message,
         "ts": e.ts.isoformat() if e.ts else None}
        for e in rows
    ]


@router.get("/{account_id}/points")
def account_points(account_id: int, limit: int = 500,
                   session: Session = Depends(get_session)):
    """Points-balance time series (from points_snapshot events)."""
    if session.get(Account, account_id) is None:
        raise HTTPException(404, "account not found")
    rows = session.exec(
        select(Event).where(Event.account_id == account_id)
        .where(Event.type == "points_snapshot")
        .order_by(asc(Event.ts)).limit(min(limit, 5000))
    ).all()
    return [
        {"ts": e.ts.isoformat() if e.ts else None, "balance": e.balance}
        for e in rows
    ]
