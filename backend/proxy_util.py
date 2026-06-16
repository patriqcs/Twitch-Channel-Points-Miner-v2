# -*- coding: utf-8 -*-
"""Helpers to turn a Proxy DB row into the engine's Proxy (with decrypted creds)."""
from backend import crypto


def to_engine_proxy(p):
    """Build an engine Proxy (decrypted) from a Proxy DB model, or None."""
    if p is None:
        return None
    from TwitchChannelPointsMiner.classes.Proxy import Proxy as EngineProxy

    return EngineProxy(
        scheme=p.scheme,
        host=p.host,
        port=p.port,
        username=p.username,
        password=crypto.decrypt(p.password_enc),
    )


def proxy_url(p) -> "str | None":
    ep = to_engine_proxy(p)
    return ep.url if ep is not None else None
