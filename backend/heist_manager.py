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

        # observer / joiner state
        self._observer: "heist.HeistIRC | None" = None
        self._observer_thread: "threading.Thread | None" = None
        self._observer_account_id: "int | None" = None
        self._observer_username: str = ""
        self._extra_joiners: list = []

        # runtime state
        self._online: "bool | None" = None
        self._last_online_check = 0.0
        self._heist_in_progress = False  # set on open announcement, cleared on end
        self._heist_since = 0.0
        self._next_open_at = 0.0
        self._last_event_msg = ""

    # The heist's end message (built-in regex) clears _heist_in_progress, so the
    # NEXT heist — even from a different user moments later — is joined right
    # away; there is no inter-heist timer. This constant is only a stuck-state
    # safety: if an end message is ever missed (observer reconnect, unknown
    # failure wording) don't stay "in progress" forever. Far longer than any real
    # heist (~60-90s), so it never interferes with normal back-to-back heists.
    SAFETY_CLEAR_SECONDS = 600.0

    # ------------------------------------------------------------------ lifecycle
    def stop(self):
        self._stop.set()
        self._teardown_observer()

    def run(self):
        logger.info("Heist manager started.")
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
        self._maybe_open_heist(cfg, openers)

    # ------------------------------------------------------------------ config
    def _refresh_config(self, cfg: dict):
        with self._lock:
            self._cfg = cfg
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

    # ------------------------------------------------------------------ online
    def _check_online(self, cfg, joiners, openers):
        now = time.monotonic()
        if self._online is not None and (now - self._last_online_check) < self.online_recheck:
            return self._online
        cred = (joiners or openers or [None])[0]
        if cred is None:
            return None
        proxies = cred["proxy"].requests_proxies if cred["proxy"] else None
        result = heist.stream_online(cfg["channel"], cred["token"], proxies)
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

    # ------------------------------------------------------------------ observer
    def _ensure_observer(self, cfg, joiners):
        if not joiners:
            return
        alive = self._observer_thread is not None and self._observer_thread.is_alive()
        # Re-create if down, or if the primary joiner account changed.
        primary = joiners[0]
        if alive and self._observer_account_id == primary["id"]:
            self._extra_joiners = joiners[1:]
            return
        self._teardown_observer()
        self._extra_joiners = joiners[1:]
        observer = heist.HeistIRC(
            primary["username"], primary["token"], cfg["channel"],
            primary["proxy"], on_message=self._on_bot_message,
        )
        t = threading.Thread(target=observer.start,
                             name=f"heist-observer-{primary['username']}", daemon=True)
        t.start()
        self._observer = observer
        self._observer_thread = t
        self._observer_account_id = primary["id"]
        self._observer_username = primary["username"]
        logger.info("heist observer/joiner connected as %s in #%s",
                    primary["username"], cfg["channel"])

    def _teardown_observer(self):
        obs, t = self._observer, self._observer_thread
        self._observer = None
        self._observer_thread = None
        self._observer_account_id = None
        self._observer_username = ""
        self._extra_joiners = []
        if obs is not None:
            try:
                obs.die()
            except Exception:  # noqa: BLE001
                pass
        if t is not None:
            t.join(timeout=5)

    # ------------------------------------------------------------------ bot messages
    def _on_bot_message(self, nick: str, msg: str):
        """Runs in the observer's IRC thread for every public chat message."""
        with self._lock:
            cfg = self._cfg
            trig, end = self._trigger_re, self._end_re
        if not cfg or nick.lower() != cfg["bot"]:
            return
        # End is checked BEFORE the open trigger: a resolved heist clears the
        # in-progress flag immediately, so the next heist is joinable at once.
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
        with self._lock:
            if self._heist_in_progress:
                return  # duplicate announcement of the same heist -> already joined
            self._heist_in_progress = True
            self._heist_since = time.monotonic()
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
        logger.info("heist: opening with %s (!heist)", rec["username"])
        ok = heist.fire_heist(rec, cfg["channel"], cfg["start_command"])
        if ok:
            heist.set_cooldown(rec["id"], cfg["start_cooldown"])
            self._record_event(rec["id"], f"{cfg['start_command']} gesendet")
        else:
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
            return {
                "online": self._online,
                "observer_connected": (
                    self._observer_thread is not None
                    and self._observer_thread.is_alive()
                ),
                "observer_account_id": self._observer_account_id,
                "observer_username": self._observer_username or None,
                "heist_active": self._heist_in_progress,
                "next_open_in": round(next_in, 1),
                "cooldowns": heist.active_cooldowns(),
            }


# Module-level singleton (mirrors backend.manager.manager) so routers and the
# app entrypoint share one coordinator instance.
heist_manager = HeistManager()
