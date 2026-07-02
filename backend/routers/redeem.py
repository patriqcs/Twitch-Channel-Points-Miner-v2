# -*- coding: utf-8 -*-
"""Manual channel-point redemption endpoints (per account, via its proxy).

Supports per-reward client cooldowns (shared across accounts) and an
"all accounts" redeem that rotates over the accounts with an internal delay —
so a reward with a per-account server cooldown can still be fired frequently by
spreading it across accounts.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend import redeem as redeem_mod
from backend.db import engine, get_session
from backend.models import Account, Event

router = APIRouter(prefix="/api/redeem", tags=["redeem"])
logger = logging.getLogger("redeem")


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
    counts: dict[str, int] | None = None
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
    if body.counts is not None:
        clean = {k: max(1, int(v)) for k, v in body.counts.items()}
        redeem_mod._set_setting(session, redeem_mod.COUNTS_KEY, json.dumps(clean))
    if body.all_delay is not None:
        redeem_mod._set_setting(session, redeem_mod.ALL_DELAY_KEY, str(max(0.0, body.all_delay)))
    session.commit()
    return redeem_mod.get_config(session)


@router.get("/cooldowns")
def cooldowns():
    """Currently-active per-(account,reward) cooldowns so the UI can show readiness."""
    return redeem_mod.active_cooldowns()


@router.get("/all/status")
def all_status():
    """Running 'all accounts' redeems with live fired/count progress."""
    return redeem_mod.active_master_runs()


class CancelRequest(BaseModel):
    run_id: str | None = None
    reward_id: str | None = None


@router.post("/all/cancel")
def cancel_all(body: CancelRequest):
    """Stop running 'all accounts' redeems (by reward_id, run_id, or all)."""
    n = redeem_mod.cancel_master_run(run_id=body.run_id, reward_id=body.reward_id)
    return {"cancelled": n}


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
    count: int | None = None      # total redemptions to distribute (default: 1)
    global_delay: float | None = None  # override the per-reward global spacing
    prompt: str | None = None


def _log_event(account_id, ok, message):
    """Background-safe event logger. Must NEVER raise: it runs inside the
    master-redeem worker thread, which has no except around it, so a DB hiccup
    here would abort the whole run mid-way and silently skip the rest."""
    if account_id is None:
        # Run-level summary (finished/cancelled): Event.account_id is NOT NULL,
        # so there is no row to attach it to — record it in the log instead.
        logger.info("master-redeem: %s", message)
        return
    try:
        with Session(engine) as s:
            s.add(Event(account_id=account_id, type="redeem", message=message))
            s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not log redeem event for account %s", account_id)


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

    # Empty/unset count -> a single redemption (NOT one-per-account); the user
    # must type a number to spam more across the accounts.
    count = body.count if body.count and body.count > 0 else 1
    count = min(count, 500)
    run_id = redeem_mod.schedule_master_redeem(
        channel_id, reward, candidates, cd_secs, gdelay, count, _log_event,
        prompt=body.prompt,
    )
    return {"reward": reward["title"], "accounts": len(candidates),
            "scheduled": count, "global_delay": gdelay, "run_id": run_id}


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
        # Respect this account's own cooldown: firing into an active cooldown only
        # wastes calls and can provoke a longer server-side cooldown.
        remaining = redeem_mod.cooldown_remaining(acc.id, reward["id"])
        if remaining > 0:
            results.append({"ok": False, "reason": "cooldown",
                            "message": f"Cooldown aktiv ({remaining:.0f}s)"})
            break
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
