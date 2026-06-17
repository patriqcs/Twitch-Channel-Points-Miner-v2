# -*- coding: utf-8 -*-
"""Process manager: one OS subprocess per account, each running miner_runner.

Each miner runs isolated (a crash of one account never takes down the others).
Subprocess stdout/stderr is appended to LOGS_DIR/<username>.log (live-tailed in
Phase 5). A background reaper updates account status when a process exits.
"""
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta

from sqlmodel import Session, select

from backend import config
from backend.db import engine
from backend.models import Account, Event, utcnow

logger = logging.getLogger("manager")


def _set_status(username: str, status: str) -> None:
    with Session(engine) as session:
        acc = session.exec(select(Account).where(Account.username == username)).first()
        if acc is not None:
            acc.status = status
            session.add(acc)
            session.commit()


def _record_event(username: str, etype: str, **fields) -> None:
    """Best-effort account Event (so the dashboard shows watchdog actions)."""
    try:
        with Session(engine) as session:
            acc = session.exec(
                select(Account).where(Account.username == username)
            ).first()
            if acc is None:
                return
            session.add(Event(account_id=acc.id, type=etype, **fields))
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not record %s event for %s", etype, username)


def _is_enabled(username: str) -> bool:
    with Session(engine) as session:
        acc = session.exec(select(Account).where(Account.username == username)).first()
        return bool(acc and acc.enabled)


