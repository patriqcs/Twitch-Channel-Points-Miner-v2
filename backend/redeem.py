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
COOLDOWNS_KEY = "REDEEM_COOLDOWNS"        # JSON {reward_id: sec} per-account cooldown (all accounts)
ALL_DELAY_KEY = "REDEEM_ALL_DELAY"        # default global spacing fallback
MASTER_DELAYS_KEY = "REDEEM_MASTER_DELAYS"  # JSON {reward_id: sec} global spacing between any two fires
COUNTS_KEY = "REDEEM_COUNTS"              # JSON {reward_id: count} how many to schedule for "all accounts"

# ---- cooldown state (WALL-CLOCK, persisted across restarts) ----
# Uses time.time() (not monotonic) and is mirrored into AppSetting so a container
# restart does not silently wipe every redeem cooldown (which would let a reward
# be re-spent immediately after a restart). Mirrors the heist module.
COOLDOWN_STATE_KEY = "REDEEM_COOLDOWN_STATE"   # JSON {"account_id:reward_id": expires_epoch}
GLOBAL_STATE_KEY = "REDEEM_GLOBAL_STATE"       # JSON {reward_id: expires_epoch}

_cooldowns: dict = {}            # (account_id, reward_id) -> wall-clock epoch when available again
_global_free: dict = {}          # reward_id -> wall-clock epoch when reward may fire again globally
_cd_lock = threading.Lock()


def _persist_cooldowns() -> None:
    now = time.time()
    with _cd_lock:
        per_acc = {f"{aid}:{rid}": exp for (aid, rid), exp in _cooldowns.items() if exp > now}
        glob = {rid: exp for rid, exp in _global_free.items() if exp > now}
    from backend.db import engine
    try:
        with Session(engine) as s:
            _set_setting(s, COOLDOWN_STATE_KEY, json.dumps(per_acc))
            _set_setting(s, GLOBAL_STATE_KEY, json.dumps(glob))
            s.commit()
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("redeem").exception("could not persist redeem cooldowns")


def load_cooldowns() -> None:
    """Restore persisted redeem cooldowns on startup (drops expired entries)."""
    import logging
    from backend.db import engine
    try:
        with Session(engine) as s:
            raw_acc = _get_setting(s, COOLDOWN_STATE_KEY)
            raw_glob = _get_setting(s, GLOBAL_STATE_KEY)
    except Exception:  # noqa: BLE001
        logging.getLogger("redeem").exception("could not load redeem cooldowns")
        return
    now = time.time()
    per_acc, glob = {}, {}
    try:
        for key, exp in (json.loads(raw_acc) if raw_acc else {}).items():
            aid_str, _, rid = key.partition(":")
            exp = float(exp)
            if rid and exp > now:
                per_acc[(int(aid_str), rid)] = exp
    except (ValueError, TypeError):
        per_acc = {}
    try:
        for rid, exp in (json.loads(raw_glob) if raw_glob else {}).items():
            exp = float(exp)
            if exp > now:
                glob[rid] = exp
    except (ValueError, TypeError):
        glob = {}
    with _cd_lock:
        _cooldowns.clear(); _cooldowns.update(per_acc)
        _global_free.clear(); _global_free.update(glob)
    if per_acc or glob:
        logging.getLogger("redeem").info(
            "redeem: restored %d account + %d global cooldown(s) after restart",
            len(per_acc), len(glob))

# ---- in-memory registry of running "all accounts" master-redeem runs ----
# run_id -> {stop: Event, reward_id, title, fired, count}. Lets the UI show a
# live progress + cancel a long-running spam before it finishes.
_master_runs: dict = {}
_master_lock = threading.Lock()

# error codes that permanently block an account for a reward (drop from rotation)
PERMANENT_REASONS = {"insufficient_points", "out_of_stock", "disabled", "paused"}


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
    def _json(key):
        raw = _get_setting(session, key, "{}")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}
    return {
        "channel": _get_setting(session, CHANNEL_KEY, "") or "",
        "cooldowns": _json(COOLDOWNS_KEY),
        "master_delays": _json(MASTER_DELAYS_KEY),
        "counts": _json(COUNTS_KEY),
        "all_delay": float(_get_setting(session, ALL_DELAY_KEY, "2") or 2),
    }


