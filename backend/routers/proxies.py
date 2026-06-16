# -*- coding: utf-8 -*-
"""Proxy CRUD + connectivity test."""
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from backend import crypto
from backend.db import get_session
from backend.models import Account, Proxy
from backend.proxy_util import to_engine_proxy
from backend.schemas import (
    MullvadImport,
    ProxyBulkDelete,
    ProxyBulkDeleteResult,
    ProxyBulkTestItem,
    ProxyCreate,
    ProxyImport,
    ProxyImportError,
    ProxyImportResult,
    ProxyRead,
    ProxyTestResult,
    ProxyUpdate,
)

MULLVAD_RELAYS_URL = "https://api.mullvad.net/www/relays/wireguard/"

# Cap concurrency so a huge proxy list doesn't open hundreds of sockets at once.
_TEST_TIMEOUT = 8
_TEST_WORKERS = 20

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


def _to_read(p: Proxy, count: int = 0) -> ProxyRead:
    return ProxyRead(
        id=p.id, name=p.name, scheme=p.scheme, host=p.host, port=p.port,
        username=p.username, has_password=bool(p.password_enc),
        account_count=count, created_at=p.created_at,
    )


def _counts(session: Session) -> dict:
    """All proxy_id -> account_count in ONE query (avoids an N+1 count per proxy)."""
    rows = session.exec(
        select(Account.proxy_id, func.count())
        .where(Account.proxy_id.is_not(None))
        .group_by(Account.proxy_id)
    ).all()
    return {pid: n for pid, n in rows}


def _count_for(session: Session, proxy_id: int) -> int:
    return session.exec(
        select(func.count()).select_from(Account).where(Account.proxy_id == proxy_id)
    ).one()


@router.get("", response_model=list[ProxyRead])
def list_proxies(session: Session = Depends(get_session)):
    counts = _counts(session)
    return [_to_read(p, counts.get(p.id, 0)) for p in session.exec(select(Proxy)).all()]


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
    return _to_read(p, 0)


@router.post("/import", response_model=ProxyImportResult, status_code=201)
def import_proxies(payload: ProxyImport, session: Session = Depends(get_session)):
    """Bulk-add proxies from a list (one 'scheme://[user:pass@]host:port' per line).

    Blank lines and '#' comments are ignored. Duplicates (same scheme/host/port,
    already stored or repeated within the list) are skipped, not errored.
    """
    from TwitchChannelPointsMiner.classes.Proxy import Proxy as EngineProxy

    # Seed the dedup set with what's already stored.
    seen = {
        (p.scheme.lower(), p.host.lower(), p.port)
        for p in session.exec(select(Proxy)).all()
    }

    result = ProxyImportResult()
    candidates = []  # engine proxies that parsed and are not duplicates
    for idx, raw in enumerate(payload.text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ep = EngineProxy.from_url(line)
        except Exception as exc:  # noqa: BLE001 - report parse error per line
            result.failed += 1
            result.errors.append(ProxyImportError(line=idx, value=line, error=str(exc)))
            continue

        key = (ep.scheme.lower(), ep.host.lower(), ep.port)
        if key in seen:
            result.skipped_duplicate += 1
            continue
        seen.add(key)
        candidates.append(ep)

    # Optionally connectivity-test all candidates first; keep only the working ones.
    if payload.test_before_add and candidates:
        def _alive(ep):
            try:
                return bool(ep.test_proxy(timeout=_TEST_TIMEOUT).get("ok"))
            except Exception:  # noqa: BLE001
                return False

        with ThreadPoolExecutor(max_workers=min(_TEST_WORKERS, len(candidates))) as pool:
            alive_flags = list(pool.map(_alive, candidates))
        keep = [ep for ep, ok in zip(candidates, alive_flags) if ok]
        result.skipped_offline = len(candidates) - len(keep)
    else:
        keep = candidates

    for ep in keep:
        p = Proxy(
            name=f"{ep.host}:{ep.port}",
            scheme=ep.scheme,
            host=ep.host,
            port=ep.port,
            username=ep.username,
            password_enc=crypto.encrypt(ep.password),
        )
        session.add(p)
        session.flush()  # assign id without committing each row individually
        result.proxies.append(_to_read(p, 0))
        result.added += 1

    session.commit()
    return result


@router.post("/mullvad-import", response_model=ProxyImportResult, status_code=201)
def import_mullvad(payload: MullvadImport, session: Session = Depends(get_session)):
    """Add Mullvad WireGuard SOCKS5 relays as proxies.

    Relays are only reachable while the container runs inside a Mullvad
    WireGuard tunnel, so they are added WITHOUT a connectivity test. Use
    'Alle testen' once the tunnel is up.
    """
    import requests

    try:
        resp = requests.get(MULLVAD_RELAYS_URL, timeout=15)
        resp.raise_for_status()
        relays = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not fetch Mullvad relay list: {exc}")

    cc = (payload.country_code or "").strip().lower() or None
    picked = []
    for r in relays:
        if not r.get("active") or not r.get("socks_name"):
            continue
        if cc and (r.get("country_code") or "").lower() != cc:
            continue
        if payload.daita_only and not r.get("daita"):
            continue
        picked.append(r)
    if payload.limit and payload.limit > 0:
        picked = picked[: payload.limit]

    seen = {
        (p.scheme.lower(), p.host.lower(), p.port)
        for p in session.exec(select(Proxy)).all()
    }
    result = ProxyImportResult()
    for r in picked:
        host = r["socks_name"]
        port = int(r.get("socks_port") or 1080)
        key = ("socks5", host.lower(), port)
        if key in seen:
            result.skipped_duplicate += 1
            continue
        seen.add(key)
        city = r.get("city_name") or r.get("country_name") or ""
        p = Proxy(name=f"MV {r.get('country_code','').upper()} {city} ({r['hostname']})",
                  scheme="socks5", host=host, port=port)
        session.add(p)
        session.flush()
        result.proxies.append(_to_read(p, 0))
        result.added += 1

    session.commit()
    return result


@router.post("/test-all", response_model=list[ProxyBulkTestItem])
def test_all_proxies(session: Session = Depends(get_session)):
    """Test every proxy concurrently and return one result per proxy."""
    proxies = session.exec(select(Proxy)).all()
    # Build engine proxies (decrypts creds) in the request thread; the DB session
    # is not touched inside worker threads.
    jobs = [(p.id, p.name, to_engine_proxy(p)) for p in proxies]

    def _run(job):
        pid, name, ep = job
        try:
            r = ep.test_proxy(timeout=_TEST_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "error": str(exc)}
        return ProxyBulkTestItem(
            id=pid, name=name, ok=bool(r.get("ok")),
            ip=r.get("ip"), latency_ms=r.get("latency_ms"), error=r.get("error"),
        )

    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(_TEST_WORKERS, len(jobs))) as pool:
        return list(pool.map(_run, jobs))


@router.post("/bulk-delete", response_model=ProxyBulkDeleteResult)
def bulk_delete_proxies(payload: ProxyBulkDelete,
                        session: Session = Depends(get_session)):
    """Delete the given proxies. Proxies still assigned to an account are kept."""
    result = ProxyBulkDeleteResult()
    for pid in payload.ids:
        p = session.get(Proxy, pid)
        if p is None:
            continue
        in_use = session.exec(
            select(func.count()).select_from(Account).where(Account.proxy_id == pid)
        ).one()
        if in_use:
            result.skipped_in_use += 1
            continue
        session.delete(p)
        result.deleted += 1
    session.commit()
    return result


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
    return _to_read(p, _count_for(session, p.id))


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
