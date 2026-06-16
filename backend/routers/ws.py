# -*- coding: utf-8 -*-
"""WebSocket live streams for the dashboard.

  /ws/logs/{username}  -> tail of LOGS_DIR/<username>.log (last lines + live)
  /ws/status           -> all account statuses, pushed every 2s
  /ws/events           -> new Event rows (points/status/...) as they arrive
"""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, desc, select

from backend import config
from backend.db import engine
from backend.models import Account, Event

router = APIRouter(tags=["ws"])

TAIL_LINES = 200


def _event_dict(e: Event) -> dict:
    return {
        "id": e.id, "account_id": e.account_id, "event_type": e.type,
        "streamer": e.streamer, "points": e.points, "balance": e.balance,
        "reason": e.reason, "message": e.message,
        "ts": e.ts.isoformat() if e.ts else None,
    }


@router.websocket("/ws/logs/{username}")
async def ws_logs(ws: WebSocket, username: str):
    await ws.accept()
    path = config.LOGS_DIR / f"{username}.log"
    offset = 0
    try:
        if path.exists():
            raw = path.read_bytes()
            tail = raw.decode("utf-8", "ignore").splitlines()[-TAIL_LINES:]
            for line in tail:
                await ws.send_text(line)
            offset = len(raw)

        while True:
            await asyncio.sleep(0.5)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size < offset:  # file rotated/truncated
                offset = 0
            if size > offset:
                with open(path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                for line in chunk.decode("utf-8", "ignore").splitlines():
                    await ws.send_text(line)
    except WebSocketDisconnect:
        return
    except Exception:
        return


@router.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            with Session(engine) as s:
                accounts = s.exec(select(Account)).all()
                payload = [
                    {
                        "id": a.id, "username": a.username, "status": a.status,
                        "enabled": a.enabled, "proxy_id": a.proxy_id,
                        "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
                    }
                    for a in accounts
                ]
            await ws.send_json({"type": "status", "accounts": payload})
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return
    except Exception:
        return


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    with Session(engine) as s:
        last = s.exec(select(Event.id).order_by(desc(Event.id))).first() or 0
    try:
        while True:
            with Session(engine) as s:
                rows = s.exec(
                    select(Event).where(Event.id > last).order_by(Event.id)
                ).all()
                payloads = [_event_dict(e) for e in rows]
                if rows:
                    last = rows[-1].id
            for p in payloads:
                await ws.send_json({"type": "event", **p})
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return
    except Exception:
        return
