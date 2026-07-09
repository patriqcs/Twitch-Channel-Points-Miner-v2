# -*- coding: utf-8 -*-
"""Long-lived coordinator that drives the website redeemer.

One background thread (started in the FastAPI lifespan, like the chat-redeem
coordinator) that, while the module is enabled, keeps the ``web_redeemer``
accounts' point balances and the channel's reward catalogue cached — so a
visitor's click can be served without a per-request GraphQL round trip.

Unlike the chat version there is no IRC observer and no queueing: ``trigger()``
is called synchronously from the public API, fires immediately with the
richest free account, and returns the outcome (or a precise "try again in Ns"
when a cooldown blocks it — the website renders that as a countdown).

Cooldown bookkeeping is shared with the manual "Einlösen" page and the chat
redeemer through ``backend/redeem.py``, so the website can never double-fire a
reward that chat just redeemed.

When the (toggleable) chat announcement is on, a configured announcer account
keeps a persistent chat connection open (reusing ``heist.HeistIRC`` like the
chat-redeem observer) and posts "user X redeemed Y" after every successful
website redemption. Announce failures never block the redemption itself.
"""
import logging
import math
import threading
import time

from sqlmodel import Session

from backend import chat_redeem, heist, redeem, web_redeem
from backend.db import engine
from backend.models import Event

logger = logging.getLogger("backend.web_redeem_manager")


