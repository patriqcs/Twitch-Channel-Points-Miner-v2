# -*- coding: utf-8 -*-
"""Proxy CRUD + connectivity test."""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from backend import crypto
from backend.db import get_session
from backend.models import Account, Proxy
from backend.proxy_util import to_engine_proxy
from backend.schemas import ProxyCreate, ProxyRead, ProxyTestResult, ProxyUpdate

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


def _to_read(session: Session, p: Proxy) -> ProxyRead:
    count = session.exec(
        select(func.count()).select_from(Account).where(Account.proxy_id == p.id)
    ).one()
    return ProxyRead(
        id=p.id, name=p.name, scheme=p.scheme, host=p.host, port=p.port,
        username=p.username, has_password=bool(p.password_enc),
        account_count=count, created_at=p.created_at,
    )


@router.get("", response_model=list[ProxyRead])
def list_proxies(session: Session = Depends(get_session)):
    return [_to_read(session, p) for p in session.exec(select(Proxy)).all()]


@router.post("", response_model=ProxyRead, status_code=201)
def create_proxy(payload: ProxyCreate, session: Session = Depends(get_session)):
    p = Proxy(
        name=payload.name, scheme=payload.scheme, host=payload.host,
        port=payload.port, username=payload.username,
        password_enc=crypto.encrypt(payload.password),
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return _to_read(session, p)


@router.patch("/{proxy_id}", response_model=ProxyRead)
def update_proxy(proxy_id: int, payload: ProxyUpdate,
                 session: Session = Depends(get_session)):
    p = session.get(Proxy, proxy_id)
    if p is None:
        raise HTTPException(404, "proxy not found")
    data = payload.model_dump(exclude_unset=True)
    if "password" in data:
        p.password_enc = crypto.encrypt(data.pop("password"))
    for k, v in data.items():
        setattr(p, k, v)
    session.add(p)
    session.commit()
    session.refresh(p)
    return _to_read(session, p)


@router.delete("/{proxy_id}", status_code=204)
def delete_proxy(proxy_id: int, session: Session = Depends(get_session)):
    p = session.get(Proxy, proxy_id)
    if p is None:
        raise HTTPException(404, "proxy not found")
    in_use = session.exec(
        select(func.count()).select_from(Account).where(Account.proxy_id == proxy_id)
    ).one()
    if in_use:
        raise HTTPException(409, f"proxy is assigned to {in_use} account(s)")
    session.delete(p)
    session.commit()


@router.post("/{proxy_id}/test", response_model=ProxyTestResult)
def test_proxy(proxy_id: int, session: Session = Depends(get_session)):
    p = session.get(Proxy, proxy_id)
    if p is None:
        raise HTTPException(404, "proxy not found")
    result = to_engine_proxy(p).test_proxy()
    return ProxyTestResult(**result)
