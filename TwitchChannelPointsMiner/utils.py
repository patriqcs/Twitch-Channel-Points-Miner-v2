import platform
import re
import socket
import string
import time
from copy import deepcopy
from datetime import datetime, timezone
from os import environ, path
from random import choice, randrange, uniform
from threading import Timer

import requests
from millify import millify

from TwitchChannelPointsMiner.constants import (
    TV_APP_USER_AGENTS,
    USER_AGENTS,
    WEB_USER_AGENTS,
    GITHUB_url,
)


# --- Per-account fingerprint generators -----------------------------------
# Called once when an account is created (backend/models.py default_factory)
# and the result is PERSISTED in the DB, so every account keeps one stable,
# coherent device identity for its whole lifetime (survives restarts and
# renames). See constants.TV_APP_USER_AGENTS / WEB_USER_AGENTS for the pools.

def new_device_id() -> str:
    """A fresh 32-char alphanumeric X-Device-Id (one per account, persisted)."""
    return "".join(choice(string.ascii_letters + string.digits) for _ in range(32))


def new_app_user_agent() -> str:
    """A random Android-TV app User-Agent (consistent with the TV CLIENT_ID)."""
    return choice(TV_APP_USER_AGENTS)


def new_web_user_agent() -> str:
    """A random desktop-browser User-Agent for the spade/web-page scrape."""
    return choice(WEB_USER_AGENTS)


# --- Behavioural timing jitter ---------------------------------------------
# Real users don't click a claim/join the millisecond a PubSub event lands, and
# 25 accounts firing the same action at the same instant is a bot tell. These
# helpers spread the reactive actions out by a small random delay, run on a
# daemon Timer so the single-threaded WebSocket message handler never blocks.

def action_jitter(env_name: str, default_lo: float, default_hi: float) -> float:
    """Random human-like delay (seconds) to defer a reactive action.

    Overridable per action via env var `env_name` as "lo,hi" seconds; set it to
    "0,0" to disable that action's jitter. Each account draws independently, so
    the fleet de-synchronises without any shared coordination.
    """
    lo, hi = default_lo, default_hi
    raw = environ.get(env_name, "")
    if raw:
        try:
            parts = [float(x) for x in raw.split(",")]
            if len(parts) == 2:
                lo, hi = parts
        except ValueError:
            pass
    if hi <= 0 or hi < lo:
        return 0.0
    return uniform(lo, hi)


def defer(delay: float, fn, *args) -> None:
    """Run fn(*args) after `delay` seconds on a daemon timer (non-blocking).

    Used from the WebSocket handler so a jittered claim/join/moment neither
    blocks message processing nor keeps the process alive on shutdown.
    """
    timer = Timer(max(0.0, delay), fn, args)
    timer.daemon = True
    timer.start()


# --- New-account behavioural warm-up ---------------------------------------
# A brand-new account behaving like a fully-established one (betting from day
# one, always present the instant a stream starts) is a risk signal. The backend
# passes the account's age via MINER_ACCOUNT_AGE_DAYS; a young account holds back
# and grows into full behaviour. Absent env (standalone mode / old rows) means
# "unknown age" -> treated as established, so nothing changes for those.

