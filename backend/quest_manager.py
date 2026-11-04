# -*- coding: utf-8 -*-
"""Badge-Quest-Manager: farmt Twitch Reward-Drops (Pokémon-Badges) für EINEN
Account (Default: der Hauptaccount), ohne dessen laufenden Miner anzufassen.

Ablauf (Thread, Tick alle paar Sekunden, Aktionen zeitgesteuert):
  1. Fortschritt holen (`ViewerRewardDropInventory`) + gewählte Lane je Track.
  2. `quest.plan` entscheidet: weiterschauen / Lane wechseln / warten / fertig.
  3. Lane wechseln = `SetProgressionTrackSelection` (wie der Web-Client).
  4. Schauen = eigene Watch-Beacons für einen Live-Kanal aus dem Pool, dessen
     Kategorie zählt: PlaybackAccessToken -> usher m3u8 -> Segment-HEAD ->
     Spade `minute-watched`, ca. jede Minute (mit Jitter). Exakt der Pfad, den
     der Miner für seine Streams nimmt — Twitch sieht einen zweiten Player-Tab.
  5. Tagesziel voll -> nächste Lane (Rotation); kein Fortschritt -> Lane heute
     sperren (Fallback, falls Twitch nur die zuerst gewählte Lane zählt);
     alle Lanes voll -> Pause bis zum frühesten Fensterablauf.

Zustand ist in-memory (Fortschritt kommt bei jedem Refresh frisch von Twitch).
"""
import logging
import random
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from sqlmodel import Session, select

from backend import cover, quest, redeem
from backend.db import engine
from backend.models import Account, Event

logger = logging.getLogger("backend.quest")

