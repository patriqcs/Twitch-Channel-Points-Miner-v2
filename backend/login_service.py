# -*- coding: utf-8 -*-
"""Twitch device-code login, driven from the web UI.

Flow per account:
  1. start(username, proxy) -> ask Twitch for a device/user code, return the
     user_code + activation URL to the UI, and spawn a background poller.
  2. The user opens https://www.twitch.tv/activate, enters the code, authorizes.
  3. The poller exchanges the device_code for an access token, then saves the
     cookie file (cookies/<username>.pkl) so the miner can run headless.

State is kept in memory (one login at a time per username). Reuses the engine's
proxy-aware TwitchLogin so login traffic also goes through the account's proxy.
"""
import logging
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from secrets import choice

from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
from TwitchChannelPointsMiner.constants import CLIENT_ID
from TwitchChannelPointsMiner.utils import get_user_agent

from backend import config

logger = logging.getLogger("login_service")

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
SCOPES = (
    "channel_read chat:read user_blocks_edit "
    "user_blocks_read user_follows_edit user_read"
)


def _device_id() -> str:
    return "".join(choice(string.ascii_letters + string.digits) for _ in range(32))


class LoginState:
    def __init__(self):
        self.status = "idle"  # idle|pending|authorized|expired|error
        self.user_code = None
        self.verification_uri = "https://www.twitch.tv/activate"
        self.expires_at = None
        self.error = None


class LoginService:
    def __init__(self):
        self._states: dict[str, LoginState] = {}
        self._lock = threading.Lock()

    def get_state(self, username: str) -> LoginState:
        with self._lock:
            return self._states.get(username) or LoginState()

    def start(self, username: str, proxy=None) -> LoginState:
        """Begin a device-code login. Returns the state with user_code set."""
        config.ensure_dirs()
        login = TwitchLogin(CLIENT_ID, _device_id(), username,
                            get_user_agent("CHROME"), proxy=proxy)

        resp = login.send_oauth_request(
            DEVICE_URL, {"client_id": CLIENT_ID, "scopes": SCOPES}
        )
        if resp.status_code != 200 or "device_code" not in resp.json():
            state = LoginState()
            state.status = "error"
            state.error = f"device request failed ({resp.status_code})"
            with self._lock:
                self._states[username] = state
            return state

        data = resp.json()
        state = LoginState()
        state.status = "pending"
        state.user_code = data["user_code"]
        state.verification_uri = data.get("verification_uri", state.verification_uri)
        state.expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(data.get("expires_in", 1800))
        )
        with self._lock:
            self._states[username] = state

        t = threading.Thread(
            target=self._poll,
            args=(username, login, data["device_code"],
                  int(data.get("interval", 5)), state),
            name=f"login-{username}",
            daemon=True,
        )
        t.start()
        return state

    def _poll(self, username, login, device_code, interval, state):
        interval = max(interval, 1)
        while True:
            time.sleep(interval)
            if state.expires_at and datetime.now(timezone.utc) >= state.expires_at:
                state.status = "expired"
                return
            try:
                resp = login.send_oauth_request(
                    TOKEN_URL,
                    {
                        "client_id": CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("login poll error for %s: %s", username, e)
                continue

            if resp.status_code != 200:
                # Twitch returns 'slow_down' when we poll too fast -> back off as
                # the spec requires, otherwise we keep getting rejected.
                if "slow_down" in (resp.text or ""):
                    interval += 5
                # 400 = user hasn't authorized yet -> keep polling.
                continue

            body = resp.json()
            if "access_token" not in body:
                continue

            try:
                login.set_token(body["access_token"])
                if not login.check_login():
                    state.status = "error"
                    state.error = "token accepted but user lookup failed"
                    return
                cookie_path = config.COOKIES_DIR / f"{username}.pkl"
                login.save_cookies(str(cookie_path))
                state.status = "authorized"
            except Exception as e:  # noqa: BLE001
                state.status = "error"
                state.error = str(e)
            return


login_service = LoginService()
