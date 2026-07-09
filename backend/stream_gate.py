# -*- coding: utf-8 -*-
"""Stream-live gate: run accounts ONLY while a configured streamer is live.

Every account mines the SAME streamer list and channel points only accrue while
a streamer is actually broadcasting. Keeping the accounts connected 24/7 while
the channel is offline earns nothing and is a bot tell — real viewers are only
present during a stream. This monitor polls the live status CENTRALLY (one GQL
request per streamer, not one per account) and drives the whole fleet:

  * stream goes live  -> stagger the enabled accounts UP (they trickle in)
  * stream goes offline-> stagger the running accounts DOWN (they trickle out)

Hysteresis is asymmetric (see config.STREAM_GATE_*):
  * online  -> act immediately (viewers appear as soon as the stream starts)
  * offline -> only after OFFLINE_CONFIRM consecutive offline polls (a brief
    encoder/stream drop must not cycle the whole fleet)
  * poll error -> after FAILOPEN_AFTER consecutive failures, treat as ONLINE
    (fail-open): a broken live-check must never silently halt all mining.

It only acts on STATE TRANSITIONS; between transitions it leaves processes
alone, so a manual start/stop from the UI is respected until the next flip.
A deliberate manager.stop() bumps the account's lifecycle epoch, which cancels
any pending crash auto-restart — so draining an account down does not fight the
reaper. While live, the heartbeat/auto-restart watchdogs work as usual.
"""
import copy
import logging
import random
import threading
from datetime import datetime, timezone

import requests
from sqlmodel import Session, select

from backend import config, redeem
from backend.db import engine
from backend.models import Account, AppSetting, Event, Proxy
from backend.proxy_util import to_engine_proxy
from TwitchChannelPointsMiner.constants import GQLOperations

logger = logging.getLogger("stream_gate")

STREAMERS_KEY = "STREAMERS"


def _record_event(username: str, reason: str, message: str) -> None:
    """Best-effort status Event so gate actions show up on the dashboard."""
    try:
        with Session(engine) as session:
            acc = session.exec(
                select(Account).where(Account.username == username)
            ).first()
            if acc is None:
                return
            session.add(Event(account_id=acc.id, type="status",
                              reason=reason, message=message))
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not record gate event for %s", username)


