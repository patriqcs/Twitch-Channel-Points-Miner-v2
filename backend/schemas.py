# -*- coding: utf-8 -*-
"""Pydantic request/response schemas for the API.

Kept separate from the table models so we never serialize encrypted password
columns to clients — we expose a boolean `has_password` instead.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


# ---- Proxy ----
class ProxyCreate(BaseModel):
    name: str
    scheme: str = "http"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None


class ProxyUpdate(BaseModel):
    name: Optional[str] = None
    scheme: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None


class ProxyRead(BaseModel):
    id: int
    name: str
    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    has_password: bool = False
    account_count: int = 0
    created_at: datetime


class ProxyTestResult(BaseModel):
    ok: bool
    ip: Optional[str] = None
    latency_ms: Optional[int] = None
    error: Optional[str] = None


class ProxyImport(BaseModel):
    # One proxy per line: "scheme://[user:pass@]host:port" or bare "host:port".
    # Blank lines and lines starting with '#' are ignored.
    text: str


class ProxyImportError(BaseModel):
    line: int
    value: str
    error: str


class ProxyImportResult(BaseModel):
    added: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    errors: List[ProxyImportError] = []
    proxies: List[ProxyRead] = []


# ---- Account ----
class AccountCreate(BaseModel):
    username: str
    password: Optional[str] = None
    proxy_id: Optional[int] = None
    enabled: bool = True


class AccountUpdate(BaseModel):
    password: Optional[str] = None
    proxy_id: Optional[int] = None
    enabled: Optional[bool] = None


class AccountRead(BaseModel):
    id: int
    username: str
    enabled: bool
    status: str
    proxy_id: Optional[int] = None
    has_password: bool = False
    created_at: datetime
    last_login_at: Optional[datetime] = None


# ---- Settings ----
class SettingRead(BaseModel):
    key: str
    value: str


class SettingWrite(BaseModel):
    value: str
