# -*- coding: utf-8 -*-
"""
Proxy support for routing a single account's traffic through an HTTP or SOCKS
proxy: both the HTTP(S) requests (login, GraphQL, minute-watched) and the
Twitch PubSub WebSocket.

One Proxy instance belongs to one account. The same proxy may be shared by
several accounts (the web layer enforces a max of 5 accounts per proxy).
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

# Endpoint used by test_proxy() to discover the proxy's exit IP.
IP_CHECK_URL = "https://api.ipify.org?format=json"

# What actually matters for mining: can the proxy reach Twitch? A proxy may
# reach the internet (ipify ok) yet refuse/timeout to Twitch — that must count
# as broken. Any HTTP response from this host means the proxy reached Twitch.
TWITCH_CHECK_URL = "https://gql.twitch.tv/"

# requests/proxy scheme -> websocket-client run_forever() proxy_type
_WS_PROXY_TYPES = {
    "http": "http",
    "https": "http",
    "socks4": "socks4",
    "socks4a": "socks4a",
    "socks5": "socks5",
    "socks5h": "socks5h",
}


@dataclass
class Proxy:
    scheme: str = "http"
    host: str = ""
    port: int = 0
    username: Optional[str] = None
    password: Optional[str] = None

    def __post_init__(self):
        self.scheme = (self.scheme or "http").lower()
        if self.scheme not in _WS_PROXY_TYPES:
            raise ValueError(
                f"Unsupported proxy scheme '{self.scheme}'. "
                f"Use one of: {', '.join(_WS_PROXY_TYPES)}"
            )
        if not self.host:
            raise ValueError("Proxy host must not be empty")
        self.port = int(self.port)
        if not (0 < self.port < 65536):
            raise ValueError(f"Proxy port out of range: {self.port}")

    @classmethod
    def from_url(cls, url: str) -> "Proxy":
        """Parse a proxy URL like 'socks5://user:pass@host:1080' or 'host:8080'."""
        if not url or not str(url).strip():
            raise ValueError("Empty proxy URL")
        url = str(url).strip()
        if "://" not in url:
            url = "http://" + url
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"Proxy URL must contain host and port: {url}")
        return cls(
            scheme=parsed.scheme or "http",
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
        )

    @property
    def url(self) -> str:
        """Full proxy URL with URL-encoded credentials (if any)."""
        auth = ""
        if self.username:
            auth = quote(self.username, safe="")
            if self.password:
                auth += ":" + quote(self.password, safe="")
            auth += "@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def requests_proxies(self) -> dict:
        """dict for the requests `proxies=` argument (same proxy for http+https)."""
        u = self.url
        return {"http": u, "https": u}

    @property
    def ws_kwargs(self) -> dict:
        """kwargs for websocket-client's WebSocketApp.run_forever()."""
        kwargs = {
            "http_proxy_host": self.host,
            "http_proxy_port": self.port,
            "proxy_type": _WS_PROXY_TYPES[self.scheme],
        }
        if self.username:
            kwargs["http_proxy_auth"] = (self.username, self.password or "")
        return kwargs

    def test_proxy(self, timeout: int = 10) -> dict:
        """Check the proxy can reach Twitch. Returns
        {"ok": True, "ip": str|None, "latency_ms": int} or {"ok": False, "error": str}.

        'ok' means Twitch is reachable through the proxy (any HTTP response from
        gql.twitch.tv). The exit IP is fetched best-effort for display only and
        does not affect the verdict.
        """
        start = time.perf_counter()
        try:
            # Any HTTP response = the proxy reached Twitch (status code irrelevant).
            requests.get(
                TWITCH_CHECK_URL, proxies=self.requests_proxies, timeout=timeout
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": str(e)}

        ip = None
        try:
            r = requests.get(IP_CHECK_URL, proxies=self.requests_proxies, timeout=timeout)
            if r.ok:
                ip = r.json().get("ip")
        except requests.exceptions.RequestException:
            pass
        return {"ok": True, "ip": ip, "latency_ms": latency_ms}

    def __str__(self):
        # Never include credentials in logs.
        return f"{self.scheme}://{self.host}:{self.port}"
