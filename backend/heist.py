# -*- coding: utf-8 -*-
"""Heist module core: chat IRC plumbing, config and per-account cooldowns.

A streamer's chat bot runs a "heist" mini-game: someone types a start command
(e.g. `!heist`) and the bot announces an open heist that others join with `!join`
to win points. This module lets us

  * open heists with a rotating pool of "opener" accounts (each on a long
    per-account cooldown the bot enforces, spaced apart in time), and
  * have one or more "joiner" accounts (typically just the main) jump on every
    open heist as fast as possible so only they collect the loot.

Like ``backend/redeem.py`` this runs inside the backend process (not the miner
subprocesses): the OAuth token comes from each account's stored login cookie and
the IRC socket is routed through that account's assigned proxy. The long-lived
coordinator that drives it lives in ``backend/heist_manager.py``.
"""
import json
import logging
import threading
import time

from irc.bot import SingleServerIRCBot
from irc.connection import Factory
from sqlmodel import Session, select

from backend import redeem
from backend.models import Account, AppSetting, Proxy
from backend.proxy_util import to_engine_proxy

logger = logging.getLogger("backend.heist")

IRC_SERVER = "irc.chat.twitch.tv"
IRC_PORT = 6667

# ---- persisted config (AppSetting keys) ----
ENABLED_KEY = "HEIST_ENABLED"
CHANNEL_KEY = "HEIST_CHANNEL"               # streamer login the heist runs in
BOT_KEY = "HEIST_BOT"                       # chat bot whose messages we react to
TRIGGER_KEY = "HEIST_TRIGGER_REGEX"         # marks an OPEN heist in a bot message
END_KEY = "HEIST_END_REGEX"                 # marks a RESOLVED heist (optional)
REJECT_KEY = "HEIST_REJECT_REGEX"           # bot rejected an opener's !heist (optional)
START_CMD_KEY = "HEIST_START_COMMAND"
JOIN_CMD_KEY = "HEIST_JOIN_COMMAND"
START_COOLDOWN_KEY = "HEIST_START_COOLDOWN"  # per-account seconds between !heist
SPACING_MIN_KEY = "HEIST_SPACING_MIN"        # min seconds between two openers
SPACING_MAX_KEY = "HEIST_SPACING_MAX"        # max seconds between two openers
JOIN_DELAY_KEY = "HEIST_JOIN_DELAY_MS"       # delay before firing !join

# Built-in chat-message patterns for the j4nkttv heist bot (j4nkb0t), derived
# from a live chat capture. Used whenever the matching setting is left blank, so
# detection works out of the box; a non-empty setting still overrides them.
#   open:        "🚨Heist on <place>! 🚐N spots left | 🎯Loot: N Points | 👉!join"
#   end success: "<user> took N points from the !heist"
#   end failure: "💥 Heist on <place> failed! 💸 No loot."
# When an opener fires !heist but the bot WON'T start a new heist, it replies
# naming that account, e.g. (from a live capture):
#   "<user> - Heist is currently active"     (a heist is already running)
#   "@<user> wait 50s"                       (command rate-limit / on cooldown)
# The reject regex matches the *reason wording only*; the heist_manager pairs it
# with the pending opener's username so a !heist that was rejected never starts
# that account's long start-cooldown.
BUILTIN_TRIGGER_REGEX = r"Heist on .+spots left"
BUILTIN_END_REGEX = r"took .+ from the !heist|Heist on .+ failed|No loot"
BUILTIN_REJECT_REGEX = r"Heist is currently active|wait\s+\d+\s*s|on cooldown"

_DEFAULTS = {
    ENABLED_KEY: "0",
    CHANNEL_KEY: "",
    BOT_KEY: "",
    TRIGGER_KEY: BUILTIN_TRIGGER_REGEX,
    END_KEY: BUILTIN_END_REGEX,
    REJECT_KEY: BUILTIN_REJECT_REGEX,
    START_CMD_KEY: "!heist",
    JOIN_CMD_KEY: "!join",
    START_COOLDOWN_KEY: "3600",
    SPACING_MIN_KEY: "300",
    SPACING_MAX_KEY: "600",
    JOIN_DELAY_KEY: "300",
}