class StreamGateMonitor:
    def __init__(self, manager):
        self.manager = manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Confirmed target state we last drove the fleet to. None = unknown (boot).
        self._online: bool | None = None
        self._offline_strikes = 0
        self._fail_strikes = 0
        self._logged_initial = False
        self._churn_thread: threading.Thread | None = None
        self._paused: set[str] = set()  # accounts currently on a session-churn pause
        # Transition generation: bumped on every flip (and on stop) so a slow
        # in-flight ramp worker aborts when the state changes under it.
        self._gen = 0
        self._gen_lock = threading.Lock()

    # ---- lifecycle ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="stream-gate", daemon=True
        )
        self._thread.start()
        logger.info(
            "Stream gate started (interval=%ss offline_confirm=%s failopen_after=%s "
            "ramp_step=%s-%ss drain_step=%s-%ss)",
            config.STREAM_GATE_CHECK_INTERVAL, config.STREAM_GATE_OFFLINE_CONFIRM,
            config.STREAM_GATE_FAILOPEN_AFTER,
            config.STREAM_GATE_RAMP_STEP_MIN, config.STREAM_GATE_RAMP_STEP_MAX,
            config.STREAM_GATE_DRAIN_STEP_MIN, config.STREAM_GATE_DRAIN_STEP_MAX,
        )
        if config.SESSION_CHURN_ENABLED:
            self._churn_thread = threading.Thread(
                target=self._churn_loop, name="stream-gate-churn", daemon=True
            )
            self._churn_thread.start()
            logger.info(
                "Session churn on (every %ss p=%.2f pause=%s-%ss, keep>=%s running, max %s paused)",
                config.SESSION_CHURN_INTERVAL, config.SESSION_CHURN_PROB,
                config.SESSION_PAUSE_MIN, config.SESSION_PAUSE_MAX,
                config.SESSION_CHURN_MIN_PRESENT, config.SESSION_CHURN_MAX_CONCURRENT,
            )

    def stop(self) -> None:
        self._stop.set()
        with self._gen_lock:
            self._gen += 1  # abort any in-flight ramp worker

    # ---- poll loop ----
    def _loop(self) -> None:
        # Evaluate once immediately so boot reacts without waiting a full interval.
        self._safe_tick()
        while not self._stop.wait(config.STREAM_GATE_CHECK_INTERVAL):
            self._safe_tick()

    def _safe_tick(self) -> None:
        try:
            self._tick()
        except Exception:  # noqa: BLE001
            logger.exception("stream gate tick failed")

    def _tick(self) -> None:
        live = self._poll_live()  # True (a streamer is live) / False / None (error)

        if not self._logged_initial:
            self._logged_initial = True
            state = "unknown (poll error)" if live is None else (
                "LIVE" if live else "offline")
            logger.info(
                "initial live status of %s = %s",
                ", ".join(self._configured_streamers()) or "(none configured)", state,
            )

        if live is None:
            self._fail_strikes += 1
            if (self._fail_strikes >= config.STREAM_GATE_FAILOPEN_AFTER
                    and self._online is not True):
                logger.warning(
                    "live-check failed %d times in a row -> fail-open, "
                    "treating streamer as ONLINE so mining is not halted",
                    self._fail_strikes,
                )
                self._enter_online()
            return

        self._fail_strikes = 0

        if live:
            self._offline_strikes = 0
            if self._online is not True:
                logger.info("streamer detected LIVE -> ramping accounts up")
                self._enter_online()
        else:
            self._offline_strikes += 1
            if (self._online is not False
                    and self._offline_strikes >= config.STREAM_GATE_OFFLINE_CONFIRM):
                logger.info(
                    "streamer OFFLINE confirmed (%d checks) -> draining accounts down",
                    self._offline_strikes,
                )
                self._enter_offline()

    # ---- live status poll (central: one request per streamer, not per account) ----
    def _configured_streamers(self) -> list[str]:
        with Session(engine) as session:
            setting = session.get(AppSetting, STREAMERS_KEY)
        if not setting or not setting.value:
            return []
        return [
            line.strip()
            for line in setting.value.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _poll_live(self) -> bool | None:
        """True if ANY configured streamer is live, False if all confirmed
        offline, None if we couldn't get a usable answer (network/parse error)."""
        streamers = self._configured_streamers()
        if not streamers:
            return None  # nothing configured -> unknown; fail-open path applies
        saw_answer = False
        for login in streamers:
            res = self._check_one(login)
            if res is True:
                return True
            if res is False:
                saw_answer = True
        return False if saw_answer else None

    @staticmethod
    def _check_one(login: str) -> bool | None:
        """Query one streamer's live status. True=live, False=offline, None=error."""
        json_data = copy.deepcopy(GQLOperations.VideoPlayerStreamInfoOverlayChannel)
        json_data["variables"] = {"channel": login}
        try:
            resp = requests.post(
                GQLOperations.url,
                json=json_data,
                # Volle TV-Client-Signatur (Client-Id + TV-UA + Client-Version +
                # Client-Session-Id) statt nur Client-Id + generischer TV-UA — der
                # anonyme Live-Check sieht damit wie ein echter TV-Client aus.
                headers=redeem.fp_headers(),
                timeout=config.STREAM_GATE_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            user = resp.json()["data"]["user"]
            if user is None:
                return None  # unknown login / malformed -> don't treat as offline
            return user["stream"] is not None
        except Exception:  # noqa: BLE001
            return None

    # ---- transitions ----
    def _next_gen(self) -> int:
        with self._gen_lock:
            self._gen += 1
            return self._gen

    def _superseded(self, gen: int) -> bool:
        with self._gen_lock:
            return gen != self._gen or self._stop.is_set()

    def _enter_online(self) -> None:
        self._online = True
        self._paused.clear()  # fresh session -> no stale pauses carry over
        gen = self._next_gen()
        threading.Thread(
            target=self._ramp_up, args=(gen,), name="gate-rampup", daemon=True
        ).start()

    def _enter_offline(self) -> None:
        self._online = False
        self._paused.clear()  # stream ended -> pending resumes are moot
        gen = self._next_gen()
        threading.Thread(
            target=self._ramp_down, args=(gen,), name="gate-rampdown", daemon=True
        ).start()

    # ---- variable session presence (churn) ----
    def _churn_loop(self) -> None:
        while not self._stop.wait(config.SESSION_CHURN_INTERVAL):
            try:
                if self._online is True:
                    self._maybe_churn()
            except Exception:  # noqa: BLE001
                logger.exception("session churn tick failed")

    def _maybe_churn(self) -> None:
        """Occasionally pause one running account for a random while, so watch
        sessions don't all span the identical start-to-end window."""
        if random.random() > config.SESSION_CHURN_PROB:
            return
        if len(self._paused) >= config.SESSION_CHURN_MAX_CONCURRENT:
            return
        running = [u for u in self._running_usernames() if u not in self._paused]
        # Never dip below the floor of present accounts (protects the last one).
        if len(running) - 1 < config.SESSION_CHURN_MIN_PRESENT:
            return
        victim = random.choice(running)
        pause = random.uniform(config.SESSION_PAUSE_MIN, config.SESSION_PAUSE_MAX)
        gen = self._gen  # tie the resume to the current online generation
        self._paused.add(victim)
        _record_event(victim, "session_pause",
                      f"viewer stepped away -> pause {int(pause / 60)}min (gated)")
        logger.info("[%s] session pause for %.0fmin (variable presence)", victim, pause / 60)
        self.manager.stop(victim)
        timer = threading.Timer(pause, self._resume_after_pause, args=(victim, gen))
        timer.daemon = True
        timer.start()

    def _resume_after_pause(self, username: str, gen: int) -> None:
        self._paused.discard(username)
        if self._stop.is_set() or self._superseded(gen) or self._online is not True:
            return  # stream ended / state changed during the pause -> stay stopped
        if self.manager.is_running(username):
            return
        if self.manager.start(username):
            _record_event(username, "session_resume", "viewer returned -> resume (gated)")
            logger.info("[%s] session resume (variable presence)", username)

    def _enabled_ramp_order(self) -> list[str]:
        """Enabled usernames ordered for ramp-up: established accounts first,
        newly added ones later (warm-up — a fresh account shouldn't appear the
        instant a stream starts). Age noise keeps it from being a rigid order."""
        now = datetime.now(timezone.utc)
        rows: list[tuple[str, float]] = []
        with Session(engine) as session:
            for a in session.exec(
                select(Account).where(Account.enabled == True)  # noqa: E712
            ).all():
                created = a.created_at
                if created is None:
                    age = 1e9  # unknown -> treat as very established
                else:
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age = max(0.0, (now - created).total_seconds() / 86400.0)
                rows.append((a.username, age))
        # Older first (descending age), with ±3-day noise so similar-age
        # accounts don't start in a fixed sequence every time.
        rows.sort(key=lambda t: -(t[1] + random.uniform(-3.0, 3.0)))
        return [u for u, _ in rows]

    def _running_usernames(self) -> list[str]:
        return [u for u, alive in self.manager.statuses().items() if alive]

    def _sleep_until(self, gen: int, seconds: float) -> bool:
        """Interruptibly wait `seconds`. Returns False if superseded/stopping."""
        deadline = seconds
        step = 0.0
        while step < deadline:
            if self._superseded(gen):
                return False
            self._stop.wait(min(2.0, deadline - step))
            step += 2.0
        return not self._superseded(gen)

    def _ramp_up(self, gen: int) -> None:
        # Miners need their proxies; wait (bounded) for the shared tunnel first.
        if not self._await_proxies(gen):
            return
        accounts = self._enabled_ramp_order()
        logger.info("stream-gate ramp-up: %d enabled account(s)", len(accounts))
        for i, u in enumerate(accounts):
            # First account starts right away; each subsequent one after a random
            # gap so they trickle in over minutes instead of arriving at once.
            if i > 0:
                gap = random.uniform(
                    config.STREAM_GATE_RAMP_STEP_MIN, config.STREAM_GATE_RAMP_STEP_MAX
                )
                if not self._sleep_until(gen, gap):
                    return
            if self._superseded(gen):
                return
            if self.manager.is_running(u):
                continue
            if self.manager.start(u):
                _record_event(u, "stream_online", "streamer live -> start (gated)")
                logger.info("[%s] started (streamer live)", u)

    def _ramp_down(self, gen: int) -> None:
        accounts = self._running_usernames()
        random.shuffle(accounts)
        logger.info("stream-gate ramp-down: %d running account(s)", len(accounts))
        for i, u in enumerate(accounts):
            if i > 0:
                gap = random.uniform(
                    config.STREAM_GATE_DRAIN_STEP_MIN, config.STREAM_GATE_DRAIN_STEP_MAX
                )
                if not self._sleep_until(gen, gap):
                    return
            if self._superseded(gen):
                return
            if self.manager.is_running(u):
                _record_event(u, "stream_offline", "streamer offline -> stop (gated)")
                self.manager.stop(u)
                logger.info("[%s] stopped (streamer offline)", u)

    def _await_proxies(self, gen: int) -> bool:
        """Wait until at least one assigned proxy is reachable (bounded by
        AUTOSTART_MAX_WAIT). Returns False if superseded/stopping. Direct-only
        fleets (no proxies) return True immediately."""
        import time

        deadline = time.monotonic() + max(0, config.AUTOSTART_MAX_WAIT)
        while True:
            if self._superseded(gen):
                return False
            if self._proxies_ready():
                return True
            if time.monotonic() >= deadline:
                logger.warning(
                    "stream-gate: proxies still not ready after %ss -> starting "
                    "anyway (proxy monitor will heal)", config.AUTOSTART_MAX_WAIT
                )
                return True
            if not self._sleep_until(gen, 4.0):
                return False

    @staticmethod
    def _proxies_ready() -> bool:
        with Session(engine) as session:
            pids = {
                a.proxy_id for a in session.exec(
                    select(Account).where(Account.enabled == True)  # noqa: E712
                ).all() if a.proxy_id is not None
            }
            eps = [to_engine_proxy(session.get(Proxy, pid)) for pid in pids]
        eps = [e for e in eps if e is not None]
        if not eps:
            return True  # direct mining: nothing to wait for
        for ep in eps:
            try:
                if ep.test_proxy(timeout=6).get("ok"):
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False
