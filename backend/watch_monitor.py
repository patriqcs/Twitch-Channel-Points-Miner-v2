# -*- coding: utf-8 -*-
"""Peer watch watchdog: spot an account that is 'online but not earning'.

Every account watches the SAME global streamer list, so the running accounts
form a control group under identical conditions. Channel points are only earned
when the minute-watched POST (spade_url, through the proxy) succeeds, whereas
the online status comes from a separate PubSub/IRC path — so a half-broken proxy
can leave one account showing online while it earns nothing.

Each cycle we compare the point *progress* of every comparable account over a
rolling window:
  * progress = sum of positive snapshot-to-snapshot deltas (ignores redeem/spend
    drops, which would otherwise look like "no earning").
  * If a healthy majority earns (median peer progress >= WATCH_MIN_EARN) but a
    given account earned ~0, it is the outlier -> fail its proxy over + restart.
  * If almost nobody earns (streamers offline / not paying), we do nothing — the
    comparison self-calibrates, no absolute threshold guessing.

Acts only after WATCH_STALL_STRIKES consecutive confirmations to avoid acting on
a streamer briefly going offline between two accounts' snapshots.
"""
import logging
import statistics
import threading
from collections import defaultdict
from datetime import timedelta

from sqlmodel import Session, select

from backend import config
from backend.db import engine
from backend.models import Account, Event, utcnow

logger = logging.getLogger("watch_monitor")

POINTS_SNAPSHOT = "points_snapshot"


class WatchHealthMonitor:
    def __init__(self, manager):
        self.manager = manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._strikes: dict[str, int] = defaultdict(int)  # username -> consec. stalls

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="watch-monitor", daemon=True
        )
        self._thread.start()
        logger.info(
            "Watch monitor started (interval=%ss window=%ss min_cohort=%s min_earn=%s)",
            config.WATCH_CHECK_INTERVAL, config.WATCH_WINDOW,
            config.WATCH_MIN_COHORT, config.WATCH_MIN_EARN,
        )

    def stop(self) -> None:
        self._stop.set()

    # ---- loop ----
    def _loop(self) -> None:
        while not self._stop.wait(config.WATCH_CHECK_INTERVAL):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("watch monitor tick failed")

    @staticmethod
    def _progress(balances: list[int]) -> int:
        """Sum of positive consecutive deltas — points gained, ignoring spends."""
        return sum(
            max(0, balances[i] - balances[i - 1]) for i in range(1, len(balances))
        )

    def _tick(self) -> None:
        window = config.WATCH_WINDOW

        # Only accounts running at least a full window have enough data to judge.
        uptimes = self.manager.running_uptimes()
        eligible = [u for u, up in uptimes.items() if up >= window]
        if len(eligible) < config.WATCH_MIN_COHORT:
            self._strikes.clear()
            return

        since = utcnow() - timedelta(seconds=window)
        progress: dict[str, int] = {}
        ids: dict[str, int] = {}
        no_proxy_map: dict[str, bool] = {}
        with Session(engine) as session:
            accounts = session.exec(
                select(Account).where(Account.username.in_(eligible))
            ).all()
            for acc in accounts:
                no_proxy_map[acc.username] = bool(acc.no_proxy)
                rows = session.exec(
                    select(Event.ts, Event.balance)
                    .where(Event.account_id == acc.id)
                    .where(Event.type == POINTS_SNAPSHOT)
                    .where(Event.ts >= since)
                    .order_by(Event.ts)
                ).all()
                bals = [r[1] for r in rows if r[1] is not None]
                if len(bals) < 2:
                    continue  # not enough samples to judge this account
                span = (rows[-1][0] - rows[0][0]).total_seconds()
                if span < window * 0.5:
                    continue  # samples don't cover enough of the window
                ids[acc.username] = acc.id
                progress[acc.username] = self._progress(bals)

        if len(progress) < config.WATCH_MIN_COHORT:
            return

        earners = [p for p in progress.values() if p > 0]
        median_earn = statistics.median(earners) if earners else 0
        # Need a healthy paying majority, otherwise the streamers just aren't
        # giving points right now -> no reliable signal, reset and bail.
        if len(earners) < (len(progress) + 1) // 2 or median_earn < config.WATCH_MIN_EARN:
            for u in progress:
                self._strikes[u] = 0
            return

        # Outliers: earned (essentially) nothing while peers clearly did.
        to_fix: list[str] = []
        for u, p in progress.items():
            if p <= 0:
                self._strikes[u] += 1
                if self._strikes[u] >= config.WATCH_STALL_STRIKES:
                    to_fix.append(u)
            else:
                self._strikes[u] = 0

        for u in to_fix:
            self._strikes[u] = 0
            aid = ids[u]
            # Decide who remediates. The proxy monitor only acts on PROXIED
            # accounts and only when it is enabled; a no_proxy account (or any
            # account when the proxy monitor is off) would otherwise be flagged
            # every cycle forever but never healed. In those cases restart the
            # account directly here — a restart is the only lever we have. For a
            # normal proxied account with the monitor enabled we still hand off
            # via 'proxy_error' (failover + one restart) to avoid double restarts.
            proxy_monitor_will_act = (
                config.PROXY_MONITOR_ENABLED and not no_proxy_map.get(u, False)
            )
            if proxy_monitor_will_act:
                msg = (f"peers earned ~{int(median_earn)} pts in {window}s but this "
                       f"account earned 0 (online but not watching) -> failover")
                logger.warning("[%s] %s", u, msg)
                with Session(engine) as session:
                    session.add(Event(account_id=aid, type="status",
                                      reason="watch_stalled", message=msg))
                    session.add(Event(account_id=aid, type="proxy_error",
                                      message="watch stalled vs peers"))
                    session.commit()
            else:
                msg = (f"peers earned ~{int(median_earn)} pts in {window}s but this "
                       f"account earned 0 (online but not watching) -> restart")
                logger.warning("[%s] %s", u, msg)
                with Session(engine) as session:
                    session.add(Event(account_id=aid, type="status",
                                      reason="watch_stalled", message=msg))
                    session.commit()
                if self.manager.is_running(u):
                    threading.Thread(
                        target=self.manager.restart, args=(u,),
                        name=f"watch-restart-{u}", daemon=True,
                    ).start()