def master_delay(session: Session, reward_id: str) -> float:
    cfg = get_config(session)
    try:
        return float(cfg["master_delays"].get(reward_id, cfg["all_delay"]) or 0)
    except (TypeError, ValueError):
        return 0.0


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
        _cooldowns[(account_id, reward_id)] = time.time() + seconds
    _persist_cooldowns()


def cooldown_remaining(account_id: int, reward_id: str) -> float:
    with _cd_lock:
        until = _cooldowns.get((account_id, reward_id), 0)
    return max(0.0, until - time.time())


def set_global_cooldown(reward_id: str, seconds: float) -> None:
    """Global spacing: no account may fire `reward_id` again for `seconds`."""
    if seconds <= 0:
        return
    with _cd_lock:
        _global_free[reward_id] = time.time() + seconds
    _persist_cooldowns()


def global_cooldown_remaining(reward_id: str) -> float:
    with _cd_lock:
        until = _global_free.get(reward_id, 0.0)
    return max(0.0, until - time.time())


def _available_at(account_id: int, reward_id: str) -> float:
    with _cd_lock:
        return _cooldowns.get((account_id, reward_id), 0.0)


def active_cooldowns() -> list:
    """Snapshot of currently-active per-(account,reward) cooldowns (remaining > 0s)."""
    now = time.time()
    with _cd_lock:
        items = list(_cooldowns.items())
    out = []
    for (aid, rid), until in items:
        rem = until - now
        if rem > 0:
            out.append({"account_id": aid, "reward_id": rid, "remaining": round(rem, 1)})
    return out


def active_master_runs() -> list:
    """Snapshot of running 'all accounts' redeems so the UI can show progress."""
    with _master_lock:
        return [
            {"run_id": rid, "reward_id": info["reward_id"], "title": info["title"],
             "fired": info["fired"], "count": info["count"]}
            for rid, info in _master_runs.items()
        ]


def cancel_master_run(run_id: "str | None" = None,
                      reward_id: "str | None" = None) -> int:
    """Signal matching master-redeem runs to stop. With no filter, stops all.

    Returns how many runs were signalled. The threads finish their current
    in-flight call (if any) and then exit before their next fire.
    """
    n = 0
    with _master_lock:
        for rid, info in _master_runs.items():
            if (run_id is not None and rid != run_id):
                continue
            if (reward_id is not None and info["reward_id"] != reward_id):
                continue
            info["stop"].set()
            n += 1
    return n


