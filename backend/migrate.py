# -*- coding: utf-8 -*-
"""One-off migration from the old flat files into the database.

Reads accounts.txt (one Twitch username per line) and streamers.txt (one
streamer per line) and seeds the Account rows + the global STREAMERS setting.
Idempotent: existing accounts/settings are left untouched.

Usage:
    python -m backend.migrate [accounts.txt] [streamers.txt]
"""
import sys
from pathlib import Path
from typing import List

from sqlmodel import Session, select

from backend import config
from backend.db import engine, init_db
from backend.models import Account, AppSetting

STREAMERS_KEY = "STREAMERS"


def _read_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def migrate_from_files(accounts_path: Path, streamers_path: Path) -> dict:
    init_db()
    accounts = _read_lines(accounts_path)
    streamers = _read_lines(streamers_path)

    created, skipped = 0, 0
    with Session(engine) as session:
        for username in accounts:
            existing = session.exec(
                select(Account).where(Account.username == username)
            ).first()
            if existing:
                skipped += 1
                continue
            session.add(Account(username=username))
            created += 1

        if streamers:
            setting = session.get(AppSetting, STREAMERS_KEY)
            # Idempotent (as the module contract promises): only seed the setting
            # when it doesn't exist yet. Never overwrite an existing STREAMERS
            # value — that would clobber a list the user edited in the WebUI with
            # the (possibly stale) streamers.txt on every re-run.
            if setting is None:
                session.add(AppSetting(key=STREAMERS_KEY, value="\n".join(streamers)))

        session.commit()

    return {
        "accounts_created": created,
        "accounts_skipped": skipped,
        "streamers": len(streamers),
    }


def main():
    root = config.DATA_DIR.parent
    accounts_path = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "accounts.txt"
    streamers_path = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "streamers.txt"
    result = migrate_from_files(accounts_path, streamers_path)
    print(
        f"Migration done: {result['accounts_created']} accounts created, "
        f"{result['accounts_skipped']} skipped, {result['streamers']} streamers."
    )


if __name__ == "__main__":
    main()
