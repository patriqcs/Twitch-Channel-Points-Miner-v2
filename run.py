# -*- coding: utf-8 -*-

"""
Generischer Runner für EINEN Twitch-Account.

Quelle für Username (Priorität von oben):
    1. ENV  TWITCH_USERNAME=deinaccount
    2. Argument:  python run.py deinaccount

Quelle für Streamer (Priorität von oben):
    1. ENV  STREAMERS="streamer1,streamer2,streamer3"   (Komma/Leerzeichen/Zeilen)
    2. Datei streamers.txt (ein Streamer pro Zeile)

Der Account schaut alle Streamer (nur zuschauen: Punkte, Watch-Streaks,
Moments und Drops – KEINE Predictions/Wetten).

Im Docker/Unraid-Betrieb setzt man einfach die ENV-Variablen TWITCH_USERNAME
und STREAMERS – dann muss keine Datei editiert werden.
"""

import logging
import os
import sys

from TwitchChannelPointsMiner import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.logger import LoggerSettings
from TwitchChannelPointsMiner.classes.Chat import ChatPresence
from TwitchChannelPointsMiner.classes.Settings import Priority, FollowersOrder
from TwitchChannelPointsMiner.classes.entities.Streamer import Streamer, StreamerSettings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STREAMERS_FILE = os.path.join(BASE_DIR, "streamers.txt")


def read_list(path):
    """Liest eine Datei, ignoriert leere Zeilen und Kommentare (#)."""
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
    return items


def read_streamers():
    """Streamer aus ENV STREAMERS (Komma/Leerzeichen/Zeilen) oder streamers.txt."""
    env = os.environ.get("STREAMERS", "").strip()
    if env:
        return [s.strip() for s in env.replace("\n", ",").replace(" ", ",").split(",") if s.strip()]
    if os.path.isfile(STREAMERS_FILE):
        return read_list(STREAMERS_FILE)
    return []


def main():
    username = (os.environ.get("TWITCH_USERNAME", "").strip()
                or (sys.argv[1].strip() if len(sys.argv) > 1 else ""))
    if not username:
        print("Kein Username. Setze ENV TWITCH_USERNAME oder: python run.py <username>")
        sys.exit(1)

    streamer_names = read_streamers()
    if not streamer_names:
        print("Keine Streamer. Setze ENV STREAMERS='a,b,c' oder fülle streamers.txt.")
        sys.exit(1)

    # Optionaler Proxy für diesen Account (HTTP-Requests UND WebSocket).
    # Format: "socks5://user:pass@host:1080", "http://host:8080" oder "host:port".
    proxy = os.environ.get("PROXY", "").strip() or None

    twitch_miner = TwitchChannelPointsMiner(
        username=username,
        # Kein Passwort -> wird beim ersten Start interaktiv abgefragt,
        # danach per Cookie (cookies/<username>.pkl) wiederverwendet.
        proxy=proxy,                        # ENV PROXY -> alle Verbindungen über diesen Proxy
        claim_drops_startup=True,           # Beim Start alle Drops aus dem Inventar einsammeln
        priority=[
            Priority.STREAK,                # Zuerst Watch-Streaks abgreifen
            Priority.DROPS,                 # Dann Drops
            Priority.ORDER,                 # Dann nach Reihenfolge der Liste
        ],
        enable_analytics=False,             # Aus -> deutlich weniger RAM (wichtig bei 10 Accounts)
        logger_settings=LoggerSettings(
            save=True,
            console_level=logging.INFO,
            console_username=True,          # Username in jeder Log-Zeile (praktisch bei mehreren Accounts)
            file_level=logging.INFO,
            emoji=True,
            less=False,
            colored=True,
        ),
        # Nur-Zuschauen-Defaults für ALLE Streamer (keine Wetten):
        streamer_settings=StreamerSettings(
            make_predictions=False,         # KEINE Wetten / Predictions
            follow_raid=True,               # Raids folgen -> extra Punkte
            claim_drops=True,               # Drops einsammeln
            claim_moments=True,             # Moments einsammeln
            watch_streak=True,              # Watch-Streak-Punkte
            community_goals=False,
            chat=ChatPresence.ONLINE,       # Chat beitreten solange Streamer online (mehr Watch-Time)
        ),
    )

    twitch_miner.mine(
        [Streamer(name) for name in streamer_names],
        followers=False,
        followers_order=FollowersOrder.ASC,
    )


if __name__ == "__main__":
    main()
