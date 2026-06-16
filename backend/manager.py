# -*- coding: utf-8 -*-
"""Process manager: one OS subprocess per account, each running miner_runner.

Each miner runs isolated (a crash of one account never takes down the others).
Subprocess stdout/stderr is appended to LOGS_DIR/<username>.log (live-tailed in
Phase 5). A background reaper updates account status when a process exits.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from sqlmodel import Session, select

from backend import config
from backend.db import engine
from backend.models import Account


def _set_status(username: str, status: str) -> None:
    with Session(engine) as session:
        acc = session.exec(select(Account).where(Account.username == username)).first()
        if acc is not None:
            acc.status = status
            session.add(acc)
            session.commit()


class MinerManager:
    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}
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

        _set_status(username, "starting")
        return True

    def stop(self, username: str, timeout: float = 10.0) -> bool:
        """Terminate the miner (SIGTERM -> wait -> SIGKILL). False if not running."""
        with self._lock:
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

    # ---- reaper ----
    def _reap_loop(self) -> None:
        while not self._stop.wait(3.0):
            with self._lock:
                items = list(self._procs.items())
            for username, proc in items:
                code = proc.poll()
                if code is None:
                    continue
                # Process exited on its own -> reflect it in the DB and drop it.
                with self._lock:
                    self._procs.pop(username, None)
                _set_status(username, "stopped" if code == 0 else "error")


# Module-level singleton used by the API.
manager = MinerManager()