def _get_setting(session: Session, key: str) -> str:
    s = session.get(AppSetting, key)
    return s.value if s is not None else _DEFAULTS.get(key, "")


def set_setting(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key)
    if s is None:
        session.add(AppSetting(key=key, value=value))
    else:
        s.value = value
        session.add(s)


def _as_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_config(session: Session) -> dict:
    """Return the heist config as a typed dict (with defaults applied)."""
    g = lambda k: _get_setting(session, k)  # noqa: E731
    return {
        "enabled": g(ENABLED_KEY).strip().lower() in ("1", "true", "yes", "on"),
        "channel": (g(CHANNEL_KEY) or "").strip().lower(),
        "bot": (g(BOT_KEY) or "").strip().lower(),
        # Regexes fall back to the built-in patterns when left blank, so heist
        # detection works without configuration but stays overridable.
        "trigger_regex": g(TRIGGER_KEY).strip() or BUILTIN_TRIGGER_REGEX,
        "end_regex": g(END_KEY).strip() or BUILTIN_END_REGEX,
        "reject_regex": g(REJECT_KEY).strip() or BUILTIN_REJECT_REGEX,
        "start_command": (g(START_CMD_KEY) or _DEFAULTS[START_CMD_KEY]).strip(),
        "join_command": (g(JOIN_CMD_KEY) or _DEFAULTS[JOIN_CMD_KEY]).strip(),
        "start_cooldown": _as_float(g(START_COOLDOWN_KEY), 3600.0),
        "spacing_min": _as_float(g(SPACING_MIN_KEY), 300.0),
        "spacing_max": _as_float(g(SPACING_MAX_KEY), 600.0),
        "join_delay_ms": _as_float(g(JOIN_DELAY_KEY), 300.0),
    }


# ---- per-account start cooldown (persisted across restarts) ----
# The bot enforces a ~60-min cooldown per account between !heist starts. We must
# remember it across a container restart, otherwise every opener looks free again
# and we'd burn rejected !heist attempts. So we store the WALL-CLOCK expiry
# (time.time(), not monotonic) per account as JSON in an AppSetting, and reload it
# on startup. Wall-clock survives the process restart; monotonic would not.
COOLDOWNS_KEY = "HEIST_COOLDOWNS"   # JSON {account_id: expires_epoch}

_cooldowns: dict = {}        # account_id -> wall-clock epoch when !heist is allowed again
_cd_lock = threading.Lock()


def _persist_cooldowns() -> None:
    now = time.time()
    with _cd_lock:
        data = {str(aid): exp for aid, exp in _cooldowns.items() if exp > now}
    from backend.db import engine
    try:
        with Session(engine) as s:
            set_setting(s, COOLDOWNS_KEY, json.dumps(data))
            s.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not persist heist cooldowns")


def load_cooldowns() -> None:
    """Restore persisted cooldowns on startup (drops already-expired entries)."""
    from backend.db import engine
    try:
        with Session(engine) as s:
            raw = _get_setting(s, COOLDOWNS_KEY)
    except Exception:  # noqa: BLE001
        logger.exception("could not load heist cooldowns")
        return
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    now = time.time()
    restored = {}
    for aid, exp in data.items():
        try:
            exp = float(exp)
            aid = int(aid)
        except (TypeError, ValueError):
            continue
        if exp > now:
            restored[aid] = exp
    with _cd_lock:
        _cooldowns.clear()
        _cooldowns.update(restored)
    if restored:
        logger.info("heist: restored %d per-account cooldown(s) after restart",
                    len(restored))


def set_cooldown(account_id: int, seconds: float) -> None:
    if seconds <= 0:
        return
    with _cd_lock:
        _cooldowns[account_id] = time.time() + seconds
    _persist_cooldowns()


