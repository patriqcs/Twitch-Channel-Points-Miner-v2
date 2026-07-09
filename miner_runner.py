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
import json
import logging
import os
import sys
import threading
import time

import requests

from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.logger import LoggerSettings
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Priority, FollowersOrder
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
HEADERS = {"X-Internal-Token": INTERNAL_TOKEN}

# Auto-follow a channel once an account becomes subscribed to it. Subscribers
# can follow even when a channel requires phone verification, and the sub shows
# up as a SUB_* points multiplier the miner already loads — so we piggyback on
# that signal and fire a one-time follow through the account's proxy + token.
AUTO_FOLLOW_ON_SUB = os.environ.get("AUTO_FOLLOW_ON_SUB", "1").strip().lower() in (
    "1", "true", "yes", "on"
)
FOLLOW_RETRY_COOLDOWN = float(os.environ.get("AUTO_FOLLOW_RETRY_COOLDOWN", "1800"))
# Raw mutation (the miner's TV client-id needs no integrity header for this).
FOLLOW_MUTATION = (
    "mutation FollowUser($input: FollowUserInput!){"
    "followUser(input:$input){follow{followedAt user{login}} error{code}}}"
)

logger = logging.getLogger("miner_runner")


# ------------------------------------------------------ auto-follow persistence
def _follow_state_path(username: str) -> str:
    base = os.environ.get("DATA_DIR") or os.getcwd()
    return os.path.join(base, "auto_follow", f"{username}.json")


def _load_followed(username: str) -> set:
    """Channel ids this account has already been auto-followed to (persisted)."""
    try:
        with open(_follow_state_path(username), encoding="utf-8") as f:
            return {str(x) for x in json.load(f)}
    except (OSError, ValueError):
        return set()


def _save_followed(username: str, followed: set) -> None:
    path = _follow_state_path(username)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(followed), f)
        os.replace(tmp, path)
    except OSError as e:  # noqa: BLE001
        logger.warning("could not persist auto-follow state for %s: %s", username, e)


# ---------------------------------------------------------------- backend I/O
def fetch_config(username: str) -> dict:
    """Get streamers + proxy from the backend.

    Under the backend (INTERNAL_TOKEN set) the backend is the source of truth
    for BOTH streamers and the assigned proxy, so we retry hard on a transient
    failure and refuse to start rather than silently falling back to ENV — a
    fallback would drop the account's proxy and mine from the real host IP,
    linking the accounts. Only in standalone mode do we use the ENV/files
    fallback (the operator's explicit choice).
    """
    url = f"{BACKEND_URL}/internal/config/{username}"
    if INTERNAL_TOKEN:
        last_err = None
        for attempt in range(6):
            try:
                r = requests.get(url, headers=HEADERS, timeout=10)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.RequestException as e:
                last_err = e
                time.sleep(min(2 ** attempt, 20))
        logger.error(
            "Config fetch failed after retries (%s); refusing to start "
            "unproxied to avoid leaking the real IP.", last_err
        )
        report(username, "status", reason="error",
               message="backend config unavailable")
        sys.exit(1)
    # Standalone mode (no backend): ENV/files fallback.
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
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


# ------------------------------------------------------- proxy error watcher
class ProxyErrorReporter(logging.Handler):
    """Watches the miner's ERROR logs for proxy/connection failures and reports
    a 'proxy_error' event to the backend so the health monitor can fail over.

    Only relevant when a proxy is set. Rate-limited: needs THRESHOLD matching
    errors within WINDOW seconds, then at most one report per COOLDOWN seconds.
    """

    MARKERS = (
        "SOCKSHTTPSConnectionPool",
        "SOCKSConnectionPool",
        "Max retries exceeded",
        "Failed to establish a new connection",
        "Connection refused",
        "Connection reset",
        "ProxyError",
        "Unable to connect to proxy",
        "Remote end closed connection",
    )

    def __init__(self, username: str, threshold: int = 3, window: float = 120.0,
                 cooldown: float = 60.0):
        super().__init__(level=logging.ERROR)
        self.username = username
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self._hits: list[float] = []
        self._last_report = 0.0
        self._episode = False  # inside a sustained-error episode?

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if not any(m in msg for m in self.MARKERS):
            return
        now = time.monotonic()
        recent = [t for t in self._hits if now - t <= self.window]
        # If the window was quiet since the last error, the previous episode is
        # over and a fresh one must re-earn the threshold.
        if not recent:
            self._episode = False
        recent.append(now)
        self._hits = recent
        if (now - self._last_report) < self.cooldown:
            return
        # Fire on an initial burst (threshold), then KEEP nudging the monitor
        # every cooldown while errors persist. We must NOT clear self._hits here:
        # doing so made the very next error see an empty window, reset _episode,
        # and permanently silence the "keep nudging" path for a slow trickle.
        # Pruning to `window` on each emit already bounds the list.
        if len(self._hits) >= self.threshold or self._episode:
            self._last_report = now
            self._episode = True
            report(self.username, "proxy_error", message=msg[:200])


