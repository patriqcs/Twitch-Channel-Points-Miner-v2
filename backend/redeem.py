# -*- coding: utf-8 -*-
"""Channel-point redemption via Twitch's private GraphQL API.

Ported from the standalone twitch-channelpoints-redeemer, but wired into this
system: the OAuth token comes from each account's stored login cookie and every
request goes through that account's assigned proxy. Watch-only mining never
spends points; this module is the explicit "spend points on a custom reward"
side, triggered manually from the UI.
"""
import json
import pickle
import threading
import time
import uuid

import requests
from sqlmodel import Session

from backend import config
from backend.models import Account, AppSetting, Proxy
from backend.proxy_util import to_engine_proxy

# ---- persisted redeem settings (AppSetting keys) ----
CHANNEL_KEY = "REDEEM_CHANNEL"
COOLDOWNS_KEY = "REDEEM_COOLDOWNS"     # JSON {reward_id: seconds}, applies to all accounts
ALL_DELAY_KEY = "REDEEM_ALL_DELAY"     # seconds between accounts on "all accounts" redeem

# ---- in-memory per-(account,reward) client cooldown (resets on restart) ----
_cooldowns: dict = {}            # (account_id, reward_id) -> epoch seconds when available again
_cd_lock = threading.Lock()


def _get_setting(session: Session, key: str, default=None):
    s = session.get(AppSetting, key)
    return s.value if s is not None else default


def _set_setting(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key)
    if s is None:
        session.add(AppSetting(key=key, value=value))
    else:
        s.value = value
        session.add(s)


def get_config(session: Session) -> dict:
    raw = _get_setting(session, COOLDOWNS_KEY, "{}")
    try:
        cooldowns = json.loads(raw) if raw else {}
    except ValueError:
        cooldowns = {}
    return {
        "channel": _get_setting(session, CHANNEL_KEY, "") or "",
        "cooldowns": cooldowns,
        "all_delay": float(_get_setting(session, ALL_DELAY_KEY, "2") or 2),
    }


