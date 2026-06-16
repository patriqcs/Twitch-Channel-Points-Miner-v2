# -*- coding: utf-8 -*-
"""Manual channel-point redemption endpoints (per account, via its proxy)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from backend import redeem as redeem_mod
from backend.db import get_session
from backend.models import Account

router = APIRouter(prefix="/api/redeem", tags=["redeem"])


def _get_account(session: Session, account_id: int) -> Account:
    acc = session.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    return acc


@router.get("/{account_id}/channel-points")
def channel_points(account_id: int, channel: str,
                   session: Session = Depends(get_session)):
    """List a channel's custom rewards + this account's balance (via its proxy)."""
    acc = _get_account(session, account_id)
    try:
        token, proxies = redeem_mod.account_creds(session, acc)
        return redeem_mod.fetch_channel_points(token, proxies, channel.strip().lower())
    except redeem_mod.RedeemError as e:
        raise HTTPException(400, str(e))


class RedeemRequest(BaseModel):
    channel: str
    reward_id: str
    count: int = 1
    prompt: str | None = None


@router.post("/{account_id}")
def redeem(account_id: int, body: RedeemRequest,
           session: Session = Depends(get_session)):
    """Redeem a reward `count` times for one account, through its proxy."""
    acc = _get_account(session, account_id)
    count = max(1, min(body.count, 50))  # sane bound
    try:
        token, proxies = redeem_mod.account_creds(session, acc)
        state = redeem_mod.fetch_channel_points(token, proxies, body.channel.strip().lower())
    except redeem_mod.RedeemError as e:
        raise HTTPException(400, str(e))

    reward = next((r for r in state["rewards"] if r["id"] == body.reward_id), None)
    if reward is None:
        raise HTTPException(404, "reward not found on this channel")

    results = []
    for _ in range(count):
        results.append(
            redeem_mod.redeem_reward(token, proxies, state["channelId"], reward, body.prompt)
        )
        if not results[-1]["ok"]:
            break  # stop on the first failure (e.g. out of points/cooldown)
    return {
        "reward": reward["title"],
        "attempted": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "results": results,
    }
