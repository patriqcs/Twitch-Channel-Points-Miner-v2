# -*- coding: utf-8 -*-
"""Symmetric encryption (Fernet) for account/proxy credentials at rest.

The key comes from the SECRET_KEY env var if set, otherwise it is generated
once into DATA_DIR/secret.key (chmod 600). Losing the key makes stored
passwords unrecoverable — back it up together with the database.
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from backend import config

logger = logging.getLogger("crypto")


def _load_or_create_key() -> bytes:
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key.encode() if isinstance(env_key, str) else env_key

    config.ensure_dirs()
    if config.SECRET_KEY_FILE.exists():
        return config.SECRET_KEY_FILE.read_bytes()

    key = Fernet.generate_key()
    config.SECRET_KEY_FILE.write_bytes(key)
    try:
        os.chmod(config.SECRET_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def _build_fernet() -> Fernet:
    key = _load_or_create_key()
    try:
        return Fernet(key)
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            "Invalid SECRET_KEY: it must be a 32-byte url-safe base64 Fernet key. "
            "Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from e


_fernet = _build_fernet()


def encrypt(plaintext) -> "str | None":
    """Encrypt a string. None/empty -> None (so 'no password' stays null)."""
    if plaintext is None or plaintext == "":
        return None
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(token) -> "str | None":
    """Decrypt a token produced by encrypt(). None/empty -> None.

    On an InvalidToken (the SECRET_KEY / secret.key no longer matches the one
    that encrypted this value — e.g. the key was lost or rotated) we log and
    return None instead of raising. A raised InvalidToken here is not caught by
    callers (autostart, proxy monitor, /internal/config) and would take down the
    whole autostart thread / failover loop, so no account could start at all.
    """
    if not token:
        return None
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        logger.error(
            "Could not decrypt a stored credential: the SECRET_KEY / secret.key "
            "does not match the one it was encrypted with. Restore the original "
            "key (DATA_DIR/secret.key or the SECRET_KEY env var) to recover the "
            "proxy/account password."
        )
        return None