GQL_URL = "https://gql.twitch.tv/gql"
REFRESH_WATCHING = 60.0      # s zwischen Fortschritts-Abfragen während des Schauens
REFRESH_IDLE = 300.0         # s im Leerlauf
BADGES_REFRESH = 1800.0      # s für die Badge-Sammlung
LIVE_CACHE = 120.0           # s Live-/Kategorie-Cache je Kanal
BEACON_BASE = 60.0           # s zwischen zwei minute-watched (±Jitter)
BEACON_JITTER = 0.15
BLOCK_FALLBACK_HOURS = 6.0   # Sperre, wenn kein Fensterende bekannt ist


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QuestManager(threading.Thread):
    def __init__(self, tick: float = 5.0):
        super().__init__(name="quest-manager", daemon=True)
        self.tick = tick
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._wake = threading.Event()   # UI-Aktion -> sofort neu planen

        # Twitch-Zustand
        self.lanes: list = []
        self.selected: dict = {}        # track -> lane
        self.badges: list = []          # [{setID,title,imageURL}]
        self.user_id: str = ""
        self.last_refresh = 0.0
        self.last_badges = 0.0
        self.last_error: str = ""
        # Planung
        self.decision: "quest.Decision | None" = None
        self.blocked: dict = {}         # lane -> datetime (bis wann gesperrt)
        self.resume_at: "datetime | None" = None
        # Watch-Sitzung
        self.watch: "dict | None" = None  # {lane, channel, channel_id, ..., started_at, ...}
        self._next_beacon = 0.0
        self._live_cache: dict = {}     # login -> (ts, info)
        self._spade: dict = {}          # login -> spade_url
        self.log: deque = deque(maxlen=60)
        self._account_id: "int | None" = None
        self._username: str = ""

    # ------------------------------------------------------------ lifecycle
    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        """Nach Konfig-/UI-Änderung: nächste Runde sofort neu laden."""
        self.last_refresh = 0.0
        self._wake.set()

    def run(self):
        logger.info("Quest manager started.")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                self._log(f"Fehler: {e}")
                logger.exception("quest tick failed")
            self._wake.wait(self.tick)
            self._wake.clear()
        self._stop_watch("Manager gestoppt")
        logger.info("Quest manager stopped.")

    # ------------------------------------------------------------ helpers
    def _log(self, msg: str, event: bool = False):
        line = f"{_now().strftime('%H:%M:%S')} {msg}"
        with self._lock:
            self.log.append(line)
        logger.info("[quest] %s", msg)
        if event and self._account_id is not None:
            try:
                with Session(engine) as s:
                    s.add(Event(account_id=self._account_id, type="quest",
                                message=msg[:500]))
                    s.commit()
            except Exception:  # noqa: BLE001
                logger.exception("quest event not recorded")

    def _creds(self, username: str):
        with Session(engine) as s:
            acc = s.exec(select(Account).where(
                Account.username == username)).first()
            if acc is None:
                raise RuntimeError(f"Account {username} nicht gefunden")
            token, proxies = redeem.account_creds(s, acc)
            fp = redeem.account_fp(acc)
            ua_web = acc.ua_web or ""
            self._account_id = acc.id
        return token, proxies, fp, ua_web

    def _gql(self, token, proxies, fp, op, query, variables=None):
        return redeem._gql(token, proxies, op, query, variables or {},
                           extra_headers=fp)

    # ------------------------------------------------------------ tick
    def _tick(self):
        with Session(engine) as s:
            cfg = quest.get_config(s)
            cover_cfg = cover.get_config(s)
        if not cfg["enabled"]:
            if self.watch:
                self._stop_watch("Modul deaktiviert")
            self.decision = None
            return
        username = cfg["account"]
        self._username = username
        token, proxies, fp, ua_web = self._creds(username)
        now = _now()
        mono = time.monotonic()

        # --- Fortschritt auffrischen
        interval = REFRESH_WATCHING if self.watch else REFRESH_IDLE
        if mono - self.last_refresh >= interval:
            self._refresh(token, proxies, fp)
            self.last_refresh = time.monotonic()
        if mono - self.last_badges >= BADGES_REFRESH:
            self._refresh_badges(token, proxies, fp)
            self.last_badges = time.monotonic()

        # --- laufende Sitzung bewerten
        if self.watch:
            lane = self._lane_by_id(self.watch["lane"])
            if lane is None:
                self._stop_watch("Lane verschwunden")
            else:
                if lane.minutes > self.watch["last_minutes"]:
                    self.watch["last_minutes"] = lane.minutes
                    self.watch["last_progress_at"] = now
                outcome = quest.watch_outcome(
                    lane, self.watch["start_minutes"], self.watch["started_at"],
                    self.watch["last_progress_at"], now,
                    cfg["max_minutes"], cfg["stall_minutes"])
                # Twitch kann nach dem 20. Minuten-Tick sofort den Tag gutschreiben
                # (grantCount+1) und den Minutenzähler zurücksetzen -> ebenfalls fertig.
                if lane.grants > self.watch["start_grants"] or quest.lane_done(lane):
                    outcome = "day_done"
                if outcome == "day_done":
                    self._log(f"{quest.german_name(lane.name)}: Tag {lane.grants}/{lane.repeat_times} voll "
                              f"({lane.minutes}/{lane.required_minutes} min)", event=True)
                    self._stop_watch("Tagesziel erreicht")
                    self.last_refresh = 0.0
                elif outcome in ("stalled", "capped"):
                    until = lane.window_end or (now + timedelta(hours=BLOCK_FALLBACK_HOURS))
                    self.blocked[lane.lane] = until
                    self._log(f"{quest.german_name(lane.name)}: kein Fortschritt ({outcome}, "
                              f"{lane.minutes} min) -> Lane bis {until.strftime('%d.%m. %H:%M')} UTC gesperrt",
                              event=True)
                    self._stop_watch(outcome)

        # --- planen
        selected = self._selected_for(self.lanes)
        d = quest.plan(self.lanes, selected, now, cfg["priority"], self.blocked)
        self.decision = d
        self.resume_at = d.resume_at
        if d.action != "watch":
            if self.watch:
                self._stop_watch(d.reason)
            return

        lane = d.lane
        if d.switch:
            if not self._select_lane(token, proxies, fp, lane):
                return
        if self.watch and self.watch["lane"] != lane.lane:
            self._stop_watch("Lane-Wechsel")
        if not self.watch:
            self._start_watch(token, proxies, fp, lane, cfg, cover_cfg)
        if self.watch and mono >= self._next_beacon:
            self._beacon(token, proxies, fp, ua_web)
            self._next_beacon = time.monotonic() + BEACON_BASE * random.uniform(
                1 - BEACON_JITTER, 1 + BEACON_JITTER)

    # ------------------------------------------------------------ Twitch state
    def _refresh(self, token, proxies, fp):
        data = self._gql(token, proxies, fp, "ViewerRewardDropInventory",
                         quest.GQL_INVENTORY)
        lanes = quest.parse_campaigns(data)
        self.user_id = str(((data.get("currentUser") or {}).get("id")) or self.user_id)
        tracks = sorted({l.track for l in lanes if l.track})
        selected = {}
        if tracks:
            sel = self._gql(token, proxies, fp, "ProgressionTrackSelections",
                            quest.GQL_TRACK_SELECTIONS, {"tracks": tracks})
            for t in sel.get("trackSelections") or []:
                selected[t.get("track")] = t.get("lane")
        with self._lock:
            # Fertige Stufen melden
            old = {l.group_id: l for l in self.lanes}
            for l in lanes:
                o = old.get(l.group_id)
                if o is not None and quest.lane_done(l) and not quest.lane_done(o):
                    self._log(f"🎉 {quest.german_name(l.name)} freigeschaltet!", event=True)
            self.lanes = lanes
            self.selected = selected
            self.last_error = ""
            # abgelaufene Sperren vergessen
            now = _now()
            self.blocked = {k: v for k, v in self.blocked.items() if v > now}

    def _refresh_badges(self, token, proxies, fp):
        try:
            data = self._gql(token, proxies, fp, "PokemonOwnedBadges",
                             quest.GQL_OWNED_BADGES)
            with self._lock:
                self.badges = [
                    {"set_id": b.get("setID"), "title": b.get("title"),
                     "image": b.get("imageURL")}
                    for b in ((data.get("currentUser") or {}).get("availableBadges") or [])
                ]
        except Exception as e:  # noqa: BLE001
            self._log(f"Badge-Liste nicht geladen: {e}")

    def _lane_by_id(self, lane_id):
        for l in self.lanes:
            if l.lane == lane_id:
                return l
        return None

    def _selected_for(self, lanes):
        """Die gewählte Lane des (einzigen) Tracks; bei mehreren Tracks die erste."""
        for l in lanes:
            if l.track in self.selected:
                return self.selected[l.track]
        return None

    def _select_lane(self, token, proxies, fp, lane) -> bool:
        try:
            res = self._gql(token, proxies, fp, "SetProgressionTrackSelection",
                            quest.GQL_SET_TRACK,
                            {"input": {"track": lane.track, "lane": lane.lane}})
        except Exception as e:  # noqa: BLE001
            self.last_error = f"Lane-Wechsel fehlgeschlagen: {e}"
            self._log(self.last_error, event=True)
            self.blocked[lane.lane] = _now() + timedelta(minutes=30)
            return False
        err = (res.get("setTrackSelection") or {}).get("error")
        if err:
            self.last_error = f"Lane-Wechsel abgelehnt: {err}"
            self._log(self.last_error, event=True)
            self.blocked[lane.lane] = _now() + timedelta(minutes=30)
            return False
        with self._lock:
            self.selected[lane.track] = lane.lane
        self._log(f"Lane gewechselt -> {quest.german_name(lane.name)}", event=True)
        return True

    def select_lane_manual(self, lane_id: str) -> dict:
        """UI: Lane von Hand wählen (laufende Sitzung wird beendet)."""
        lane = self._lane_by_id(lane_id)
        if lane is None:
            return {"ok": False, "error": "unbekannte Lane"}
        token, proxies, fp, _ = self._creds(self._username or quest.DEFAULT_ACCOUNT)
        ok = self._select_lane(token, proxies, fp, lane)
        if ok:
            self.blocked.pop(lane.lane, None)
            self._stop_watch("manueller Lane-Wechsel")
            self.poke()
        return {"ok": ok, "error": None if ok else self.last_error}

    # ------------------------------------------------------------ channel
    def _stream_info(self, token, proxies, fp, login: str):
        ts, info = self._live_cache.get(login, (0.0, None))
        if time.monotonic() - ts < LIVE_CACHE:
            return info
        info = None
        try:
            data = self._gql(token, proxies, fp, "VideoPlayerStreamInfoOverlayChannel",
                             quest.GQL_STREAM_INFO, {"channel": login})
            u = data.get("user") or {}
            st = u.get("stream")
            game = ((st or {}).get("game") or (u.get("broadcastSettings") or {}).get("game") or {})
            info = {
                "channel_id": u.get("id"), "live": st is not None,
                "broadcast_id": (st or {}).get("id"),
                "game_id": str(game.get("id")) if game.get("id") else None,
                "game": game.get("displayName") or game.get("name"),
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("stream info %s failed: %s", login, e)
        self._live_cache[login] = (time.monotonic(), info)
        return info

    def _start_watch(self, token, proxies, fp, lane, cfg, cover_cfg):
        pool = cfg["pool"] or list(cover_cfg.get("pool") or [])
        live = {}
        for login in pool:
            info = self._stream_info(token, proxies, fp, login)
            if info:
                live[login] = (info["game_id"], info["live"])
        channel = quest.pick_channel(pool, live, lane.categories, None)
        if channel is None:
            self.last_error = "kein Live-Kanal in einer zählenden Kategorie im Pool"
            self._log(self.last_error)
            self._next_beacon = time.monotonic()
            # kurz warten, dann erneut versuchen
            self.last_refresh = time.monotonic() - REFRESH_WATCHING + 90
            return
        info = self._stream_info(token, proxies, fp, channel)
        now = _now()
        self.watch = {
            "lane": lane.lane, "lane_name": lane.name, "channel": channel,
            "channel_id": info["channel_id"], "broadcast_id": info["broadcast_id"],
            "game": info["game"], "game_id": info["game_id"],
            "started_at": now, "start_minutes": lane.minutes,
            "start_grants": lane.grants,
            "last_minutes": lane.minutes, "last_progress_at": now,
            "beacons": 0, "beacon_errors": 0,
        }
        self._next_beacon = time.monotonic()
        self._log(f"Schaue {channel} ({info['game']}) für {quest.german_name(lane.name)} "
                  f"— Tag {lane.grants + 1}/{lane.repeat_times}, {lane.minutes}/{lane.required_minutes} min",
                  event=True)

    def _stop_watch(self, reason: str):
        if self.watch:
            self._log(f"Watch beendet ({reason}): {self.watch['channel']}, "
                      f"{self.watch['beacons']} Beacons")
        self.watch = None

    # ------------------------------------------------------------ beacon
    def _spade_url(self, login: str, ua_web: str, proxies) -> str:
        url = self._spade.get(login)
        if url:
            return url
        headers = {"User-Agent": ua_web or redeem._FALLBACK_TV_UA}
        page = requests.get(f"https://www.twitch.tv/{login}", headers=headers,
                            proxies=proxies, timeout=20).text
        m = re.search(r"(https://static\.twitchcdn\.net/config/settings.*?js|"
                      r"https://assets\.twitch\.tv/config/settings.*?\.js)", page)
        if not m:
            raise RuntimeError("settings.js nicht gefunden")
        settings = requests.get(m.group(1), headers=headers, proxies=proxies,
                                timeout=20).text
        m2 = re.search(r'"spade_url":"(.*?)"', settings)
        if not m2:
            raise RuntimeError("spade_url nicht gefunden")
        self._spade[login] = m2.group(1)
        return m2.group(1)

    def _beacon(self, token, proxies, fp, ua_web):
        w = self.watch
        if not w:
            return
        login = w["channel"]
        ua = fp.get("User-Agent") or redeem._FALLBACK_TV_UA
        try:
            info = self._stream_info(token, proxies, fp, login)
            lane = self._lane_by_id(w["lane"])
            if not info or not info["live"] or lane is None or (
                    info["game_id"] not in lane.categories):
                self._stop_watch("Kanal offline oder Kategorie gewechselt")
                return
            w["broadcast_id"] = info["broadcast_id"] or w["broadcast_id"]
            pat = self._gql(token, proxies, fp, "PlaybackAccessToken",
                            quest.GQL_PLAYBACK_TOKEN,
                            {"login": login, "isLive": True, "isVod": False,
                             "vodID": "", "playerType": "site"})
            tok = pat.get("streamPlaybackAccessToken") or {}
            sig, val = tok.get("signature"), tok.get("value")
            if not sig or not val:
                raise RuntimeError("kein PlaybackAccessToken")
            m3u8 = requests.get(
                f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8",
                params={"sig": sig, "token": val}, headers={"User-Agent": ua},
                proxies=proxies, timeout=20)
            if m3u8.status_code != 200:
                raise RuntimeError(f"usher {m3u8.status_code}")
            lowest = [l for l in m3u8.text.split("\n") if l.startswith("http")][-1]
            pl = requests.get(lowest, headers={"User-Agent": ua}, proxies=proxies, timeout=20)
            if pl.status_code != 200:
                raise RuntimeError(f"playlist {pl.status_code}")
            segs = [l for l in pl.text.split("\n") if l.startswith("http")]
            if not segs:
                raise RuntimeError("keine Segmente")
            requests.head(segs[-1], headers={"User-Agent": ua}, proxies=proxies, timeout=20)
            spade = self._spade_url(login, ua_web, proxies)
            payload = quest.minute_watched_payload(
                channel_id=w["channel_id"], broadcast_id=w["broadcast_id"],
                user_id=self.user_id, login=login, game=w["game"], game_id=w["game_id"])
            r = requests.post(spade, data={"data": payload}, headers={"User-Agent": ua},
                              proxies=proxies, timeout=20)
            if r.status_code != 204:
                raise RuntimeError(f"spade {r.status_code}")
            w["beacons"] += 1
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            w["beacon_errors"] += 1
            self.last_error = f"Beacon-Fehler: {e}"
            logger.warning("quest beacon failed: %s", e)
            if w["beacon_errors"] >= 10 and w["beacons"] == 0:
                self._stop_watch("Beacons schlagen dauerhaft fehl")
                self._live_cache.pop(login, None)

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        with self._lock:
            now = _now()
            w = None
            if self.watch:
                w = {k: (v.isoformat() if isinstance(v, datetime) else v)
                     for k, v in self.watch.items()}
                w["lane_name_de"] = quest.german_name(self.watch["lane_name"])
            selected = self._selected_for(self.lanes)
            lanes = []
            for l in self.lanes:
                d = l.to_dict()
                d["selected"] = (l.lane == selected)
                d["day_complete"] = quest.day_complete(l, now)
                r = quest.ready_at(l, now)
                d["ready_at"] = r.isoformat() if r else None
                b = self.blocked.get(l.lane)
                d["blocked_until"] = b.isoformat() if b and b > now else None
                lanes.append(d)
            return {
                "account": self._username,
                "user_id": self.user_id,
                "lanes": lanes,
                "badges": list(self.badges),
                "watch": w,
                "decision": ({"action": self.decision.action, "reason": self.decision.reason,
                              "lane": self.decision.lane.lane if self.decision.lane else None,
                              "resume_at": self.decision.resume_at.isoformat()
                              if self.decision.resume_at else None}
                             if self.decision else None),
                "last_error": self.last_error,
                "last_refresh_age": (time.monotonic() - self.last_refresh)
                if self.last_refresh else None,
                "log": list(self.log)[-30:],
            }


quest_manager = QuestManager()
