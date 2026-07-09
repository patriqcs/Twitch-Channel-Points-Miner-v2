# -*- coding: utf-8 -*-
"""Long-lived coordinator that drives the heist module.

One background thread (started in the FastAPI lifespan, like the proxy/watch
monitors) that, while the target streamer is live and the module is enabled:

  * keeps a persistent observer/joiner IRC connection open (the first joiner
    account) which fires `!join` the instant the bot announces an open heist;
  * rotates the opener accounts to fire `!heist`, respecting each account's
    per-account cooldown (the bot's own start limit) and a randomized spacing
    between two openers;
  * never starts a new heist while one is still active.

State (cooldowns, online flag, active heist) is in-memory and resets on restart.
"""
import logging
import random
import re
import threading
import time

from sqlmodel import Session

from backend import config, heist
from backend.db import engine
from backend.models import Event

logger = logging.getLogger("backend.heist_manager")


class HeistManager(threading.Thread):
    def __init__(self, poll_interval: float = 5.0,
                 online_recheck: float = 60.0):
        super().__init__(name="heist-manager", daemon=True)
        self.poll_interval = poll_interval
        self.online_recheck = online_recheck
        self._stop = threading.Event()

        self._lock = threading.Lock()
        self._cfg: dict | None = None
        self._trigger_re: "re.Pattern | None" = None
        self._end_re: "re.Pattern | None" = None
        self._reject_re: "re.Pattern | None" = None
        # last raw pattern strings compiled, so we only recompile on change.
        self._last_patterns: tuple = (None, None, None)

        # observer / joiner state
        self._observer: "heist.HeistIRC | None" = None
        self._observer_thread: "threading.Thread | None" = None
        self._observer_account_id: "int | None" = None
        self._observer_username: str = ""
        self._observer_channel: str = ""
        self._observer_proxy_key: "str | None" = None
        self._extra_joiners: list = []

        # runtime state
        self._online: "bool | None" = None
        self._last_online_check = 0.0
        self._heist_in_progress = False  # set on open announcement, cleared on end
        self._heist_since = 0.0
        self._next_open_at = 0.0
        self._last_event_msg = ""

        # Confirmation tracking for the opener we last fired !heist with. The
        # bot's open announcement carries no starter name, so we credit an
        # opener (and only then start its long start-cooldown) when a FRESH open
        # appears within OPEN_CONFIRM_WINDOW of our fire, or DROP the attempt
        # (short backoff, no start-cooldown) when the bot rejects it by name or
        # nothing is confirmed within PENDING_TTL. {id, username, fired_at,
        # confirmed, confirmed_at} or None.
        self._pending: "dict | None" = None

    # The heist's end message (built-in regex) clears _heist_in_progress, so the
    # NEXT heist — even from a different user moments later — is joined right
    # away; there is no inter-heist timer. This constant is only a stuck-state
    # safety: if an end message is ever missed (observer reconnect, unknown
    # failure wording) don't stay "in progress" forever. A heist never lasts
    # longer than ~3 min, so this never interferes with normal back-to-back heists.
    SAFETY_CLEAR_SECONDS = 180.0

    # A fresh open announcement this many seconds after our !heist counts as
    # "our opener started it" (live captures show the open lands within ~1-2s).
    OPEN_CONFIRM_WINDOW = 8.0
    # How long to keep a fired !heist "pending" while waiting for the open
    # announcement or a by-name rejection. The bot batches rejections up to
    # ~13s late, so this is comfortably above that. A heist itself lasts ~60s,
    # so the pending always resolves before the heist ends.
    PENDING_TTL = 25.0
    # Backoff applied to an opener whose !heist did NOT start a heist (rejected
    # or unconfirmed). NOT the long start-cooldown — the bot's per-account start
    # limit was never consumed, so the account stays usable; this only spaces
    # out retries so we don't hammer the same opener.
    UNCONFIRMED_BACKOFF = 90.0

    # ------------------------------------------------------------------ lifecycle
    def stop(self):
        self._stop.set()
        self._teardown_observer()

    def run(self):
        logger.info("Heist manager started.")
        heist.load_cooldowns()  # restore per-account cooldowns across restarts
        while not self._stop.wait(self.poll_interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("heist manager tick failed")
        logger.info("Heist manager stopped.")

    # ------------------------------------------------------------------ main tick
    def _tick(self):
        with Session(engine) as session:
            cfg = heist.get_config(session)
            openers, joiners = ([], [])
            if cfg["enabled"] and cfg["channel"]:
                openers, joiners = heist.load_heist_accounts(session)
        self._refresh_config(cfg)

        if not cfg["enabled"] or not cfg["channel"] or not cfg["bot"]:
            self._set_offline_state()
            return
        if not joiners and not openers:
            self._set_offline_state()
            return

        online = self._check_online(cfg, joiners, openers)
        if online is False:
            self._set_offline_state()
            return
        # online is True (or None=unknown -> assume up and let IRC sort it out)

        self._ensure_observer(cfg, joiners)
        self._safety_clear()
        self._pending_check()
        self._maybe_open_heist(cfg, openers)

    # ------------------------------------------------------------------ config
    def _refresh_config(self, cfg: dict):
        patterns = (cfg["trigger_regex"], cfg["end_regex"], cfg.get("reject_regex"))
        with self._lock:
            self._cfg = cfg
            # Only recompile when a pattern string actually changed. Otherwise
            # every 5s tick recompiles all three AND re-logs the same warning for
            # an invalid pattern forever.
            if patterns == self._last_patterns:
                return
            self._last_patterns = patterns
            try:
                self._trigger_re = re.compile(cfg["trigger_regex"], re.IGNORECASE) \
                    if cfg["trigger_regex"] else None
            except re.error as e:
                logger.warning("invalid HEIST_TRIGGER_REGEX (%s); disabling trigger", e)
                self._trigger_re = None
            try:
                self._end_re = re.compile(cfg["end_regex"], re.IGNORECASE) \
                    if cfg["end_regex"] else None
            except re.error as e:
                logger.warning("invalid HEIST_END_REGEX (%s); ignoring", e)
                self._end_re = None
            try:
                self._reject_re = re.compile(cfg["reject_regex"], re.IGNORECASE) \
                    if cfg.get("reject_regex") else None
            except re.error as e:
                logger.warning("invalid HEIST_REJECT_REGEX (%s); ignoring", e)
                self._reject_re = None

    # ------------------------------------------------------------------ online
    def _check_online(self, cfg, joiners, openers):
        now = time.monotonic()
        if self._online is not None and (now - self._last_online_check) < self.online_recheck:
            return self._online
        cred = (joiners or openers or [None])[0]
        if cred is None:
            return None
        proxies = cred["proxy"].requests_proxies if cred["proxy"] else None
        result = heist.stream_online(cfg["channel"], cred["token"], proxies,
                                     extra_headers=heist.redeem.fp_for_username(
                                         cred["username"]))
        self._last_online_check = now
        if result is not None:
            if result != self._online:
                logger.info("heist: %s is now %s", cfg["channel"],
                            "ONLINE" if result else "offline")
            self._online = result
        return self._online if result is None else result

    def _set_offline_state(self):
        self._teardown_observer()
        with self._lock:
            self._heist_in_progress = False
            self._pending = None

    # ------------------------------------------------------------------ observer
    def _ensure_observer(self, cfg, joiners):
        if not joiners:
            return
        alive = self._observer_thread is not None and self._observer_thread.is_alive()
        primary = joiners[0]
        proxy_key = primary["proxy"].url if primary.get("proxy") else None
        # Re-create if down, or if the primary joiner account, the CHANNEL, or the
        # assigned PROXY changed. HeistIRC captures the channel and the proxy
        # connect_factory at construction, so without rebuilding on those changes
        # a HEIST_CHANNEL edit leaves the observer in the old channel and a proxy
        # failover leaves it reconnecting through the old (dead) proxy forever.
        if (alive and self._observer_account_id == primary["id"]
                and self._observer_channel == cfg["channel"]
                and self._observer_proxy_key == proxy_key):
            self._extra_joiners = joiners[1:]
            return
        self._teardown_observer()
        observer = heist.HeistIRC(
            primary["username"], primary["token"], cfg["channel"],
            primary["proxy"], on_message=self._on_bot_message,
        )
        t = threading.Thread(target=observer.start,
                             name=f"heist-observer-{primary['username']}", daemon=True)
        t.start()
        with self._lock:
            self._observer = observer
            self._observer_thread = t
            self._observer_account_id = primary["id"]
            self._observer_username = primary["username"]
            self._observer_channel = cfg["channel"]
            self._observer_proxy_key = proxy_key
            self._extra_joiners = joiners[1:]
        logger.info("heist observer/joiner connected as %s in #%s",
                    primary["username"], cfg["channel"])

    def _teardown_observer(self):
        # Swap the pointers out atomically under the lock (like the chat-redeem
        # manager) so a concurrent stop() vs tick thread can't both grab the same
        # observer and double-die/double-join it, or leave a zombie observer that
        # keeps acting after shutdown. Do the blocking die()/join OUTSIDE the lock.
        with self._lock:
            obs, t = self._observer, self._observer_thread
            self._observer = None
            self._observer_thread = None
            self._observer_account_id = None
            self._observer_username = ""
            self._observer_channel = ""
            self._observer_proxy_key = None
            self._extra_joiners = []
        if obs is not None:
            try:
                obs.die()
            except Exception:  # noqa: BLE001
                pass
        if t is not None:
            t.join(timeout=5)

    # ------------------------------------------------------------------ bot messages
    @staticmethod
    def _names_account(text: str, username: str) -> bool:
        """True if a bot message refers to `username` as a whole word (so
        'patriq' never matches inside 'patriq1'). Usernames are [a-z0-9_]."""
        return re.search(r"(?<![0-9a-z_])" + re.escape(username.lower())
                         + r"(?![0-9a-z_])", text.lower()) is not None

    def _on_bot_message(self, nick: str, msg: str):
        """Runs in the observer's IRC thread for every public chat message."""
        with self._lock:
            cfg = self._cfg
            trig, end, rej = self._trigger_re, self._end_re, self._reject_re
            pending = self._pending
        if not cfg or nick.lower() != cfg["bot"]:
            return
        # 1) A by-name rejection of the opener we just fired (e.g. "<acc> - Heist
        #    is currently active" / "@<acc> wait 50s"). Checked first so a !heist
        #    that the bot refused never starts that account's long cooldown.
        if pending is not None and rej is not None \
                and self._names_account(msg, pending["username"]) and rej.search(msg):
            self._on_open_rejected(cfg, pending, msg)
            return
        # 2) End is checked BEFORE the open trigger: a resolved heist clears the
        #    in-progress flag immediately, so the next heist is joinable at once.
        if end is not None and end.search(msg):
            with self._lock:
                was = self._heist_in_progress
                self._heist_in_progress = False
            if was:
                logger.info("heist resolved (bot end message)")
            return
        if trig is not None and trig.search(msg):
            self._handle_open(cfg, msg)

    def _handle_open(self, cfg, msg: str):
        confirm = None
        with self._lock:
            if self._heist_in_progress:
                return  # duplicate announcement of the same heist -> already joined
            self._heist_in_progress = True
            self._heist_since = time.monotonic()
            # A fresh open right after our !heist == our opener started it. Only
            # one heist runs at a time, so this attribution is safe; a late
            # by-name rejection (handled in _on_bot_message) can still retract it.
            pending = self._pending
            if pending is not None and not pending["confirmed"] \
                    and (time.monotonic() - pending["fired_at"]) <= self.OPEN_CONFIRM_WINDOW:
                pending["confirmed"] = True
                pending["confirmed_at"] = time.monotonic()
                confirm = dict(pending)
        if confirm is not None:
            heist.set_cooldown(confirm["id"], cfg["start_cooldown"])
            self._record_event(
                confirm["id"],
                f"{cfg['start_command']} bestätigt gestartet -> Start-Cooldown "
                f"{int(cfg['start_cooldown'])}s",
            )
            logger.info("heist: open confirmed for %s -> start-cooldown set",
                        confirm["username"])
        logger.info("heist OPEN detected: %s", msg[:140])
        # primary join via the persistent observer (fast path)
        delay = max(0.0, cfg["join_delay_ms"] / 1000.0)
        if delay:
            time.sleep(delay)
        obs = self._observer
        if obs is not None:
            try:
                obs.send(cfg["join_command"])
                self._record_event(self._observer_account_id,
                                    f"{cfg['join_command']} gesendet (Heist erkannt)")
                logger.info("heist %s sent by %s", cfg["join_command"],
                            self._observer_username)
            except Exception:  # noqa: BLE001
                logger.exception("observer join failed")
        # secondary joiners (uncommon) via short-lived connections
        for rec in list(self._extra_joiners):
            threading.Thread(
                target=self._join_extra, args=(rec, cfg), daemon=True,
                name=f"heist-join-{rec['username']}",
            ).start()

    def _join_extra(self, rec, cfg):
        ok = heist.fire_heist(rec, cfg["channel"], cfg["join_command"], linger=2.0)
        if ok:
            self._record_event(rec["id"], f"{cfg['join_command']} gesendet")

    # ------------------------------------------------------------------ confirmation
    def _on_open_rejected(self, cfg, pending, msg: str):
        """The bot refused our pending opener's !heist (named it explicitly).

        No heist started for us, so the bot's long per-account start-cooldown was
        NOT consumed: never set it (and retract it if a fast open already had).
        Apply only a short backoff so we don't re-fire the same opener at once.
        """
        currently_active = "currently active" in msg.lower()
        with self._lock:
            was_confirmed = pending.get("confirmed")
            # Only clear the pending if it is STILL the one this rejection refers
            # to. A late by-name rejection for opener A must not wipe a newer
            # pending for opener B that _pending_check + _maybe_open_heist created
            # in the meantime (B would then never get confirmed).
            if self._pending is pending:
                self._pending = None
            if currently_active:
                # A heist really is running (we just never saw its open) -> block
                # opening until its end message arrives.
                self._heist_in_progress = True
                self._heist_since = time.monotonic()
        if was_confirmed:
            heist.clear_cooldown(pending["id"])  # retract the premature credit
        if currently_active:
            # "Heist is currently active" only means one is already running -- our
            # opener was NOT rate-limited, so neither the bot's per-account start
            # limit nor any backoff applies. Leave the account free so it can open
            # the NEXT heist immediately once this one ends.
            heist.clear_cooldown(pending["id"])
            self._record_event(
                pending["id"],
                f"{cfg['start_command']} -> Heist laeuft bereits, kein Cooldown",
            )
            logger.info("heist: %s !heist hit an active heist -> no cooldown",
                        pending["username"])
            return
        secs = self.UNCONFIRMED_BACKOFF
        m = re.search(r"wait\s+(\d+)\s*s", msg, re.IGNORECASE)
        if m:  # "@<acc> wait 50s" -> respect the bot's own retry-after
            secs = max(self.UNCONFIRMED_BACKOFF, float(m.group(1)))
        heist.set_cooldown(pending["id"], secs)
        self._record_event(
            pending["id"],
            f"{cfg['start_command']} abgelehnt ({msg[:70]}) -> kein Start-Cooldown, "
            f"Backoff {int(secs)}s",
        )
        logger.info("heist: %s !heist rejected (%s) -> %ds backoff, no start-cooldown",
                    pending["username"], msg[:60], int(secs))

    def _pending_check(self):
        """Resolve a fired !heist that neither got an open nor a by-name reject
        within PENDING_TTL: treat as 'did not start' (short backoff, no long
        start-cooldown). Also expires a confirmed pending once it has aged out."""
        with self._lock:
            pending = self._pending
            if pending is None:
                return
            if (time.monotonic() - pending["fired_at"]) <= self.PENDING_TTL:
                return
            self._pending = None
            confirmed = pending.get("confirmed")
            cmd = (self._cfg or {}).get("start_command", "!heist")
        if confirmed:
            return  # already credited with the start-cooldown; just aged out
        heist.set_cooldown(pending["id"], self.UNCONFIRMED_BACKOFF)
        self._record_event(
            pending["id"],
            f"{cmd} nicht bestätigt -> kein Start-Cooldown, "
            f"Backoff {int(self.UNCONFIRMED_BACKOFF)}s",
        )
        logger.info("heist: %s open unconfirmed within %.0fs -> %ds backoff, "
                    "no start-cooldown", pending["username"], self.PENDING_TTL,
                    int(self.UNCONFIRMED_BACKOFF))

    # ------------------------------------------------------------------ openers
    def _safety_clear(self):
        # Pure stuck-state recovery (see SAFETY_CLEAR_SECONDS). Normally the end
        # message clears the flag long before this fires.
        with self._lock:
            if self._heist_in_progress and \
                    (time.monotonic() - self._heist_since) > self.SAFETY_CLEAR_SECONDS:
                self._heist_in_progress = False
                logger.warning("heist state force-cleared (missed end message?)")

    def _maybe_open_heist(self, cfg, openers):
        if not openers:
            return
        with self._lock:
            if self._heist_in_progress:
                return
            # Don't fire a second opener while the last one is still awaiting its
            # open/reject verdict (avoids two unattributable !heist in flight).
            if self._pending is not None:
                return
            now = time.monotonic()
            if now < self._next_open_at:
                return
        # earliest-free opener
        openers = sorted(openers, key=lambda r: heist.available_at(r["id"]))
        rec = openers[0]
        if heist.cooldown_remaining(rec["id"]) > 0:
            return  # no opener off cooldown yet
        # advance the spacing gate immediately so the loop won't double-fire
        spacing = random.uniform(min(cfg["spacing_min"], cfg["spacing_max"]),
                                 max(cfg["spacing_min"], cfg["spacing_max"]))
        with self._lock:
            self._next_open_at = time.monotonic() + spacing
        threading.Thread(target=self._open_with, args=(rec, cfg), daemon=True,
                         name=f"heist-open-{rec['username']}").start()

    def _open_with(self, rec, cfg):
        logger.info("heist: opening with %s (%s)", rec["username"], cfg["start_command"])
        # Mark this opener pending BEFORE sending, so the observer thread can
        # match the bot's open/reject (which can arrive within a second) against
        # it. The long start-cooldown is set only once the open is confirmed
        # (_handle_open); a rejection or timeout drops it with a short backoff.
        with self._lock:
            self._pending = {
                "id": rec["id"],
                "username": rec["username"].lower(),
                "fired_at": time.monotonic(),
                "confirmed": False,
            }
        ok = heist.fire_heist(rec, cfg["channel"], cfg["start_command"])
        if ok:
            self._record_event(
                rec["id"], f"{cfg['start_command']} gesendet (warte auf Bestätigung)")
        else:
            # Never left our socket -> nothing to confirm; free the opener again.
            with self._lock:
                if self._pending is not None and self._pending["id"] == rec["id"]:
                    self._pending = None
            logger.warning("heist: opener %s failed to send %s",
                           rec["username"], cfg["start_command"])

    # ------------------------------------------------------------------ events
    def _record_event(self, account_id, message: str):
        if account_id is None:
            return  # Event.account_id is NOT NULL; log-only for global events
        try:
            with Session(engine) as s:
                s.add(Event(account_id=account_id, type="heist", message=message))
                s.commit()
        except Exception:  # noqa: BLE001
            logger.exception("could not record heist event")

    # ------------------------------------------------------------------ status (API)
    def status(self) -> dict:
        with self._lock:
            next_in = max(0.0, self._next_open_at - time.monotonic())
            pending = self._pending
            pending_info = None
            if pending is not None:
                pending_info = {
                    "account_id": pending["id"],
                    "username": pending["username"],
                    "confirmed": pending.get("confirmed", False),
                    "age": round(time.monotonic() - pending["fired_at"], 1),
                }
            return {
                "online": self._online,
                "observer_connected": (
                    self._observer_thread is not None
                    and self._observer_thread.is_alive()
                ),
                "observer_account_id": self._observer_account_id,
                "observer_username": self._observer_username or None,
                "heist_active": self._heist_in_progress,
                "pending_open": pending_info,
                "next_open_in": round(next_in, 1),
                "cooldowns": heist.active_cooldowns(),
            }


# Module-level singleton (mirrors backend.manager.manager) so routers and the
# app entrypoint share one coordinator instance.
heist_manager = HeistManager()
