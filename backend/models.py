# -*- coding: utf-8 -*-
"""SQLModel database models.

Passwords are stored only in their encrypted form (`*_enc` columns); plaintext
never touches the database. Cookies stay as pickle files on disk (existing
miner mechanism), keyed by the account username.
"""
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Proxy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    scheme: str = "http"  # http | https | socks4 | socks5 | socks5h
    host: str
    port: int
    username: Optional[str] = None
    password_enc: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)

    accounts: List["Account"] = Relationship(back_populates="proxy")


class Account(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_enc: Optional[str] = None
    enabled: bool = True
    # stopped | running | starting | needs_login | error
    status: str = "stopped"
    proxy_id: Optional[int] = Field(default=None, foreign_key="proxy.id")
    # Hard "direct connection" flag: when True this account never uses a proxy —
    # neither a user-chosen one nor an auto-assigned one. The proxy monitor skips
    # it entirely (no failover, no auto-attach) and proxy_id is forced to None.
    # Use for accounts already farmed elsewhere (e.g. patriqcs on the Unraid
    # Twitch-Channel-Points-Miner-v2) that must keep their own direct IP.
    no_proxy: bool = False
    # Heist module roles: opener accounts fire "!heist" (60-min cooldown each),
    # joiner accounts (typically only the main) fire "!join" to collect the loot.
    heist_opener: bool = False
    heist_joiner: bool = False
    # Chat-redeem role: when True this account's channel points may be spent by
    # the chat-command redeemer (a viewer typing e.g. "!flash" makes the
    # earliest-free of these accounts WITH THE MOST points redeem that reward).
    chat_redeemer: bool = False
    # Web-redeem role: when True this account's channel points may be spent by
    # the public redeem website (backend/web_redeem_manager.py) — same
    # richest-free rotation as chat_redeemer, but selectable independently.
    web_redeemer: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: Optional[datetime] = None

    proxy: Optional[Proxy] = Relationship(back_populates="accounts")
    # delete-orphan: removing an account also removes its events. Without this,
    # SQLAlchemy tries to NULL the (NOT NULL) event.account_id -> IntegrityError.
    events: List["Event"] = Relationship(
        back_populates="account",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class Event(SQLModel, table=True):
    """Time-series of per-account activity: points, status changes, errors."""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="account.id")
    ts: datetime = Field(default_factory=utcnow, index=True)
    # points_snapshot | status | login | error | proxy | proxy_error |
    # redeem | follow | follow_failed
    type: str = Field(index=True)
    streamer: Optional[str] = None
    points: Optional[int] = None
    balance: Optional[int] = None
    reason: Optional[str] = None
    message: Optional[str] = None

    account: Optional[Account] = Relationship(back_populates="events")


class WebUser(SQLModel, table=True):
    """Login account for the PUBLIC redeem website (webredeem/ container).

    Completely separate from ``Account`` (Twitch miner accounts): a WebUser
    never touches Twitch — it only authenticates a visitor so redemptions can
    be attributed ("user X redeemed Y") and access can be revoked. Passwords
    are stored as scrypt hashes (backend/web_users.py); plaintext never
    touches the database.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    # set by an admin password reset: the website forces a new password on login
    must_change_password: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: Optional[datetime] = None


class AppSetting(SQLModel, table=True):
    """Global key/value settings (e.g. the shared STREAMERS list)."""
    key: str = Field(primary_key=True)
    value: str = ""