def clear_cooldown(account_id: int) -> None:
    """Drop an account's start cooldown (e.g. a !heist that turned out rejected)."""
    with _cd_lock:
        existed = _cooldowns.pop(account_id, None) is not None
    if existed:
        _persist_cooldowns()


def available_at(account_id: int) -> float:
    """Wall-clock epoch when this account may !heist again (0 = now)."""
    with _cd_lock:
        return _cooldowns.get(account_id, 0.0)


def cooldown_remaining(account_id: int) -> float:
    return max(0.0, available_at(account_id) - time.time())


def active_cooldowns() -> list:
    """Snapshot of currently-active per-account cooldowns (remaining > 0s)."""
    now = time.time()
    with _cd_lock:
        items = list(_cooldowns.items())
    return [
        {"account_id": aid, "remaining": round(until - now, 1)}
        for aid, until in items
        if until - now > 0
    ]


# ---- account credentials (token from cookie + engine proxy) ----
def load_heist_accounts(session: Session):
    """Return (openers, joiners) lists of usable account records.

    Only accounts that have a stored auth-token (logged in) are returned. Roles
    come from the per-account heist_opener / heist_joiner flags and are
    independent of the mining `enabled` flag (a join-only main need not mine).
    """
    openers, joiners = [], []
    for a in session.exec(select(Account)).all():
        if not (a.heist_opener or a.heist_joiner):
            continue
        token = redeem.account_auth_token(a.username)
        if not token:
            continue
        ep = to_engine_proxy(session.get(Proxy, a.proxy_id)) if a.proxy_id else None
        rec = {"id": a.id, "username": a.username, "token": token, "proxy": ep}
        if a.heist_opener:
            openers.append(rec)
        if a.heist_joiner:
            joiners.append(rec)
    return openers, joiners


# ---- stream online check (reuse redeem's GQL plumbing) ----
_STREAM_QUERY = """
query HeistStreamCheck($login: String!) {
  user(login: $login) { stream { id } }
}"""


def stream_online(channel: str, token: str, proxies) -> "bool | None":
    """True/False if the channel is live, or None if the check itself failed."""
    try:
        data = redeem._gql(token, proxies, "HeistStreamCheck", _STREAM_QUERY,
                           {"login": channel})
    except redeem.RedeemError as e:
        logger.debug("stream_online check failed for %s: %s", channel, e)
        return None
    user = data.get("user")
    if not user:
        return False
    return user.get("stream") is not None


# ---- IRC: proxy-routed connect factory ----
def _socks_connect_factory(engine_proxy):
    """Build an irc connect_factory that routes the TCP socket through a proxy.

    Without a proxy we still wrap the direct connect with a timeout, so an
    unreachable route (e.g. a tunnel killswitch) fails fast instead of hanging
    the "connecting…" state on the OS-default multi-minute TCP timeout.
    """
    if engine_proxy is None:
        import socket

        def direct_factory(server_address):
            host, port = server_address
            sock = socket.create_connection((host, int(port)), timeout=20)
            sock.settimeout(None)
            return sock

        return direct_factory

    import socks  # from PySocks (requests[socks]); also covers python-socks install

    proxy_types = {
        "http": socks.HTTP, "https": socks.HTTP,
        "socks4": socks.SOCKS4, "socks4a": socks.SOCKS4,
        "socks5": socks.SOCKS5, "socks5h": socks.SOCKS5,
    }
    ptype = proxy_types[engine_proxy.scheme]
    # *a / *h schemes resolve the destination host on the proxy side.
    rdns = engine_proxy.scheme in ("socks4a", "socks5h")

    def factory(server_address):
        host, port = server_address
        sock = socks.socksocket()
        sock.set_proxy(
            ptype, engine_proxy.host, int(engine_proxy.port), rdns=rdns,
            username=engine_proxy.username or None,
            password=engine_proxy.password or None,
        )
        sock.settimeout(20)
        sock.connect((host, int(port)))
        sock.settimeout(None)
        return sock

    return factory