def account_age_days():
    """Account age in days from the backend, or None if unknown."""
    raw = environ.get("MINER_ACCOUNT_AGE_DAYS", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def warmup_blocks_betting() -> bool:
    """True while an account is too new to place predictions.

    Threshold in days via MINER_WARMUP_BET_DAYS (default 7); set 0 to disable.
    Unknown age -> False (established account, bet as usual).
    """
    try:
        days = float(environ.get("MINER_WARMUP_BET_DAYS", "7"))
    except ValueError:
        days = 7.0
    if days <= 0:
        return False
    age = account_age_days()
    if age is None:
        return False
    return age < days


def _millify(input, precision=2):
    return millify(input, precision)


def get_streamer_index(streamers: list, channel_id) -> int:
    try:
        return next(
            i for i, x in enumerate(streamers) if str(x.channel_id) == str(channel_id)
        )
    except StopIteration:
        return -1


def float_round(number, ndigits=2):
    return round(float(number), ndigits)


def server_time(message_data):
    return (
        datetime.fromtimestamp(
            message_data["server_time"], timezone.utc).isoformat()
        + "Z"
        if message_data is not None and "server_time" in message_data
        else datetime.fromtimestamp(time.time(), timezone.utc).isoformat() + "Z"
    )


# https://en.wikipedia.org/wiki/Cryptographic_nonce
def create_nonce(length=30) -> str:
    nonce = ""
    for i in range(length):
        char_index = randrange(0, 10 + 26 + 26)
        if char_index < 10:
            char = chr(ord("0") + char_index)
        elif char_index < 10 + 26:
            char = chr(ord("a") + char_index - 10)
        else:
            char = chr(ord("A") + char_index - 26 - 10)
        nonce += char
    return nonce

# for mobile-token


def get_user_agent(browser: str) -> str:
    """try:
        return USER_AGENTS[platform.system()][browser]
    except KeyError:
        # return USER_AGENTS["Linux"]["FIREFOX"]
        # return USER_AGENTS["Windows"]["CHROME"]"""
    return USER_AGENTS["Android"]["TV"]
    # return USER_AGENTS["Android"]["App"]


def remove_emoji(string: str) -> str:
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002500-\U00002587"  # chinese char
        "\U00002589-\U00002BEF"  # I need Unicode Character “█” (U+2588)
        "\U00002702-\U000027B0"
        "\U00002702-\U000027B0"
        "\U000024C2-\U00002587"
        "\U00002589-\U0001F251"
        "\U0001f926-\U0001f937"
        "\U00010000-\U0010ffff"
        "\u2640-\u2642"
        "\u2600-\u2B55"
        "\u200d"
        "\u23cf"
        "\u23e9"
        "\u231a"
        "\ufe0f"  # dingbats
        "\u3030"
        "\u231b"
        "\u2328"
        "\u23cf"
        "\u23e9"
        "\u23ea"
        "\u23eb"
        "\u23ec"
        "\u23ed"
        "\u23ee"
        "\u23ef"
        "\u23f0"
        "\u23f1"
        "\u23f2"
        "\u23f3"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(r"", string)


def at_least_one_value_in_settings_is(items, attr, value=True):
    for item in items:
        if getattr(item.settings, attr) == value:
            return True
    return False


def copy_values_if_none(settings, defaults):
    values = list(
        filter(
            lambda x: x.startswith("__") is False
            and callable(getattr(settings, x)) is False,
            dir(settings),
        )
    )

    for value in values:
        if getattr(settings, value) is None:
            setattr(settings, value, getattr(defaults, value))
    return settings


def set_default_settings(settings, defaults):
    # If no settings was provided use the default settings ...
    # If settings was provided but maybe are only partial set
    # Get the default values from Settings.streamer_settings
    return (
        deepcopy(defaults)
        if settings is None
        else copy_values_if_none(settings, defaults)
    )


'''def char_decision_as_index(char):
    return 0 if char == "A" else 1'''


def internet_connection_available(host="8.8.8.8", port=53, timeout=3):
    # Set the timeout on THIS socket only. socket.setdefaulttimeout() would
    # mutate the process-wide default, silently capping every later socket
    # (including all requests calls that pass no explicit timeout) at `timeout`
    # seconds — turning slow-but-healthy proxy responses into spurious
    # ReadTimeouts. Also close the socket so we don't leak an fd per check.
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        return True
    except socket.error:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except socket.error:
                pass


def percentage(a, b):
    return 0 if a == 0 else int((a / b) * 100)


def create_chunks(lst, n):
    return [lst[i: (i + n)] for i in range(0, len(lst), n)]  # noqa: E203


def download_file(name, fpath):
    r = requests.get(
        path.join(GITHUB_url, name),
        headers={"User-Agent": get_user_agent("FIREFOX")},
        stream=True,
    )
    if r.status_code == 200:
        with open(fpath, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
    return True


def read(fname):
    return open(path.join(path.dirname(__file__), fname), encoding="utf-8").read()


def init2dict(content):
    return dict(re.findall(r"""__([a-z]+)__ = "([^"]+)""", content))


def check_versions():
    try:
        current_version = init2dict(read("__init__.py"))
        current_version = (
            current_version["version"] if "version" in current_version else "0.0.0"
        )
    except Exception:
        current_version = "0.0.0"
    try:
        r = requests.get(
            "/".join(
                [
                    s.strip("/")
                    for s in [GITHUB_url, "TwitchChannelPointsMiner", "__init__.py"]
                ]
            ),
            # Bounded timeout: this runs in every account subprocess's __init__,
            # so a blackholed route here would hang startup indefinitely.
            timeout=5,
        )
        github_version = init2dict(r.text)
        github_version = (
            github_version["version"] if "version" in github_version else "0.0.0"
        )
    except Exception:
        github_version = "0.0.0"
    return current_version, github_version
