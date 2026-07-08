# -*- coding: utf-8 -*-
"""SQLite engine + session factory with WAL enabled.

WAL lets the FastAPI process and the miner subprocesses (via the internal API)
read/write concurrently without locking each other out.
"""
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from backend import config, models  # noqa: F401 (import models so metadata is populated)

config.ensure_dirs()

engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
    # The default pool (5 + 10 overflow) is too small for the WS streams +
    # background threads (monitor/scheduler/pruner) + parallel requests, and
    # exhausted under load. SQLite file connections are cheap, so allow many.
    pool_size=20,
    max_overflow=40,
    pool_timeout=30,
    pool_recycle=1800,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def _ensure_columns() -> None:
    """Lightweight, idempotent column add-ons (no Alembic in this project).

    create_all() creates missing tables but never alters existing ones, so new
    columns on an already-created table must be added by hand. SQLite supports
    ADD COLUMN with a default; we only add what's missing.
    """
    from sqlalchemy import text

    wanted = {
        "account": {
            "heist_opener": "BOOLEAN NOT NULL DEFAULT 0",
            "heist_joiner": "BOOLEAN NOT NULL DEFAULT 0",
            "no_proxy": "BOOLEAN NOT NULL DEFAULT 0",
            "chat_redeemer": "BOOLEAN NOT NULL DEFAULT 0",
            "web_redeemer": "BOOLEAN NOT NULL DEFAULT 0",
            # Per-account client fingerprint (added nullable; existing rows are
            # backfilled with a generated value by _backfill_fingerprints()).
            "device_id": "VARCHAR",
            "ua_app": "VARCHAR",
            "ua_web": "VARCHAR",
            "signup_email": "VARCHAR",
        },
        # existing website users predate self-registration -> keep them approved
        "webuser": {
            "approved": "BOOLEAN NOT NULL DEFAULT 1",
        },
    }
    with engine.begin() as conn:
        for table, columns in wanted.items():
            existing = {
                row[1]  # name column of PRAGMA table_info
                for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _backfill_fingerprints() -> None:
    """Give pre-existing accounts a persistent client fingerprint.

    ADD COLUMN leaves old rows NULL; generate a coherent (device_id, ua_app,
    ua_web) triple for each so they too present a stable per-account device
    instead of falling back to the shared default. Idempotent: only rows still
    missing a value are touched, so it never reshuffles an assigned fingerprint.
    """
    from sqlalchemy import text
    from TwitchChannelPointsMiner.utils import (
        new_app_user_agent,
        new_device_id,
        new_web_user_agent,
    )

    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id FROM account "
            "WHERE device_id IS NULL OR ua_app IS NULL OR ua_web IS NULL"
        )).fetchall()
        for (account_id,) in rows:
            conn.execute(
                text(
                    "UPDATE account SET "
                    "device_id = COALESCE(device_id, :dev), "
                    "ua_app = COALESCE(ua_app, :app), "
                    "ua_web = COALESCE(ua_web, :web) "
                    "WHERE id = :id"
                ),
                {
                    "dev": new_device_id(),
                    "app": new_app_user_agent(),
                    "web": new_web_user_agent(),
                    "id": account_id,
                },
            )


def _ensure_indexes() -> None:
    """Idempotent composite indexes (create_all only makes single-column ones).

    The heartbeat liveness check and the watch-monitor both query Event by
    (account_id, type, ts); without a composite index SQLite falls back to a
    single-column index and scans, which grows costly as the Event table fills.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_event_account_type_ts "
            "ON event (account_id, type, ts)"
        ))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_columns()
    _backfill_fingerprints()
    _ensure_indexes()


def get_session():
    """FastAPI dependency: yields a session per request."""
    with Session(engine) as session:
        yield session
