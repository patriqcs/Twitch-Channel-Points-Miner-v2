# -*- coding: utf-8 -*-
"""Manual channel-point redemption endpoints (per account, via its proxy).

Supports per-reward client cooldowns (shared across accounts) and an
"all accounts" redeem that rotates over the accounts with an internal delay —
so a reward with a per-account server cooldown can still be fired frequently by
spreading it across accounts.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import redeem as redeem_mod
from backend.db import engine, get_session
from backend.models import Account, Event

router = APIRouter(prefix="/api/redeem", tags=["redeem"])


def _get_account(session: Session, account_id: int) -> Account:
    acc = session.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    return acc


# ---- persisted config (channel, per-reward cooldowns, all-accounts delay) ----
class RedeemConfig(BaseModel):
    channel: str | None = None
    cooldowns: dict[str, float] | None = None
    master_delays: dict[str, float] | None = None
    all_delay: float | None = None


@router.get("/config")
def get_config(session: Session = Depends(get_session)):
    return redeem_mod.get_config(session)


@router.put("/config")
def put_config(body: RedeemConfig, session: Session = Depends(get_session)):
    import json
    if body.channel is not None:
        redeem_mod._set_setting(session, redeem_mod.CHANNEL_KEY, body.channel.strip().lower())
    if body.cooldowns is not None:
        clean = {k: max(0.0, float(v)) for k, v in body.cooldowns.items()}
        redeem_mod._set_setting(session, redeem_mod.COOLDOWNS_KEY, json.dumps(clean))
    if body.master_delays is not None:
        clean = {k: max(0.0, float(v)) for k, v in body.master_delays.items()}
        redeem_mod._set_setting(session, redeem_mod.MASTER_DELAYS_KEY, json.dumps(clean))
    if body.all_delay is not None:
        redeem_mod._set_setting(session, redeem_mod.ALL_DELAY_KEY, str(max(0.0, body.all_delay)))
    session.commit()
    return redeem_mod.get_config(session)


@router.get("/cooldowns")
def cooldowns():
    """Currently-active per-(account,reward) cooldowns so the UI can show readiness."""
    return redeem_mod.active_cooldowns()


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


class AllRedeemRequest(BaseModel):
    channel: str
    reward_id: str
    count: int | None = None      # total redemptions to distribute (default: one per account)
    global_delay: float | None = None  # override the per-reward global spacing
    prompt: str | None = None


def _log_event(account_id, ok, message):
    """Background-safe event logger (own session)."""
    if account_id is None:
        return
    with Session(engine) as s:
        s.add(Event(account_id=account_id, type="redeem", message=message))
        s.commit()


@router.post("/all")
def redeem_all(body: AllRedeemRequest, session: Session = Depends(get_session)):
    """Schedule `count` redemptions of one reward across all enabled accounts.

    Picks the earliest-free account each time, respecting each account's
    per-account cooldown AND a global spacing between fires. Runs in the
    background and returns immediately.
    """
    channel = body.channel.strip().lower()
    cd_secs = redeem_mod.cooldown_seconds(session, body.reward_id)
    gdelay = body.global_delay if body.global_delay is not None \
        else redeem_mod.master_delay(session, body.reward_id)

    accounts = session.exec(
        select(Account).where(Account.enabled == True)  # noqa: E712
    ).all()

    # Scout the reward once (same channel for everyone) + gather usable creds.
    reward = None
    channel_id = None
    candidates = []
    for acc in accounts:
        try:
            token, proxies = redeem_mod.account_creds(session, acc)
        except redeem_mod.RedeemError:
            continue
        if reward is None:
            try:
                state = redeem_mod.fetch_channel_points(token, proxies, channel)
            except redeem_mod.RedeemError:
                continue
            reward = next((r for r in state["rewards"] if r["id"] == body.reward_id), None)
            channel_id = state["channelId"]
            if reward is None:
                raise HTTPException(404, "reward not found on this channel")
        candidates.append((acc.id, acc.username, token, proxies))

    if reward is None or not candidates:
        raise HTTPException(400, "no usable account login")

    count = body.count if body.count and body.count > 0 else len(candidates)
    count = min(count, 500)
    redeem_mod.schedule_master_redeem(
        channel_id, reward, candidates, cd_secs, gdelay, count, _log_event
    )
    return {"reward": reward["title"], "accounts": len(candidates),
            "scheduled": count, "global_delay": gdelay}


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
    count = max(1, min(body.count, 50))
    cd_secs = redeem_mod.cooldown_seconds(session, body.reward_id)
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
        r = redeem_mod.redeem_reward(token, proxies, state["channelId"], reward, body.prompt)
        results.append(r)
        if r["ok"]:
            redeem_mod.set_account_cooldown(acc.id, reward["id"], cd_secs)
        else:
            break
    return {
        "reward": reward["title"],
        "attempted": len(results),
        "succeeded": sum(1 for r in results if r["ok"]),
        "results": results,
    }