class HeistIRC(SingleServerIRCBot):
    """A single Twitch IRC connection for one account, proxy-routed.

    Used both as the persistent observer/joiner (reads the bot's messages and
    fires `!join`) and as a throwaway opener connection (connects, sends
    `!heist`, disconnects). Incoming public messages are forwarded to the
    optional ``on_message(nick, text)`` callback.
    """

    def __init__(self, username, token, channel, engine_proxy=None, on_message=None):
        self.channel = "#" + channel.lower()
        self._on_message = on_message
        self.joined = threading.Event()
        self._active = False
        # surfaced to callers for diagnostics when a connection never joins
        self.connect_error: "str | None" = None
        self.notice_error: "str | None" = None
        super().__init__(
            [(IRC_SERVER, IRC_PORT, f"oauth:{token}")], username, username,
            connect_factory=_socks_connect_factory(engine_proxy),
        )

    # ---- irc.bot event handlers ----
    def on_welcome(self, connection, event):
        connection.join(self.channel)

    def on_join(self, connection, event):
        self.joined.set()

    def _capture_notice(self, event):
        """Twitch rejects a bad/expired oauth with a NOTICE (e.g. 'Login
        authentication failed'); record it so the UI can explain the failure."""
        text = event.arguments[0] if event.arguments else ""
        if text and ("authentication failed" in text.lower()
                     or "improperly formatted" in text.lower()
                     or "login unsuccessful" in text.lower()):
            self.notice_error = text

    # Twitch sends login/ban rejections as a NOTICE to target "*", which the irc
    # library dispatches as a *privnotice* (the target isn't a channel) — so
    # on_privnotice is the one that actually fires; the others are belt-and-braces.
    def on_privnotice(self, connection, event):
        self._capture_notice(event)

    def on_notice(self, connection, event):
        self._capture_notice(event)

    def on_pubnotice(self, connection, event):
        self._capture_notice(event)

    def on_pubmsg(self, connection, event):
        if self._on_message is None:
            return
        try:
            nick = event.source.nick
        except AttributeError:
            nick = str(event.source).split("!", 1)[0]
        msg = event.arguments[0] if event.arguments else ""
        try:
            self._on_message(nick, msg)
        except Exception:  # noqa: BLE001
            logger.exception("heist on_message handler raised")

    # ---- helpers ----
    def send(self, text: str) -> None:
        self.connection.privmsg(self.channel, text)

    def start(self):
        """Blocking reactor loop (run in a dedicated thread)."""
        self._active = True
        try:
            self._connect()
        except Exception as e:  # noqa: BLE001
            logger.warning("heist IRC connect failed (%s): %s", self.channel, e)
            self.connect_error = str(e) or e.__class__.__name__
            self._active = False
            return
        while self._active:
            try:
                self.reactor.process_once(timeout=0.2)
                time.sleep(0.01)
            except Exception as e:  # noqa: BLE001
                logger.error("heist IRC loop error (%s): %s", self.channel, e)

    def die(self, msg="bye"):
        self._active = False
        try:
            self.connection.disconnect(msg)
        except Exception:  # noqa: BLE001
            pass


def fire_heist(record: dict, channel: str, start_command: str,
               connect_timeout: float = 15.0, linger: float = 6.0) -> bool:
    """Open one heist with an opener account via a short-lived IRC connection.

    Connects, waits until joined to the channel, sends the start command, lingers
    briefly so the message is flushed and the bot can react, then disconnects.
    Returns True if the command was sent.
    """
    client = HeistIRC(record["username"], record["token"], channel, record["proxy"])
    t = threading.Thread(target=client.start, name=f"heist-open-{record['username']}",
                         daemon=True)
    t.start()
    sent = False
    if client.joined.wait(timeout=connect_timeout):
        try:
            client.send(start_command)
            sent = True
        except Exception as e:  # noqa: BLE001
            logger.warning("heist !heist send failed for %s: %s", record["username"], e)
        time.sleep(linger)
    else:
        logger.warning("heist opener %s could not join %s within %.0fs",
                       record["username"], channel, connect_timeout)
    client.die()
    t.join(timeout=5)
    return sent