# --------------------------------------------------------- ban signal watcher
class BanSignalReporter(logging.Handler):
    """Watches the miner's logs for account-level ban signals and reports a
    'ban_signal' event so the backend can auto-pull the account instead of
    hammering endless BADAUTH reconnects (which only confirm the detection).

    Two signals:
      * PubSub close reason "security" -> Twitch flagged/banned the account
        (the ban-wave signature). Strong -> backend disables the account.
      * ERR_BADAUTH -> the auth token is invalid (banned OR just an expired
        cookie). Backend stops the process + marks needs_login (recoverable).
    Reports once, then rate-limited, so a reconnect storm is not amplified.
    """

    def __init__(self, username: str, cooldown: float = 300.0):
        super().__init__(level=logging.WARNING)
        self.username = username
        self.cooldown = cooldown
        self._last = {}  # reason -> monotonic time of last report

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        low = msg.lower()
        reason = None
        if "reason=security" in low or "\\x10hsecurity" in low or "'security'" in low:
            reason = "security"
        elif "err_badauth" in low:
            reason = "badauth"
        if reason is None:
            return
        now = time.monotonic()
        if now - self._last.get(reason, 0.0) < self.cooldown:
            return
        self._last[reason] = now
        report(self.username, "ban_signal", reason=reason, message=msg[:200])