def schedule_master_redeem(channel_id, reward, candidates, per_account_cd,
                           global_delay, count, log_event, prompt=None) -> str:
    """Background thread: fire `reward` `count` times across `candidates`,
    always taking the account that is free earliest, respecting each account's
    per-account cooldown AND a global spacing between any two fires. Accounts
    that hit a permanent block (no points / disabled / out of stock) drop out.

    candidates: list of (account_id, username, token, proxies)
    log_event: fn(account_id|None, ok, message) -> None

    Returns a run_id; the run is cancellable via cancel_master_run() and its
    progress is visible via active_master_runs().
    """
    run_id = uuid.uuid4().hex
    count = max(1, count)
    stop = threading.Event()
    with _master_lock:
        _master_runs[run_id] = {"stop": stop, "reward_id": reward["id"],
                                "title": reward["title"], "fired": 0, "count": count}

    def _run():
        pool = list(candidates)
        rid = reward["id"]
        fired = 0
        cancelled = False
        try:
            for _ in range(count):
                if stop.is_set():
                    cancelled = True
                    break
                if not pool:
                    break
                # Wait until this fire is actually allowed, RE-CHECKING after each
                # sleep: another master run or a chat-redeem may advance the global
                # spacing while we sleep, so we must not fire on a stale fire_at
                # computed before the wait (that let two runs fire back-to-back).
                while True:
                    if stop.is_set():
                        cancelled = True
                        break
                    # account that is free earliest
                    pool.sort(key=lambda a: _available_at(a[0], rid))
                    aid, uname, token, proxies = pool[0]
                    with _cd_lock:  # locked read of both cooldown maps
                        fire_at = max(_cooldowns.get((aid, rid), 0.0),
                                      _global_free.get(rid, 0.0))
                    wait = fire_at - time.time()
                    if wait <= 0:
                        break
                    if stop.wait(min(wait, 3600)):  # interruptible
                        cancelled = True
                        break
                if cancelled:
                    break
                r = redeem_reward(token, proxies, channel_id, reward, prompt)
                if r["ok"]:
                    set_account_cooldown(aid, rid, per_account_cd)
                    set_global_cooldown(rid, global_delay)
                    fired += 1
                    with _master_lock:
                        if run_id in _master_runs:
                            _master_runs[run_id]["fired"] = fired
                    log_event(aid, True, f'„{reward["title"]}" eingelöst')
                elif r.get("reason") in PERMANENT_REASONS:
                    pool = [a for a in pool if a[0] != aid]  # drop from rotation
                    log_event(aid, False, f'„{reward["title"]}": {r.get("message")} (raus)')
                elif r.get("reason") == "server_cooldown":
                    set_account_cooldown(aid, rid, max(per_account_cd, 5))
                    log_event(aid, False, f'„{reward["title"]}": Server-Cooldown')
                else:
                    set_account_cooldown(aid, rid, max(per_account_cd, 5))
                    log_event(aid, False, f'„{reward["title"]}": {r.get("message")}')
            if cancelled:
                log_event(None, False, f'Master-Einlösen abgebrochen: {fired}/{count}')
            else:
                log_event(None, True, f'Master-Einlösen fertig: {fired}/{count}')
        finally:
            with _master_lock:
                _master_runs.pop(run_id, None)

    t = threading.Thread(target=_run, name="master-redeem", daemon=True)
    t.start()
    return run_id

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

_REASONS = {
    "NOT_ENOUGH_POINTS": "insufficient_points",
    "INSUFFICIENT_POINTS": "insufficient_points",
    "OUT_OF_STOCK": "out_of_stock",
    "MAX_PER_STREAM": "out_of_stock",
    "MAX_PER_USER_PER_STREAM": "out_of_stock",
    "REWARD_COOLDOWN": "server_cooldown",
    "GLOBAL_COOLDOWN": "server_cooldown",
    "DISABLED": "disabled",
    "REWARD_DISABLED": "disabled",
    "PAUSED": "paused",
}

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


_token_cache: dict = {}          # username -> (cookie_mtime, token)
_token_cache_lock = threading.Lock()


def account_auth_token(username: str) -> "str | None":
    """Read the 'auth-token' cookie value saved by the miner login.

    Cached by the cookie file's mtime so the heist/chat-redeem tick loops (which
    call this every few seconds for every account) don't re-open and unpickle the
    file each time. A re-login rewrites the cookie -> new mtime -> cache miss.
    """
    cookie = config.COOKIES_DIR / f"{username}.pkl"
    try:
        mtime = cookie.stat().st_mtime
    except OSError:
        return None
    with _token_cache_lock:
        cached = _token_cache.get(username)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        with open(cookie, "rb") as f:
            cookies = pickle.load(f)
    except Exception:  # noqa: BLE001
        return None
    token = None
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") == "auth-token":
            token = c.get("value")
            break
    with _token_cache_lock:
        _token_cache[username] = (mtime, token)
    return token


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
        return {"ok": False, "reason": "network", "message": str(e)}

    result = data.get("redeemCustomReward")
    if not result:
        return {"ok": False, "reason": "unknown", "message": "no confirmation from Twitch"}
    err = result.get("error")
    if err and err.get("code"):
        code = (err["code"] or "").upper()
        return {"ok": False, "reason": _REASONS.get(code, "unknown"),
                "message": _ERROR_MESSAGES.get(code, f"rejected: {code}")}
    redemption = result.get("redemption")
    if redemption and redemption.get("id"):
        return {"ok": True, "redemptionId": redemption["id"]}
    return {"ok": False, "reason": "unknown", "message": "no confirmation from Twitch"}
