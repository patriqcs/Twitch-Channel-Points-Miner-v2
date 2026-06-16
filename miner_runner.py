# -*- coding: utf-8 -*-
"""Run ONE Twitch account, managed by the web backend.

Lifecycle:
  1. Fetch this account's config (streamers + proxy) from the backend's internal
     API. If the backend is unreachable, fall back to ENV/files (standalone mode).
  2. Start the miner (watch-only: points / streaks / drops / moments, NO bets).
  3. Report status + periodic points snapshots back to the backend so the
     dashboard has live data.

Username: ENV TWITCH_USERNAME or argv[1].
Backend:  ENV BACKEND_URL + INTERNAL_TOKEN (injected by MinerManager).
"""
import logging
import os
import sys
import threading

import requests

from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.logger import LoggerSettings
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Priority, FollowersOrder
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
HEADERS = {"X-Internal-Token": INTERNAL_TOKEN}

logger = logging.getLogger("miner_runner")


# ---------------------------------------------------------------- backend I/O
def fetch_config(username: str) -> dict:
    """Get streamers + proxy from the backend. Fall back to ENV/files."""
    try:
        r = requests.get(
            f"{BACKEND_URL}/internal/config/{username}", headers=HEADERS, timeout=10
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        logger.warning("Config fetch failed (%s); falling back to ENV/files.", e)
        return {"username": username, "streamers": _streamers_fallback(), "proxy": os.environ.get("PROXY") or None}


def _streamers_fallback() -> list:
    env = os.environ.get("STREAMERS", "").strip()
    if env:
        return [s.strip() for s in env.replace("\n", ",").replace(" ", ",").split(",") if s.strip()]
    path = os.path.join(os.getcwd(), "streamers.txt")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return []


def report(username: str, etype: str, **fields) -> None:
    """Best-effort event POST. Never breaks mining on failure."""
    if not INTERNAL_TOKEN:
        return
    try:
        requests.post(
            f"{BACKEND_URL}/internal/events",
            json={"username": username, "type": etype, **fields},
            headers=HEADERS,
            timeout=5,
        )
    except requests.exceptions.RequestException:
        pass


# ---------------------------------------------------------------- reporter
class Reporter(threading.Thread):
    """Posts 'running'/'login' once streamers load, then periodic point totals."""

    def __init__(self, username: str, miner, interval: int = 60):
        super().__init__(name="reporter", daemon=True)
        self.username = username
        self.miner = miner
        self.interval = interval
        self._stop = threading.Event()
        self._announced = False

    def run(self):
        while not self._stop.is_set():
            streamers = getattr(self.miner, "streamers", None) or []
            if streamers:
                if not self._announced:
                    self._announced = True
                    report(self.username, "status", reason="running")
                    report(self.username, "login")
                total = sum(int(getattr(s, "channel_points", 0) or 0) for s in streamers)
                report(self.username, "points_snapshot", balance=total)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------- main
def main():
    username = (
        os.environ.get("TWITCH_USERNAME", "").strip()
        or (sys.argv[1].strip() if len(sys.argv) > 1 else "")
    )
    if not username:
        print("No username. Set TWITCH_USERNAME or pass it as argument.")
        sys.exit(1)

    cfg = fetch_config(username)
    streamer_names = cfg.get("streamers") or []
    proxy = cfg.get("proxy")
    if not streamer_names:
        report(username, "status", reason="error", message="no streamers configured")
        print("No streamers configured.")
        sys.exit(1)

    # No cookie yet -> needs an interactive device-code login (Phase 4 handles it).
    cookie_file = os.path.join(os.getcwd(), "cookies", f"{username}.pkl")
    if not os.path.isfile(cookie_file):
        report(username, "status", reason="needs_login")

    miner = TwitchChannelPointsMiner(
        username=username,
        proxy=proxy,
        claim_drops_startup=True,
        priority=[Priority.STREAK, Priority.DROPS, Priority.ORDER],
        enable_analytics=False,
        logger_settings=LoggerSettings(
            save=True,
            console_level=logging.INFO,
            console_username=True,
            file_level=logging.INFO,
            emoji=True,
            less=False,
            colored=True,
        ),
        streamer_settings=StreamerSettings(
            make_predictions=False,   # never bet
            follow_raid=True,
            claim_drops=True,
            claim_moments=True,
            watch_streak=True,
            community_goals=False,
            chat=ChatPresence.ONLINE,
        ),
    )

    reporter = Reporter(username, miner)
    reporter.start()
    report(username, "status", reason="starting")
    try:
        miner.mine([Streamer(n) for n in streamer_names], followers=False,
                   followers_order=FollowersOrder.ASC)
    except SystemExit:
        report(username, "status", reason="stopped")
        raise
    except Exception as e:  # noqa: BLE001
        report(username, "status", reason="error", message=str(e))
        raise
    finally:
        reporter.stop()


if __name__ == "__main__":
    main()