def cooldown_seconds(session: Session, reward_id: str) -> float:
    cfg = get_config(session)
    try:
        return float(cfg["cooldowns"].get(reward_id, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def set_account_cooldown(account_id: int, reward_id: str, seconds: float) -> None:
    if seconds <= 0:
        return
    with _cd_lock:
        _cooldowns[(account_id, reward_id)] = time.monotonic() + seconds


def cooldown_remaining(account_id: int, reward_id: str) -> float:
    with _cd_lock:
        until = _cooldowns.get((account_id, reward_id), 0)
    return max(0.0, until - time.monotonic())

# Same public web client id the twitch.tv site uses; Helix/OAuth app tokens are
# NOT allowed to run these private operations.
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_ENDPOINT = "https://gql.twitch.tv/gql"

_CHANNEL_POINTS_QUERY = """
query ChannelPointsContext($channelLogin: String!) {
  community: user(login: $channelLogin) {
    id
    displayName
    channel {
      id
      self { communityPoints { balance } }
      communityPointsSettings {
        customRewards {
          id title cost isEnabled isPaused isInStock isUserInputRequired prompt
          backgroundColor cooldownExpiresAt
          globalCooldownSetting { isEnabled globalCooldownSeconds }
        }
      }
    }
  }
}"""

_REDEEM_MUTATION = """
mutation RedeemCustomReward($input: RedeemCommunityPointsCustomRewardInput!) {
  redeemCustomReward: redeemCommunityPointsCustomReward(input: $input) {
    redemption { id }
    error { code }
  }
}"""

_ERROR_MESSAGES = {
    "NOT_ENOUGH_POINTS": "Nicht genug Punkte.",
    "INSUFFICIENT_POINTS": "Nicht genug Punkte.",
    "OUT_OF_STOCK": "Ausverkauft oder Limit erreicht.",
    "MAX_PER_STREAM": "Limit pro Stream erreicht.",
    "MAX_PER_USER_PER_STREAM": "Limit pro Nutzer/Stream erreicht.",
    "REWARD_COOLDOWN": "Belohnung ist auf Cooldown.",
    "GLOBAL_COOLDOWN": "Belohnung ist auf Cooldown.",
    "DISABLED": "Belohnung ist deaktiviert.",
    "REWARD_DISABLED": "Belohnung ist deaktiviert.",
    "PAUSED": "Belohnung ist pausiert.",
}


class RedeemError(Exception):
    pass


def account_auth_token(username: str) -> "str | None":
    """Read the 'auth-token' cookie value saved by the miner login."""
    cookie = config.COOKIES_DIR / f"{username}.pkl"
    if not cookie.exists():
        return None
    try:
        with open(cookie, "rb") as f:
            cookies = pickle.load(f)
    except Exception:  # noqa: BLE001
        return None
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") == "auth-token":
            return c.get("value")
    return None


def account_creds(session: Session, account: Account):
    """Return (token, proxies_dict) for an account, raising RedeemError if no token."""
    token = account_auth_token(account.username)
    if not token:
        raise RedeemError("no auth-token - login required")
    proxies = None
    if account.proxy_id is not None:
        ep = to_engine_proxy(session.get(Proxy, account.proxy_id))
        proxies = ep.requests_proxies if ep is not None else None
    return token, proxies


def _gql(token, proxies, operation_name, query, variables, timeout=15):
    headers = {
        "Content-Type": "application/json",
        "Client-Id": TWITCH_WEB_CLIENT_ID,
        "Authorization": f"OAuth {token}",
    }
    body = {"operationName": operation_name, "query": query, "variables": variables}
    try:
        resp = requests.post(GQL_ENDPOINT, json=body, headers=headers,
                             proxies=proxies, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise RedeemError(f"network error: {e}")
    if resp.status_code == 401:
        raise RedeemError("token rejected (401) - login expired/invalid")
    if not resp.ok:
        raise RedeemError(f"GraphQL HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        raise RedeemError("invalid JSON response")
    if payload.get("errors"):
        msgs = "; ".join(
            str(e.get("message", e)) for e in payload["errors"][:3]
        )
        raise RedeemError(f"GraphQL error: {msgs}")
    data = payload.get("data")
    if data is None:
        raise RedeemError("GraphQL response without data")
    return data


def fetch_channel_points(token, proxies, channel_login: str) -> dict:
    """Return {channelId, displayName, balance, rewards:[...]} for a channel."""
    data = _gql(token, proxies, "ChannelPointsContext", _CHANNEL_POINTS_QUERY,
                {"channelLogin": channel_login})
    community = data.get("community")
    if not community:
        raise RedeemError(f'channel "{channel_login}" not found')
    channel = community["channel"]
    settings = channel.get("communityPointsSettings") or {}
    self_ = channel.get("self") or {}
    balance = ((self_.get("communityPoints") or {}).get("balance")) or 0
    rewards = []
    for r in settings.get("customRewards") or []:
        rewards.append({
            "id": r["id"], "title": r["title"], "cost": r["cost"],
            "isEnabled": bool(r.get("isEnabled")),
            "isPaused": bool(r.get("isPaused")),
            "isInStock": bool(r.get("isInStock", True)),
            "isUserInputRequired": bool(r.get("isUserInputRequired")),
            "prompt": r.get("prompt") or "",
        })
    return {
        "channelId": channel["id"],
        "displayName": community.get("displayName") or channel_login,
        "balance": balance,
        "rewards": rewards,
    }


def redeem_reward(token, proxies, channel_id: str, reward: dict,
                  prompt: "str | None" = None) -> dict:
    """Redeem one custom reward. Returns {ok, redemptionId|reason, message?}."""
    input_ = {
        "channelID": channel_id,
        "cost": reward["cost"],
        # For input-required rewards this is the user text; otherwise it must
        # match the reward's own prompt exactly.
        "prompt": prompt if prompt is not None else (reward.get("prompt") or ""),
        "rewardID": reward["id"],
        "title": reward["title"],
        "transactionID": uuid.uuid4().hex,
    }
    try:
        data = _gql(token, proxies, "RedeemCustomReward", _REDEEM_MUTATION,
                    {"input": input_})
    except RedeemError as e:
        return {"ok": False, "message": str(e)}

    result = data.get("redeemCustomReward")
    if not result:
        return {"ok": False, "message": "no confirmation from Twitch"}
    err = result.get("error")
    if err and err.get("code"):
        code = (err["code"] or "").upper()
        return {"ok": False, "message": _ERROR_MESSAGES.get(code, f"rejected: {code}")}
    redemption = result.get("redemption")
    if redemption and redemption.get("id"):
        return {"ok": True, "redemptionId": redemption["id"]}
    return {"ok": False, "message": "no confirmation from Twitch"}
