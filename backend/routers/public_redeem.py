# -*- coding: utf-8 -*-
"""Endpoints consumed by the PUBLIC redeem website container (webredeem/).

Protected by a shared token (X-Redeem-Token header) so the manager API can stay
off the public internet: only the small website container knows the token and
proxies exactly these two calls. Never expose anything here beyond what an
anonymous visitor may see (no usernames, no per-account balances).

  GET  /api/public-redeem/catalog  -> branding + total points + item states
  POST /api/public-redeem/trigger  -> fire one item's redemption
"""
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from backend import config
from backend.web_redeem_manager import web_redeem_manager

router = APIRouter(prefix="/api/public-redeem", tags=["public-redeem"])


def require_token(x_redeem_token: str = Header(default="")):
    if not secrets.compare_digest(x_redeem_token, config.get_webredeem_token()):
        raise HTTPException(status_code=401, detail="bad redeem token")


@router.get("/catalog", dependencies=[Depends(require_token)])
def catalog():
    return web_redeem_manager.catalog()


class TriggerIn(BaseModel):
    reward_id: str
    visitor: str | None = None


@router.post("/trigger", dependencies=[Depends(require_token)])
def trigger(body: TriggerIn):
    return web_redeem_manager.trigger(body.reward_id, body.visitor or "")