class WebRedeemManager(threading.Thread):
    def __init__(self, poll_interval: float = 3.0, balance_refresh: float = 45.0):
        super().__init__(name="web-redeem-manager", daemon=True)
        self.poll_interval = poll_interval
        self.balance_refresh = balance_refresh
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # config snapshot (refreshed each tick)
        self._cfg: dict = {"enabled": False, "channel": "", "items": []}
        self._items: dict = {}             # reward_id -> item entry

        # announcer (chat) connection — only while announce is enabled
        self._announcer: "heist.HeistIRC | None" = None
        self._announcer_thread: "threading.Thread | None" = None
        self._announcer_username: str = ""
        self._announcer_channel: str = ""

        # runtime caches
        self._item_cd: dict = {}           # reward_id -> monotonic time free again
        self._inflight: set = set()        # reward_ids with a live redemption
        self._balances: dict = {}          # account_id -> cached points balance
        self._spent_at: dict = {}          # account_id -> monotonic of last local spend
        self._reward_cache: dict = {}      # reward_id -> reward dict (from catalogue)
        self._channel_id: "str | None" = None
        self._display_name: "str | None" = None
        self._last_balance_refresh = 0.0
        self._refreshing = False
        self._last_triggers: list = []     # recent fires (for the status UI)
        self._reason = "aus"               # human-readable current state (for the UI)

    # ------------------------------------------------------------------ lifecycle
    def stop(self):
        self._stop.set()
        self._teardown_announcer()

    def run(self):
        logger.info("Web-redeem manager started.")
        while not self._stop.wait(self.poll_interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("web-redeem manager tick failed")
        logger.info("Web-redeem manager stopped.")

    # ------------------------------------------------------------------ main tick
    def _tick(self):
        with Session(engine) as session:
            cfg = web_redeem.get_config(session)
        was_enabled = self._cfg.get("enabled", False)
        with self._lock:
            self._cfg = cfg
            self._items = {i["reward_id"]: i for i in cfg["items"]}

        if not cfg["enabled"]:
            self._set_reason("aus")
            self._teardown_announcer()
            return

        missing = []
        if not cfg["channel"]:
            missing.append("Channel")
        if not cfg["items"]:
            missing.append("mind. 1 Belohnung")
        with Session(engine) as session:
            redeemers = web_redeem.load_redeemer_accounts(session)
        if not any(r["logged_in"] for r in redeemers):
            missing.append("mind. 1 eingeloggter Web-Einlöser-Account")
        if missing:
            self._set_reason("Es fehlt: " + ", ".join(missing))
            return

        if not was_enabled:
            with self._lock:
                self._last_balance_refresh = 0.0   # just enabled -> refresh now
        self._maybe_refresh_balances(cfg)
        self._ensure_announcer(cfg)
        with self._lock:
            ready = bool(self._reward_cache) and self._channel_id is not None
        self._set_reason("aktiv" if ready else "lade Reward-Katalog…")

    # ------------------------------------------------------------------ announcer
    def _ensure_announcer(self, cfg):
        """Keep the chat announcer link up while announce is enabled."""
        if not cfg.get("announce") or not cfg.get("announcer") or not cfg["channel"]:
            self._teardown_announcer()
            return
        alive = (self._announcer_thread is not None
                 and self._announcer_thread.is_alive())
        same = (self._announcer_username == cfg["announcer"]
                and self._announcer_channel == cfg["channel"])
        if alive and same:
            return
        self._teardown_announcer()
        with Session(engine) as session:
            # same creds lookup the chat module uses (case-insensitive username)
            rec = chat_redeem.announcer_creds(session, cfg["announcer"])
        if rec is None or not rec["logged_in"]:
            logger.warning("web-redeem announcer %r missing or not logged in",
                           cfg["announcer"])
            return
        announcer = heist.HeistIRC(rec["username"], rec["token"], cfg["channel"],
                                   rec["proxy"])
        t = threading.Thread(target=announcer.start,
                             name=f"web-redeem-ann-{rec['username']}", daemon=True)
        t.start()
        self._announcer = announcer
        self._announcer_thread = t
        self._announcer_username = cfg["announcer"]
        self._announcer_channel = cfg["channel"]
        logger.info("web-redeem announcer connecting as %s in #%s",
                    rec["username"], cfg["channel"])

    def _teardown_announcer(self):
        with self._lock:
            ann, t = self._announcer, self._announcer_thread
            self._announcer = None
            self._announcer_thread = None
            self._announcer_username = ""
            self._announcer_channel = ""
        if ann is not None:
            try:
                ann.die()
            except Exception:  # noqa: BLE001
                pass
        if t is not None:
            t.join(timeout=5)

    def _announce_redeem(self, user: str, reward: dict):
        """Post "user X redeemed Y" in chat (fire-and-forget, never blocks)."""
        with self._lock:
            cfg = self._cfg
            ann = self._announcer
        if not cfg.get("announce") or ann is None or not ann.joined.is_set():
            return
        text = web_redeem.render_announce_text(
            cfg.get("announce_text", ""), user, reward["title"], reward.get("cost"))

        def _send():
            try:
                ann.send(text)
            except Exception:  # noqa: BLE001
                logger.exception("web-redeem announce failed")

        threading.Thread(target=_send, name="web-redeem-announce",
                         daemon=True).start()

    def _set_reason(self, reason: str):
        with self._lock:
            self._reason = reason

    # ------------------------------------------------------------------ trigger
    def trigger(self, reward_id: str, visitor: str = "") -> dict:
        """Fire the redemption mapped to `reward_id` for a website visitor.

        Synchronous: called from the public API request thread. Returns
        {ok, message, retry_in?} — retry_in tells the website how long to show
        a countdown before the button unlocks again.
        """
        visitor = _clean_visitor(visitor)
        now = time.monotonic()
        with self._lock:
            if not self._cfg.get("enabled"):
                return {"ok": False, "reason": "disabled",
                        "message": "Web-Einlösen ist gerade ausgeschaltet."}
            entry = self._items.get(reward_id)
            if entry is None or not entry["enabled"]:
                return {"ok": False, "reason": "unknown_item",
                        "message": "Diese Belohnung ist nicht (mehr) verfügbar."}
            reward = self._reward_cache.get(reward_id)
            channel_id = self._channel_id
            if reward is None or channel_id is None:
                self._last_balance_refresh = 0.0   # pull a fresh catalogue soon
                return {"ok": False, "reason": "not_ready", "retry_in": 5,
                        "message": "Der Reward-Katalog lädt noch — gleich nochmal versuchen."}
            blocked = _reward_blocked(reward)
            if blocked:
                return {"ok": False, "reason": "unavailable", "message": blocked}
            rem = max(self._item_cd.get(reward_id, 0.0) - now,
                      redeem.global_cooldown_remaining(reward_id))
            if rem > 0:
                return {"ok": False, "reason": "cooldown",
                        "retry_in": math.ceil(rem),
                        "message": f"Noch {math.ceil(rem)}s Cooldown."}
            if reward_id in self._inflight:
                return {"ok": False, "reason": "busy", "retry_in": 3,
                        "message": "Wird gerade eingelöst — kurz warten."}
            self._inflight.add(reward_id)
        try:
            return self._fire(entry, reward, channel_id, visitor)
        finally:
            with self._lock:
                self._inflight.discard(reward_id)

    def _fire(self, entry: dict, reward: dict, channel_id: str, visitor: str) -> dict:
        reward_id = reward["id"]
        cost = reward.get("cost", 0)
        with Session(engine) as session:
            redeemers = web_redeem.load_redeemer_accounts(session)
            per_account_cd = redeem.cooldown_seconds(session, reward_id)
            global_delay = redeem.master_delay(session, reward_id)

        # accounts with enough points, preferring the richest FREE one
        funded = []
        for r in redeemers:
            if not r["logged_in"]:
                continue
            with self._lock:
                bal = self._balances.get(r["id"])
            if bal is None or bal < cost:
                continue
            funded.append((bal, r, redeem.cooldown_remaining(r["id"], reward_id)))
        if not funded:
            self._note_trigger(entry, visitor, ok=False,
                               message="Kein Account mit genug Punkten")
            return {"ok": False, "reason": "no_points",
                    "message": "Gerade nicht genug Punkte auf den Konten — später nochmal!"}
        free = [(bal, r) for bal, r, rem in funded if rem <= 0]
        if not free:
            wait = math.ceil(min(rem for _, _, rem in funded))
            return {"ok": False, "reason": "cooldown", "retry_in": wait,
                    "message": f"Alle Konten im Cooldown — in ~{wait}s wieder frei."}

        free.sort(key=lambda x: x[0], reverse=True)
        bal, acc = free[0]
        proxies = acc["proxy"].requests_proxies if acc["proxy"] else None
        res = redeem.redeem_reward(acc["token"], proxies, channel_id, reward,
                                   extra_headers=redeem.fp_for_username(acc["username"]))
        who = visitor or "Webseite"
        if res["ok"]:
            redeem.set_account_cooldown(acc["id"], reward_id,
                                        max(per_account_cd, web_redeem.ROTATE_COOLDOWN))
            redeem.set_global_cooldown(reward_id, global_delay)
            now = time.monotonic()
            with self._lock:
                cur = self._balances.get(acc["id"], bal)
                self._balances[acc["id"]] = max(0, cur - cost)
                self._spent_at[acc["id"]] = now
                self._item_cd[reward_id] = now + entry["cooldown"]
            self._record_event(
                acc["id"],
                f'Webseite ({who}) → „{reward["title"]}" eingelöst ({acc["username"]})')
            self._note_trigger(entry, visitor, ok=True, message=acc["username"])
            self._announce_redeem(who, reward)
            return {"ok": True, "retry_in": math.ceil(entry["cooldown"]),
                    "message": f'„{reward["title"]}" wurde eingelöst!'}

        # ---- failure: keep the rotation honest so we don't fail-loop on one acc
        reason = res.get("reason")
        if reason == "insufficient_points":
            with self._lock:
                self._balances[acc["id"]] = 0  # stale-high -> rotate to next-richest
        elif reason == "server_cooldown":
            redeem.set_account_cooldown(acc["id"], reward_id, max(per_account_cd, 30.0))
        elif reason in redeem.PERMANENT_REASONS:
            redeem.set_global_cooldown(reward_id, max(global_delay, 60.0))
        self._record_event(
            acc["id"],
            f'Webseite ({who}) → „{reward["title"]}" fehlgeschlagen: {res.get("message")}')
        self._note_trigger(entry, visitor, ok=False,
                           message=f'{acc["username"]}: {res.get("message")}')
        return {"ok": False, "reason": reason or "unknown", "retry_in": 5,
                "message": "Hat nicht geklappt — bitte gleich nochmal versuchen."}

    # ------------------------------------------------------------------ catalog
    def catalog(self) -> dict:
        """Public snapshot for the website: branding, points and item states."""
        with Session(engine) as session:
            cfg = web_redeem.get_config(session)
        now = time.monotonic()
        with self._lock:
            balances = dict(self._balances)
            reward_cache = dict(self._reward_cache)
            item_cd = dict(self._item_cd)
            display_name = self._display_name
        channel = cfg["channel"]
        total = sum(b for b in balances.values() if b)
        richest = max((b for b in balances.values() if b), default=0)
        items = []
        for it in cfg["items"]:
            if not it["enabled"]:
                continue
            rid = it["reward_id"]
            reward = reward_cache.get(rid)
            cost = reward.get("cost", 0) if reward else None
            retry_in = max(
                math.ceil(max(0.0, item_cd.get(rid, 0.0) - now)),
                math.ceil(redeem.global_cooldown_remaining(rid)),
            )
            blocked = _reward_blocked(reward) if reward else "Katalog lädt noch…"
            affordable = cost is not None and richest >= cost
            items.append({
                "reward_id": rid,
                "label": it["label"] or it["reward_title"]
                or (reward.get("title") if reward else rid),
                "title": reward.get("title") if reward else it["reward_title"],
                "description": it["description"],
                "cost": cost,
                "retry_in": retry_in,
                "cooldown": it["cooldown"],
                "available": bool(reward) and not blocked and affordable and retry_in == 0,
                "blocked_reason": blocked
                or (None if affordable else "Nicht genug Punkte"),
            })
        return {
            "enabled": cfg["enabled"],
            "title": cfg["title"],
            "tagline": cfg["tagline"],
            "offline_text": cfg["offline_text"],
            "channel": channel,
            "channel_display": display_name or channel,
            "twitch_url": f"https://www.twitch.tv/{channel}" if channel else None,
            "points_total": total,
            "items": items if cfg["enabled"] else [],
        }

    # ------------------------------------------------------------------ balances
    def _maybe_refresh_balances(self, cfg):
        now = time.monotonic()
        with self._lock:
            if self._refreshing:
                return
            if self._reward_cache and (now - self._last_balance_refresh) < self.balance_refresh:
                return
            self._refreshing = True
            self._last_balance_refresh = now
        try:
            threading.Thread(target=self._refresh_balances, args=(cfg,), daemon=True,
                             name="web-redeem-balances").start()
        except Exception:  # noqa: BLE001 — never leave _refreshing stuck True
            with self._lock:
                self._refreshing = False
            logger.exception("web-redeem: could not start balance refresh")

    def _refresh_balances(self, cfg):
        try:
            with Session(engine) as session:
                redeemers = web_redeem.load_redeemer_accounts(session)
            redeemer_ids = {r["id"] for r in redeemers if r["logged_in"]}
            balances, catalogue, channel_id, display_name = {}, None, None, None
            for r in redeemers:
                if not r["logged_in"]:
                    continue
                proxies = r["proxy"].requests_proxies if r["proxy"] else None
                try:
                    state = redeem.fetch_channel_points(
                        r["token"], proxies, cfg["channel"],
                        extra_headers=redeem.fp_for_username(r["username"]))
                except redeem.RedeemError as e:
                    logger.debug("web-redeem balance fetch failed for %s: %s",
                                 r["username"], e)
                    continue
                balances[r["id"]] = state["balance"]
                if catalogue is None:
                    channel_id = state["channelId"]
                    display_name = state["displayName"]
                    catalogue = {rw["id"]: rw for rw in state["rewards"]}
            now = time.monotonic()
            with self._lock:
                # Keep the previously cached balance for a current redeemer whose
                # fetch FAILED this cycle (a transient proxy blip must not evict a
                # funded account); scope to the current redeemer set.
                merged = {aid: bal for aid, bal in self._balances.items()
                          if aid in redeemer_ids}
                for aid, fetched in balances.items():
                    cached = self._balances.get(aid)
                    spent = self._spent_at.get(aid, 0.0)
                    # Don't let a stale server balance UNDO a very recent local
                    # spend (Twitch's balance often hasn't reflected it yet).
                    if cached is not None and (now - spent) < 90 and fetched > cached:
                        merged[aid] = cached
                    else:
                        merged[aid] = fetched
                self._balances = merged
                if catalogue is not None:
                    self._reward_cache = catalogue
                    self._channel_id = channel_id
                    self._display_name = display_name
        finally:
            with self._lock:
                self._refreshing = False

    # ------------------------------------------------------------------ events / status
    def _note_trigger(self, entry, visitor, ok, message):
        with self._lock:
            self._last_triggers.insert(0, {
                "label": entry["label"] or entry["reward_title"] or entry["reward_id"],
                "visitor": visitor or "anonym", "ok": ok,
                "message": message, "_at": time.monotonic(),
            })
            del self._last_triggers[20:]

    def _record_event(self, account_id, message: str):
        if account_id is None:
            return  # Event.account_id is NOT NULL
        try:
            with Session(engine) as s:
                s.add(Event(account_id=account_id, type="redeem", message=message))
                s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("could not record web-redeem event")

    def status(self) -> dict:
        now = time.monotonic()
        with self._lock:
            ann = self._announcer
            triggers = [
                {"label": t["label"], "visitor": t["visitor"], "ok": t["ok"],
                 "message": t["message"], "age": round(now - t["_at"], 1)}
                for t in self._last_triggers
            ]
            return {
                "enabled": self._cfg.get("enabled", False),
                "reason": self._reason,
                "channel": self._cfg.get("channel") or None,
                "channel_display": self._display_name,
                "catalog_loaded": bool(self._reward_cache),
                "announce": self._cfg.get("announce", False),
                "announcer": self._announcer_username or None,
                "announcer_connected": ann is not None and ann.joined.is_set(),
                "balances": dict(self._balances),
                "last_triggers": triggers,
            }


def _clean_visitor(raw: str) -> str:
    """Sanitize the optional visitor name shown in the manager's trigger log."""
    cleaned = "".join(ch for ch in (raw or "") if ch.isprintable()).strip()
    return cleaned[:24]


def _reward_blocked(reward: "dict | None") -> "str | None":
    """Why a catalogue reward cannot be redeemed right now (None = fine)."""
    if reward is None:
        return "Belohnung nicht im Katalog"
    if reward.get("isUserInputRequired"):
        return "Belohnung erfordert Texteingabe — nicht unterstützt"
    if not reward.get("isEnabled"):
        return "Belohnung ist deaktiviert"
    if reward.get("isPaused"):
        return "Belohnung ist pausiert"
    if not reward.get("isInStock", True):
        return "Belohnung ist ausverkauft"
    return None


# Module-level singleton (mirrors chat_redeem_manager) so routers and the app
# entrypoint share one coordinator instance.
web_redeem_manager = WebRedeemManager()
