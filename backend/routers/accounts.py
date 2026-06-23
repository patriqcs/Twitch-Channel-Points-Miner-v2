# -*- coding: utf-8 -*-
"""Account CRUD, miner control, device-code login and login test."""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from backend import config, crypto
from backend.db import get_session
from backend.login_service import login_service
from backend.manager import manager
from backend.models import Account, Proxy
from backend.proxy_util import to_engine_proxy
from backend.schemas import AccountCreate, AccountRead, AccountUpdate

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _to_read(a: Account) -> AccountRead:
    return AccountRead(
        id=a.id, username=a.username, enabled=a.enabled, status=a.status,
        proxy_id=a.proxy_id, has_password=bool(a.password_enc),
        heist_opener=a.heist_opener, heist_joiner=a.heist_joiner,
        created_at=a.created_at, last_login_at=a.last_login_at,
    )


def _check_proxy_capacity(session: Session, proxy_id: int, exclude_account_id=None):
    """Enforce the max-accounts-per-proxy rule."""
    if proxy_id is None:
        return
    if session.get(Proxy, proxy_id) is None:
        raise HTTPException(404, "proxy not found")
    q = select(func.count()).select_from(Account).where(Account.proxy_id == proxy_id)
    if exclude_account_id is not None:
        q = q.where(Account.id != exclude_account_id)
    if session.exec(q).one() >= config.MAX_ACCOUNTS_PER_PROXY:
        raise HTTPException(
            409, f"proxy already has the maximum of {config.MAX_ACCOUNTS_PER_PROXY} accounts"
        )


def _get(session: Session, account_id: int) -> Account:
    acc = session.get(Account, account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    return acc


@router.get("", response_model=list[AccountRead])
def list_accounts(session: Session = Depends(get_session)):
    return [_to_read(a) for a in session.exec(select(Account)).all()]


@router.post("", response_model=AccountRead, status_code=201)
def create_account(payload: AccountCreate, session: Session = Depends(get_session)):
    if session.exec(select(Account).where(Account.username == payload.username)).first():
        raise HTTPException(409, "username already exists")
    _check_proxy_capacity(session, payload.proxy_id)
    acc = Account(
        username=payload.username, password_enc=crypto.encrypt(payload.password),
        proxy_id=payload.proxy_id, enabled=payload.enabled,
        heist_opener=payload.heist_opener, heist_joiner=payload.heist_joiner,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return _to_read(acc)


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: int, session: Session = Depends(get_session)):
    return _to_read(_get(session, account_id))


@router.patch("/{account_id}", response_model=AccountRead)
def update_account(account_id: int, payload: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = _get(session, account_id)
    data = payload.model_dump(exclude_unset=True)
    if "proxy_id" in data and data["proxy_id"] != acc.proxy_id:
        _check_proxy_capacity(session, data["proxy_id"], exclude_account_id=acc.id)
    if "password" in data:
        acc.password_enc = crypto.encrypt(data.pop("password"))
    for k, v in data.items():
        setattr(acc, k, v)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return _to_read(acc)


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: int, session: Session = Depends(get_session)):
    acc = _get(session, account_id)
    manager.stop(acc.username)
    cookie = config.COOKIES_DIR / f"{acc.username}.pkl"
    if cookie.exists():
        cookie.unlink()
    session.delete(acc)
    session.commit()


# ---- miner control ----
@router.post("/{account_id}/start")
def start_account(account_id: int, session: Session = Depends(get_session)):
    acc = _get(session, account_id)
    started = manager.start(acc.username)
    return {"started": started, "username": acc.username}


@router.post("/{account_id}/stop")
def stop_account(account_id: int, session: Session = Depends(get_session)):
    acc = _get(session, account_id)
    stopped = manager.stop(acc.username)
    return {"stopped": stopped, "username": acc.username}


@router.post("/{account_id}/restart")
def restart_account(account_id: int, session: Session = Depends(get_session)):
    acc = _get(session, account_id)
    manager.restart(acc.username)
    return {"restarted": True, "username": acc.username}


# ---- device-code login ----
@router.post("/{account_id}/login")
def start_login(account_id: int, session: Session = Depends(get_session)):
    acc = _get(session, account_id)
    proxy = to_engine_proxy(session.get(Proxy, acc.proxy_id)) if acc.proxy_id else None
    state = login_service.start(acc.username, proxy=proxy)
    if state.status == "error":
        raise HTTPException(502, state.error or "login start failed")
    return {
        "status": state.status,
        "user_code": state.user_code,
        "verification_uri": state.verification_uri,
        "expires_at": state.expires_at,
    }


@router.get("/{account_id}/login/status")
def login_status(account_id: int, session: Session = Depends(get_session)):
    acc = _get(session, account_id)
    state = login_service.get_state(acc.username)
    if state.status == "authorized":
        acc.status = "stopped"  # logged in, ready to be started
        session.add(acc)
        session.commit()
    return {"status": state.status, "user_code": state.user_code,
            "verification_uri": state.verification_uri, "error": state.error}


@router.post("/{account_id}/login-test")
def login_test(account_id: int, session: Session = Depends(get_session)):
    """Validate the stored cookie by doing a user-id lookup (through the proxy)."""
    acc = _get(session, account_id)
    cookie = config.COOKIES_DIR / f"{acc.username}.pkl"
    if not cookie.exists():
        return {"ok": False, "error": "no cookie - login required"}

    from TwitchChannelPointsMiner.classes.TwitchLogin import TwitchLogin
    from TwitchChannelPointsMiner.constants import CLIENT_ID
    from TwitchChannelPointsMiner.utils import get_user_agent

    proxy = to_engine_proxy(session.get(Proxy, acc.proxy_id)) if acc.proxy_id else None
    login = TwitchLogin(CLIENT_ID, "x" * 32, acc.username,
                        get_user_agent("CHROME"), proxy=proxy)
    try:
        login.load_cookies(str(cookie))
        login.set_token(login.get_auth_token())
        ok = bool(login.check_login())
        return {"ok": ok, "error": None if ok else "cookie invalid or expired"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.get("/{account_id}/auth-token")
def get_auth_token(account_id: int, session: Session = Depends(get_session)):
    """Return this account's stored Twitch 'auth-token' cookie value.

    SENSITIVE: the auth-token is a full account credential. Only expose the WebUI
    to trusted networks (or behind Cloudflare Access).
    """
    import pickle

    acc = _get(session, account_id)
    cookie = config.COOKIES_DIR / f"{acc.username}.pkl"
    if not cookie.exists():
        return {"auth_token": None, "error": "no cookie - login required"}
    try:
        with open(cookie, "rb") as f:
            cookies = pickle.load(f)
    except Exception as e:  # noqa: BLE001
        return {"auth_token": None, "error": f"could not read cookie: {e}"}
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") == "auth-token":
            return {"auth_token": c.get("value"), "error": None}
    return {"auth_token": None, "error": "auth-token not found in cookie"}
