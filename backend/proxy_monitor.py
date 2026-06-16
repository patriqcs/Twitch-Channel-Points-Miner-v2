# -*- coding: utf-8 -*-
"""Background proxy health monitor with automatic failover.

Every PROXY_CHECK_INTERVAL seconds it checks the proxy of each *running*
account. A proxy is considered dead when EITHER:
  * an active probe (Twitch reachability through it) fails
    PROXY_FAIL_THRESHOLD checks in a row, OR
  * the miner reported recent runtime connection errors (a 'proxy_error'
    event) — this reacts within one cycle instead of waiting for the threshold.

On failure the account is moved to another *working* proxy (probed on demand,
respecting MAX_ACCOUNTS_PER_PROXY). If none is free and PROXY_ALLOW_DIRECT is
set, the account keeps mining without a proxy as a last resort. Accounts that
ended up direct are re-attached to a proxy as soon as a working one is free.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlmodel import Session, select

from backend import config
from backend.db import engine
from backend.models import Account, Event, Proxy, utcnow
from backend.proxy_util import to_engine_proxy

logger = logging.getLogger("proxy_monitor")

PROXY_ERROR_EVENT = "proxy_error"


class ProxyHealthMonitor:
    def __init__(self, manager):
        self.manager = manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fails: dict[str, int] = {}  # username -> consecutive probe failures

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="proxy-monitor", daemon=True
        )
        self._thread.start()
        logger.info(
            "Proxy monitor started (interval=%ss threshold=%s allow_direct=%s)",
            config.PROXY_CHECK_INTERVAL,
            config.PROXY_FAIL_THRESHOLD,
            config.PROXY_ALLOW_DIRECT,
        )

    def stop(self) -> None:
        self._stop.set()

    # ---- loop ----
    def _loop(self) -> None:
        while not self._stop.wait(config.PROXY_CHECK_INTERVAL):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("proxy monitor tick failed")

    @staticmethod
    def _probe_ep(ep) -> bool:
        try:
            return bool(ep.test_proxy(timeout=8).get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def _recent_proxy_errors(self, session: Session) -> set[int]:
        """account_ids that reported a runtime proxy error within the last window."""
        window = max(config.PROXY_CHECK_INTERVAL * 2, 90)
        since = utcnow() - timedelta(seconds=window)
        rows = session.exec(
            select(Event.account_id)
            .where(Event.type == PROXY_ERROR_EVENT)
            .where(Event.ts >= since)
        ).all()
        return set(rows)

    def _tick(self) -> None:
        # Phase 1 — snapshot (SHORT db session): copy everything we need into plain
        # data + detached engine-proxy objects, then release the connection BEFORE
        # doing any network probing (probing dead proxies can take many seconds and
        # must never hold a pooled DB connection — that exhausted the pool).
        with Session(engine) as session:
            proxies = {}  # id -> {"ep": EngineProxy, "label": str}
            for p in session.exec(select(Proxy)).all():
                proxies[p.id] = {"ep": to_engine_proxy(p),
                                 "label": f"{p.scheme}://{p.host}:{p.port}"}
            accts = [(a.id, a.username, a.proxy_id)
                     for a in session.exec(select(Account)).all()]
            errored = self._recent_proxy_errors(session)

        if not proxies and not any(pid for _, _, pid in accts):
            return

        usage: dict[int, int] = {}
        for _, _, pid in accts:
            if pid is not None:
                usage[pid] = usage.get(pid, 0) + 1

        # Phase 2 — probe + decide (NO db connection held)
        tested: dict[int, bool] = {}

        def healthy(pid: int) -> bool:
            if pid not in tested:
                m = proxies.get(pid)
                tested[pid] = self._probe_ep(m["ep"]) if m else False
            return tested[pid]

        def pick_replacement(exclude_id):
            cands = [pid for pid in proxies
                     if pid != exclude_id and usage.get(pid, 0) < config.MAX_ACCOUNTS_PER_PROXY]
            cands.sort(key=lambda pid: usage.get(pid, 0))
            for pid in cands:
                if healthy(pid):
                    return pid
            return None

        decisions = []  # (account_id, username, new_proxy_id|keep, change_bool, message)
        for aid, uname, pid in accts:
            if not self.manager.is_running(uname):
                self._fails.pop(uname, None)
                continue
            if pid is not None:
                if healthy(pid) and aid not in errored:
                    self._fails[uname] = 0
                    continue
                runtime_bad = aid in errored
                n = self._fails.get(uname, 0) + 1
                self._fails[uname] = n
                if not runtime_bad and n < config.PROXY_FAIL_THRESHOLD:
                    continue
                repl = pick_replacement(pid)
                why = "runtime errors" if runtime_bad else "probe failed"
                if repl is not None:
                    usage[pid] = max(0, usage.get(pid, 1) - 1)
                    usage[repl] = usage.get(repl, 0) + 1
                    decisions.append((aid, uname, repl, True,
                                      f"proxy #{pid} dead ({why}) -> {proxies[repl]['label']} (#{repl})"))
                    self._fails[uname] = 0
                elif config.PROXY_ALLOW_DIRECT:
                    usage[pid] = max(0, usage.get(pid, 1) - 1)
                    decisions.append((aid, uname, None, True,
                                      f"proxy #{pid} dead ({why}), none free -> WITHOUT proxy"))
                    self._fails[uname] = 0
                else:
                    decisions.append((aid, uname, None, False,
                                      f"proxy #{pid} dead, no replacement (direct disabled)"))
            else:
                repl = pick_replacement(None)
                if repl is not None:
                    usage[repl] = usage.get(repl, 0) + 1
                    decisions.append((aid, uname, repl, True,
                                      f"working proxy available -> attached {proxies[repl]['label']} (#{repl})"))

        if not decisions:
            return

        # Phase 3 — apply (SHORT db session) + restart affected miners
        with Session(engine) as session:
            for aid, uname, new_pid, change, msg in decisions:
                if change:
                    acc = session.get(Account, aid)
                    if acc is not None:
                        acc.proxy_id = new_pid
                        session.add(acc)
                session.add(Event(account_id=aid, type="proxy", message=msg))
            session.commit()
        for aid, uname, new_pid, change, msg in decisions:
            logger.info("[%s] %s", uname, msg)
            if change:
                self.manager.restart(uname)
