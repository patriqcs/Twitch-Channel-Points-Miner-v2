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
    def _probe(p: Proxy) -> bool:
        try:
            return bool(to_engine_proxy(p).test_proxy(timeout=8).get("ok"))
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
        with Session(engine) as session:
            proxies = {p.id: p for p in session.exec(select(Proxy)).all()}
            accounts = session.exec(select(Account)).all()
            if not proxies and not any(a.proxy_id for a in accounts):
                return  # nothing proxy-related to manage

            usage: dict[int, int] = {}
            for a in accounts:
                if a.proxy_id is not None:
                    usage[a.proxy_id] = usage.get(a.proxy_id, 0) + 1

            errored = self._recent_proxy_errors(session)
            tested: dict[int, bool] = {}

            def healthy(pid: int) -> bool:
                if pid not in tested:
                    p = proxies.get(pid)
                    tested[pid] = self._probe(p) if p else False
                return tested[pid]

            def pick_replacement(exclude_id: int | None) -> Proxy | None:
                cands = [
                    p for pid, p in proxies.items()
                    if pid != exclude_id
                    and usage.get(pid, 0) < config.MAX_ACCOUNTS_PER_PROXY
                ]
                cands.sort(key=lambda p: usage.get(p.id, 0))  # least-loaded first
                for p in cands:
                    if healthy(p.id):
                        return p
                return None

            for acc in accounts:
                if not self.manager.is_running(acc.username):
                    self._fails.pop(acc.username, None)
                    continue

                if acc.proxy_id is not None:
                    probe_ok = healthy(acc.proxy_id)
                    runtime_bad = acc.id in errored
                    if probe_ok and not runtime_bad:
                        self._fails[acc.username] = 0
                        continue

                    # count consecutive failures; a runtime error escalates now
                    n = self._fails.get(acc.username, 0) + 1
                    self._fails[acc.username] = n
                    if not runtime_bad and n < config.PROXY_FAIL_THRESHOLD:
                        continue

                    old_id = acc.proxy_id
                    repl = pick_replacement(exclude_id=old_id)
                    if repl is not None:
                        usage[old_id] = max(0, usage.get(old_id, 1) - 1)
                        usage[repl.id] = usage.get(repl.id, 0) + 1
                        acc.proxy_id = repl.id
                        self._failover(session, acc, repl, old_id, runtime_bad)
                    elif config.PROXY_ALLOW_DIRECT:
                        usage[old_id] = max(0, usage.get(old_id, 1) - 1)
                        acc.proxy_id = None
                        self._record(
                            session, acc,
                            f"proxy #{old_id} dead and no working proxy free "
                            f"-> running WITHOUT proxy (last resort)",
                        )
                        session.add(acc)
                        session.commit()
                        self._fails[acc.username] = 0
                        self.manager.restart(acc.username)
                    else:
                        self._record(
                            session, acc,
                            f"proxy #{old_id} dead, no replacement and direct "
                            f"disabled -> keeping (will retry)",
                        )
                        session.commit()
                else:
                    # running direct: re-attach a working proxy once one is free
                    repl = pick_replacement(exclude_id=None)
                    if repl is not None:
                        usage[repl.id] = usage.get(repl.id, 0) + 1
                        acc.proxy_id = repl.id
                        self._record(
                            session, acc,
                            f"working proxy available -> attached "
                            f"{repl.scheme}://{repl.host}:{repl.port} (#{repl.id})",
                        )
                        session.add(acc)
                        session.commit()
                        self.manager.restart(acc.username)

    # ---- helpers ----
    def _failover(self, session, acc, repl, old_id, runtime_bad) -> None:
        why = "runtime errors" if runtime_bad else "probe failed"
        self._record(
            session, acc,
            f"proxy #{old_id} dead ({why}) -> switched to "
            f"{repl.scheme}://{repl.host}:{repl.port} (#{repl.id})",
        )
        session.add(acc)
        session.commit()
        self._fails[acc.username] = 0
        self.manager.restart(acc.username)

    def _record(self, session: Session, acc: Account, message: str) -> None:
        logger.info("[%s] %s", acc.username, message)
        session.add(Event(account_id=acc.id, type="proxy", message=message))