# ---------------------------------------------------------------- reporter
class Reporter(threading.Thread):
    """Posts 'running'/'login' once streamers load, then periodic point totals.

    The point totals double as the data source for the backend's peer watchdog:
    since every account watches the same streamers, the backend can compare the
    point progress across accounts and spot one that earns nothing while its
    peers do (a half-broken proxy: online via PubSub, but failing watch POSTs).
    """

    def __init__(self, username: str, miner, interval: int = 60):
        super().__init__(name="reporter", daemon=True)
        self.username = username
        self.miner = miner
        self.interval = interval
        self._stop = threading.Event()
        self._announced = False
        # auto-follow-on-sub state
        self._followed = _load_followed(username) if AUTO_FOLLOW_ON_SUB else set()
        self._follow_retry: dict[str, float] = {}  # channel_id -> next retry (monotonic)

    def run(self):
        while not self._stop.is_set():
            streamers = getattr(self.miner, "streamers", None) or []
            if streamers:
                if not self._announced:
                    self._announced = True
                    report(self.username, "status", reason="running")
                    report(self.username, "login")
                # Nur die Punkte der FARM-Streamer (j4nkttv) melden — die
                # Tarn-Kanäle sind fürs Dashboard irrelevant. Fallback (kein
                # Farm-Set, z.B. Standalone): Summe über alle Streamer.
                farm = getattr(getattr(self.miner, "twitch", None),
                               "protected_streamers", None) or set()
                if farm:
                    total = sum(
                        int(getattr(s, "channel_points", 0) or 0)
                        for s in streamers
                        if str(getattr(s, "username", "")).lower() in farm
                    )
                else:
                    total = sum(int(getattr(s, "channel_points", 0) or 0)
                                for s in streamers)
                report(self.username, "points_snapshot", balance=total)
                if AUTO_FOLLOW_ON_SUB:
                    try:
                        self._auto_follow_subscribed(streamers)
                    except Exception:  # noqa: BLE001
                        logger.exception("auto-follow check failed")
            self._stop.wait(self.interval)

    # ---- auto-follow on subscription ----
    @staticmethod
    def _is_subscribed(streamer) -> bool:
        """A SUB_* points multiplier means this account is subscribed here."""
        for m in (getattr(streamer, "activeMultipliers", None) or []):
            if isinstance(m, dict) and str(m.get("reasonCode", "")).startswith("SUB_"):
                return True
        return False

    def _auto_follow_subscribed(self, streamers) -> None:
        now = time.monotonic()
        for s in streamers:
            cid = getattr(s, "channel_id", None)
            if not cid:
                continue
            cid = str(cid)
            if cid in self._followed or not self._is_subscribed(s):
                continue
            if now < self._follow_retry.get(cid, 0.0):
                continue  # backing off after a recent failure
            login = str(getattr(s, "username", "?"))
            ok, err = self._do_follow(cid)
            if ok:
                self._followed.add(cid)
                _save_followed(self.username, self._followed)
                logger.info("[%s] auto-followed %s after subscription", self.username, login)
                report(self.username, "follow", streamer=login,
                       message=f"auto-followed after subscription ({login})")
            else:
                self._follow_retry[cid] = now + FOLLOW_RETRY_COOLDOWN
                logger.warning("[%s] auto-follow of %s failed: %s", self.username, login, err)
                report(self.username, "follow_failed", streamer=login, message=str(err)[:120])

    def _do_follow(self, channel_id: str):
        """Fire followUser through the miner's authenticated GQL (proxy + token).
        Returns (ok, error_or_None)."""
        twitch = getattr(self.miner, "twitch", None)
        if twitch is None:
            return False, "no twitch client"
        payload = {
            "operationName": "FollowUser",
            "query": FOLLOW_MUTATION,
            "variables": {"input": {"targetID": str(channel_id),
                                    "disableNotifications": True}},
        }
        try:
            resp = twitch.post_gql_request(payload)
        except Exception as e:  # noqa: BLE001
            return False, f"request error: {e}"
        if not resp:
            return False, "empty response (network/proxy)"
        if resp.get("errors"):
            return False, str(resp["errors"][0].get("message", "gql error"))
        fu = (resp.get("data") or {}).get("followUser")
        if isinstance(fu, dict) and fu.get("error"):
            return False, str(fu["error"].get("code") or "unknown error")
        if isinstance(fu, dict) and fu.get("follow"):
            return True, None
        return False, "unexpected response"

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
    # Persistent per-account client fingerprint from the backend. Absent in the
    # standalone/ENV fallback -> the miner generates a coherent default itself.
    device_id = cfg.get("device_id")
    ua_app = cfg.get("ua_app")
    ua_web = cfg.get("ua_web")
    # Expose account age to the engine (behavioural warm-up for new accounts).
    # The engine reads MINER_ACCOUNT_AGE_DAYS via env in its jitter/bet helpers.
    age_days = cfg.get("account_age_days")
    if age_days is not None:
        os.environ["MINER_ACCOUNT_AGE_DAYS"] = str(age_days)
    if not streamer_names:
        report(username, "status", reason="error", message="no streamers configured")
        print("No streamers configured.")
        sys.exit(1)

    # No cookie yet -> needs a device-code login (handled via the backend UI).
    # Honor COOKIES_DIR the same way the backend does; otherwise the miner would
    # look in cwd/cookies while the backend saved the cookie elsewhere, so a
    # freshly-logged-in account would still report needs_login and never mine.
    cookies_dir = os.environ.get("COOKIES_DIR") or os.path.join(os.getcwd(), "cookies")
    cookie_file = os.path.join(cookies_dir, f"{username}.pkl")
    has_cookie = os.path.isfile(cookie_file)
    if not has_cookie:
        report(username, "status", reason="needs_login")

    miner = TwitchChannelPointsMiner(
        username=username,
        proxy=proxy,
        user_agent=ua_app,
        web_user_agent=ua_web,
        device_id=device_id,
        claim_drops_startup=True,
        priority=[Priority.STREAK, Priority.DROPS, Priority.ORDER],
        enable_analytics=False,
        logger_settings=LoggerSettings(
            # The MinerManager already redirects this process's stdout to
            # LOGS_DIR/<username>.log, so the library's own file logging is a
            # redundant SECOND open handle on the same file (plus daily-rotated
            # backups). Disable it to avoid doubling open file handles / log files.
            save=False,
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

    # Farm-Streamer schützen: sie behalten im Watch-Loop immer einen der 2 Slots,
    # damit Tarn-Kanäle (mit frischer Watch-Streak) ihnen den Slot nie klauen.
    farm_streamers = {s.lower() for s in (cfg.get("farm_streamers") or [])}
    if farm_streamers and getattr(miner, "twitch", None) is not None:
        miner.twitch.protected_streamers = farm_streamers

    reporter = Reporter(username, miner)
    reporter.start()

    # If mining through a proxy, watch the miner's logs for connection failures
    # and report them so the backend health monitor can fail over to another proxy.
    if proxy and INTERNAL_TOKEN:
        logging.getLogger().addHandler(ProxyErrorReporter(username))

    # Watch for account-level ban signals (PubSub "security" close / ERR_BADAUTH)
    # so the backend can auto-pull the account instead of reconnect-storming.
    if INTERNAL_TOKEN:
        logging.getLogger().addHandler(BanSignalReporter(username))

    # Do NOT overwrite a "needs_login" status with "starting": the heartbeat
    # watchdog treats "starting" as a stalled startup and restarts the account
    # every GRACE, so a login-required account would loop forever (and the UI
    # would never show that a login is needed). Only announce "starting" when we
    # actually have a cookie to mine with.
    if has_cookie:
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