class MinerManager:
    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}
        self._started_at: dict[str, float] = {}      # username -> monotonic start time
        self._fail_streak: dict[str, int] = {}       # username -> consecutive fast crashes
        self._epoch: dict[str, int] = {}             # username -> lifecycle generation
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    # ---- lifecycle ----
    def start_reaper(self) -> None:
        if self._reaper and self._reaper.is_alive():
            return
        self._stop.clear()
        self._reaper = threading.Thread(target=self._reap_loop, name="reaper", daemon=True)
        self._reaper.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            usernames = list(self._procs)
        for u in usernames:
            self.stop(u)

    # ---- per-account control ----
    def is_running(self, username: str) -> bool:
        with self._lock:
            proc = self._procs.get(username)
            return proc is not None and proc.poll() is None

    def start(self, username: str) -> bool:
        """Spawn a miner for the account. Returns False if already running."""
        with self._lock:
            if self.is_running(username):
                return False

            config.ensure_dirs()
            log_path = config.LOGS_DIR / f"{username}.log"
            logf = open(log_path, "ab", buffering=0)

            env = dict(os.environ)
            env["INTERNAL_TOKEN"] = config.get_internal_token()
            env["BACKEND_URL"] = config.BACKEND_URL
            env["DATA_DIR"] = str(config.DATA_DIR)
            env["TWITCH_USERNAME"] = username
            # Make the miner_runner module + TwitchChannelPointsMiner importable
            # regardless of cwd (we run in DATA_DIR so cookies/logs land there).
            existing_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                str(config.PROJECT_ROOT) + (os.pathsep + existing_pp if existing_pp else "")
            )

            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "miner_runner", username],
                cwd=str(config.DATA_DIR),
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,  # own process group -> clean group kill
            )
            self._procs[username] = proc
            self._started_at[username] = time.monotonic()
            self._epoch[username] = self._epoch.get(username, 0) + 1

        _set_status(username, "starting")
        return True

    def stop(self, username: str, timeout: float = 10.0) -> bool:
        """Terminate the miner (SIGTERM -> wait -> SIGKILL). False if not running.

        Always bumps the lifecycle epoch and clears the failure streak so any
        pending auto-restart timer for this account is cancelled (a deliberate
        stop must win over a scheduled crash-restart).
        """
        with self._lock:
            self._epoch[username] = self._epoch.get(username, 0) + 1
            self._fail_streak.pop(username, None)
            self._started_at.pop(username, None)
            proc = self._procs.get(username)
            if proc is None or proc.poll() is not None:
                self._procs.pop(username, None)
                return False

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

        with self._lock:
            self._procs.pop(username, None)
        _set_status(username, "stopped")
        return True

    def restart(self, username: str) -> bool:
        self.stop(username)
        return self.start(username)

    # ---- bulk control ----
    def start_all(self) -> list[str]:
        started = []
        with Session(engine) as session:
            accounts = session.exec(select(Account).where(Account.enabled == True)).all()  # noqa: E712
            usernames = [a.username for a in accounts]
        for u in usernames:
            if self.start(u):
                started.append(u)
        return started

    def stop_all(self) -> list[str]:
        with self._lock:
            usernames = list(self._procs)
        return [u for u in usernames if self.stop(u)]

    def statuses(self) -> dict[str, bool]:
        with self._lock:
            return {u: (p.poll() is None) for u, p in self._procs.items()}

    def running_uptimes(self) -> dict[str, float]:
        """username -> seconds running, for each currently-alive miner."""
        now = time.monotonic()
        with self._lock:
            return {
                u: now - self._started_at.get(u, now)
                for u, p in self._procs.items()
                if p.poll() is None
            }

    # ---- reaper / watchdog ----
    def _reap_loop(self) -> None:
        last_hb = 0.0
        while not self._stop.wait(3.0):
            self._reap_exited()
            now = time.monotonic()
            if config.MINER_HEARTBEAT_ENABLED and now - last_hb >= config.MINER_HEARTBEAT_INTERVAL:
                last_hb = now
                try:
                    self._heartbeat_check()
                except Exception:  # noqa: BLE001
                    logger.exception("heartbeat check failed")

    def _reap_exited(self) -> None:
        """Detect subprocesses that exited on their own and (maybe) auto-restart."""
        with self._lock:
            items = list(self._procs.items())
        for username, proc in items:
            code = proc.poll()
            if code is None:
                continue
            with self._lock:
                started = self._started_at.pop(username, None)
                self._procs.pop(username, None)
            uptime = time.monotonic() - started if started is not None else 0.0
            if self._stop.is_set():
                _set_status(username, "stopped" if code == 0 else "error")
                continue
            self._maybe_autorestart(username, code, uptime)

    def _maybe_autorestart(self, username: str, code: int, uptime: float) -> None:
        """Schedule a backoff restart for an enabled account that crashed."""
        if not config.MINER_AUTORESTART_ENABLED or not _is_enabled(username):
            with self._lock:
                self._fail_streak.pop(username, None)
            _set_status(username, "stopped" if code == 0 else "error")
            return

        with self._lock:
            # A run that stayed up long enough counts as healthy -> reset streak.
            if uptime >= config.MINER_HEALTHY_UPTIME:
                self._fail_streak[username] = 0
            n = self._fail_streak.get(username, 0) + 1
            self._fail_streak[username] = n
            epoch = self._epoch.get(username, 0)

        delay = min(
            config.MINER_RESTART_BACKOFF_MAX,
            config.MINER_RESTART_BACKOFF_BASE * (2 ** (n - 1)),
        )
        msg = (f"exit code {code} after {int(uptime)}s; "
               f"auto-restart #{n} in {int(delay)}s")
        logger.warning("[%s] crashed (%s)", username, msg)
        _set_status(username, "restarting")
        _record_event(username, "status", reason="restarting", message=msg)

        timer = threading.Timer(delay, self._do_autorestart, args=(username, epoch))
        timer.daemon = True
        timer.start()

    def _do_autorestart(self, username: str, epoch: int) -> None:
        if self._stop.is_set():
            return
        with self._lock:
            # Any start()/stop() since scheduling bumped the epoch -> superseded.
            if self._epoch.get(username, 0) != epoch:
                return
        if not _is_enabled(username) or self.is_running(username):
            return
        logger.info("[%s] auto-restarting now", username)
        self.start(username)

    def _heartbeat_check(self) -> None:
        """Restart running miners that have gone silent (alive but not mining)."""
        now = time.monotonic()
        with self._lock:
            running = [
                u for u, p in self._procs.items()
                if p.poll() is None
                and now - self._started_at.get(u, now) >= config.MINER_HEARTBEAT_GRACE
            ]
        if not running:
            return

        cutoff = utcnow() - timedelta(seconds=config.MINER_HEARTBEAT_TIMEOUT)
        stale: list[str] = []
        with Session(engine) as session:
            for username in running:
                acc = session.exec(
                    select(Account).where(Account.username == username)
                ).first()
                # Only accounts that announced "running" are expected to emit a
                # steady heartbeat. Skip ones still logging in / needing a login
                # so we never restart-loop an account that can't run yet.
                if acc is None or acc.status != "running":
                    continue
                # Any event at all within the window means the miner is alive.
                recent = session.exec(
                    select(Event.id)
                    .where(Event.account_id == acc.id)
                    .where(Event.ts >= cutoff)
                    .limit(1)
                ).first()
                if recent is None:
                    stale.append(username)

        for username in stale:
            msg = f"no activity for >{config.MINER_HEARTBEAT_TIMEOUT}s (hung) -> restart"
            logger.warning("[%s] %s", username, msg)
            _record_event(username, "status", reason="restarting", message=msg)
            threading.Thread(
                target=self.restart, args=(username,),
                name=f"hb-restart-{username}", daemon=True,
            ).start()


# Module-level singleton used by the API.
manager = MinerManager()
