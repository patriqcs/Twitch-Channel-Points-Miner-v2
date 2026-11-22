# -*- coding: utf-8 -*-
"""Badge-Quest-Manager: farmt Twitch Reward-Drops (Pokémon-Badges, GTA-VI-Ketten)
für EINEN Account (Default: der Hauptaccount), ohne dessen laufenden Miner anzufassen.
Kampagnen ohne Lane (GTA VI) laufen denselben Weg, nur ohne Lane-Wechsel.

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
PLAYLIST_INTERVAL = 4.0      # s zwischen Playlist-/Segment-Abrufen (wie ein Player)
MINUTE_INTERVAL = 60.0       # s zwischen zwei minute-watched
BLOCK_FALLBACK_HOURS = 6.0   # Sperre, wenn kein Fensterende bekannt ist
DISCOVERY_INTERVAL = 6 * 3600.0  # s zwischen zwei Abfragen der Kampagnenliste
DISCOVERY_RETRY = 1800.0     # s bis zum nächsten Versuch nach einem Fehler


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
        self._player: "dict | None" = None  # laufende Player-Session (Manifest, Session, Zähler)
        self._live_cache: dict = {}     # login -> (ts, info)
        self._device_id: str = ""
        self._bad_channels: dict = {}   # login -> datetime: hat zuletzt nicht gezählt
        self._lane_stalls: dict = {}    # lane -> Anzahl Stalls heute
        self._ref_channel_id: str = ""  # zuletzt geschauter Kanal (für Kanal-Fortschritt)
        # Kampagnenliste (alle laufenden, auch nicht begonnene)
        self.discovered: list = []      # quest.parse_discovery()
        self.next_discovery = 0.0       # monotonic
        self._inventory_ids: set = set()
        self._cfg: dict = {}
        self.log: deque = deque(maxlen=60)
        self._last_nochannel_log = 0.0
        self._last_dir_lookup = 0.0
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
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
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
            self._device_id = acc.device_id or ""
        return token, proxies, fp, ua_web

    def _gql(self, token, proxies, fp, op, query, variables=None):
        return redeem._gql(token, proxies, op, query, variables or {},
                           extra_headers=fp)

    # ------------------------------------------------------------ tick
    def _tick(self):
        with Session(engine) as s:
            cfg = quest.get_config(s)
            cover_cfg = cover.get_config(s)
        self._cfg = cfg
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

        # --- Fortschritt auffrischen (Badges zuerst: _refresh braucht den Besitz)
        if mono - self.last_badges >= BADGES_REFRESH:
            self._refresh_badges(token, proxies, fp)
            self.last_badges = time.monotonic()
        interval = REFRESH_WATCHING if self.watch else REFRESH_IDLE
        if mono - self.last_refresh >= interval:
            self._refresh(token, proxies, fp)
            self.last_refresh = time.monotonic()
        # Nach dem Inventar, damit begonnene Kampagnen nicht als neu gemeldet werden;
        # findet die Liste Neues, wird das Inventar gleich noch einmal geladen.
        if mono >= self.next_discovery:
            self._discover(token, proxies, fp)

        # --- laufende Sitzung bewerten
        if self.watch:
            lane = self._lane_by_group(self.watch["group_id"])
            if lane is None:
                self._stop_watch("Reward-Gruppe verschwunden")
            else:
                if lane.minutes > self.watch["last_minutes"]:
                    self.watch["last_minutes"] = lane.minutes
                    self.watch["last_progress_at"] = now
                elif lane.minutes < self.watch["last_minutes"]:
                    # Neues Tagesfenster: Twitch hat den Zähler zurückgesetzt. Ohne das
                    # sähe eine Sitzung, die mit dem alten Stand (z. B. 60/60) gestartet
                    # ist, wie Stillstand aus.
                    self.watch["last_minutes"] = lane.minutes
                    self.watch["start_minutes"] = min(self.watch["start_minutes"], lane.minutes)
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
                    self._lane_stalls.pop(lane.key, None)
                    self._log(f"{quest.german_name(lane.name)}: Tag {lane.grants}/{lane.repeat_times} voll "
                              f"({lane.minutes}/{lane.required_minutes} min)", event=True)
                    self._stop_watch("Tagesziel erreicht")
                    self.last_refresh = 0.0
                elif outcome in ("stalled", "capped"):
                    ch = self.watch["channel"]
                    n = self._lane_stalls.get(lane.key, 0) + 1
                    self._lane_stalls[lane.key] = n
                    # Erst den Kanal wechseln (vielleicht zählt nur dieser nicht),
                    # erst beim zweiten Fehlschlag die Lane für heute pausieren.
                    self._bad_channels[ch] = now + timedelta(hours=BLOCK_FALLBACK_HOURS)
                    if n < 2:
                        self._log(f"{quest.german_name(lane.name)}: kein Fortschritt auf {ch} ({outcome}, "
                                  f"{lane.minutes} min) -> anderer Kanal", event=True)
                        self._stop_watch(outcome)
                    else:
                        until = lane.window_end or (now + timedelta(hours=BLOCK_FALLBACK_HOURS))
                        self.blocked[lane.key] = until
                        self._log(f"{quest.german_name(lane.name)}: kein Fortschritt auch auf {ch} ({outcome}, "
                                  f"{lane.minutes} min) -> Lane bis {until.astimezone().strftime('%d.%m. %H:%M')} pausiert",
                                  event=True)
                        self._stop_watch(outcome)

        # --- planen
        selected = self._selected_for(self.lanes)
        d = quest.plan([l for l in self.lanes if l.enabled], selected, now,
                       cfg["priority"], self.blocked)
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
        if self.watch and self.watch["group_id"] != lane.group_id:
            self._stop_watch("Wechsel der Reward-Gruppe")
        if not self.watch:
            self._start_watch(token, proxies, fp, lane, cfg, cover_cfg)
        if self.watch:
            self._player_tick(token, proxies, fp, ua_web)

    # ------------------------------------------------------------ Twitch state
    def _refresh(self, token, proxies, fp):
        data = self._gql(token, proxies, fp, "ViewerRewardDropInventory",
                         quest.GQL_INVENTORY)
        # Abschluss-Belohnungen (Golden Iguana) stehen nur im Kanal-Fortschritt,
        # nicht im Inventar -> vom zuletzt geschauten Kanal dazuholen.
        extra = None
        if self._ref_channel_id:
            try:
                cp = self._gql(token, proxies, fp, "DropChannelCampaignsProgress",
                               quest.GQL_CHANNEL_PROGRESS,
                               {"channelID": self._ref_channel_id})
                extra = cp.get("channelDropCampaignsProgress") or None
            except Exception as e:  # noqa: BLE001
                logger.debug("channel progress failed: %s", e)
        cfg = self._cfg
        inv = ((data.get("currentUser") or {}).get("inventory") or {})
        inventory_ids = {c.get("id") for c in
                         inv.get("viewerRewardDropCampaignsInProgress") or []}
        # Freigegebene, aber noch nicht begonnene Kampagnen anstoßen.
        seeds = [c["raw"] for c in self.discovered
                 if quest.campaign_state(c["id"], c["auto_key"], False, cfg) == "on"]
        lanes = quest.parse_campaigns(data, now=_now(), extra=extra, seeds=seeds)
        owned = {b.get("set_id") for b in self.badges}
        off = set(cfg.get("campaigns_off") or [])
        kept = []
        for l in lanes:
            # Fällt eine fertige Kampagne aus dem Inventar, sähe sie in der Liste
            # wieder wie neu aus -> nicht noch einmal anfangen, wenn alle Abzeichen da sind.
            sets = {p.get("badge_set") for p in l.pool} | {l.badge_set}
            sets.discard("")
            if l.seed and sets and sets <= owned:
                continue
            l.enabled = l.campaign_id not in off
            kept.append(l)
        lanes = kept
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
            self._inventory_ids = inventory_ids
            self.last_error = ""
            # abgelaufene Sperren vergessen
            now = _now()
            self.blocked = {k: v for k, v in self.blocked.items() if v > now}

    def _discover(self, token, proxies, fp):
        """Kampagnenliste holen und neue Kampagnen einmalig melden."""
        try:
            data = self._gql(token, proxies, fp, "NewAllCampaignsPage", quest.GQL_DISCOVERY)
            found = quest.parse_discovery(data, now=_now())
        except Exception as e:  # noqa: BLE001
            self._log(f"Kampagnenliste nicht geladen: {e}")
            self.next_discovery = time.monotonic() + DISCOVERY_RETRY
            return
        self.next_discovery = time.monotonic() + DISCOVERY_INTERVAL
        with Session(engine) as s:
            seen = set(quest.get_list(s, quest.SEEN_KEY))
            fresh = [c for c in found if c["id"] not in seen]
            if fresh or seen - {c["id"] for c in found}:
                quest.set_list(s, quest.SEEN_KEY, [c["id"] for c in found])
                s.commit()
        for c in fresh:
            state = quest.campaign_state(c["id"], c["auto_key"],
                                         c["id"] in self._inventory_ids, self._cfg)
            hint = {"on": "wird gefarmt", "off": "abgeschaltet",
                    "new": "wartet auf Freigabe im Tab Badges"}[state]
            self._log(f"Neue Kampagne: {c['name']} ({c['game'] or c['brand'] or 'Twitch'}) — {hint}",
                      event=True)
        with self._lock:
            self.discovered = found
        if fresh:
            self.last_refresh = 0.0

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
        """Erste OFFENE Gruppe der Lane (Glutexo/Glurak teilen sich eine Lane-ID)."""
        open_ = [l for l in self.lanes if l.lane == lane_id and not quest.lane_done(l)]
        if open_:
            return open_[0]
        for l in self.lanes:
            if l.lane == lane_id:
                return l
        return None

    def _lane_by_group(self, group_id):
        for l in self.lanes:
            if l.group_id == group_id:
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
            self.blocked[lane.key] = _now() + timedelta(minutes=30)
            return False
        err = (res.get("setTrackSelection") or {}).get("error")
        if err:
            self.last_error = f"Lane-Wechsel abgelehnt: {err}"
            self._log(self.last_error, event=True)
            self.blocked[lane.key] = _now() + timedelta(minutes=30)
            return False
        with self._lock:
            self.selected[lane.track] = lane.lane
        self._log(f"Lane gewechselt -> {quest.german_name(lane.name)}", event=True)
        return True

    def select_lane_manual(self, lane_id: str) -> dict:
        """UI: Lane von Hand wählen (laufende Sitzung wird beendet)."""
        lane = self._lane_by_id(lane_id)
        if lane is None or lane.trackless:
            return {"ok": False, "error": "unbekannte Lane"}
        token, proxies, fp, _ = self._creds(self._username or quest.DEFAULT_ACCOUNT)
        ok = self._select_lane(token, proxies, fp, lane)
        if ok:
            self.blocked.pop(lane.key, None)
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
        now0 = _now()
        self._bad_channels = {k: v for k, v in self._bad_channels.items() if v > now0}
        bad = set(self._bad_channels)
        channel = quest.pick_channel(pool, live, lane.categories, None, exclude=bad)
        if channel is None:
            channel = self._channel_from_directory(token, proxies, fp, lane, exclude=bad)
        if channel is None:
            self.last_error = "kein Live-Kanal in einer zählenden Kategorie gefunden"
            if time.monotonic() - self._last_nochannel_log > 600:
                self._log(self.last_error)
                self._last_nochannel_log = time.monotonic()
            return
        info = self._stream_info(token, proxies, fp, channel)
        if not info or not info["live"]:
            return
        now = _now()
        # Minutenstand zählt nur, solange das Fenster noch offen ist; ein
        # abgelaufenes Fenster zeigt bis zum nächsten Refresh den alten Stand.
        window_open = lane.window_end is not None and now < lane.window_end
        minutes0 = lane.minutes if window_open else 0
        self.watch = {
            "lane": lane.lane, "key": lane.key, "group_id": lane.group_id,
            "lane_name": lane.name, "channel": channel,
            "channel_id": info["channel_id"], "broadcast_id": info["broadcast_id"],
            "game": info["game"], "game_id": info["game_id"],
            "started_at": now, "start_minutes": minutes0,
            "start_grants": lane.grants,
            "last_minutes": minutes0, "last_progress_at": now,
            "beacons": 0, "beacon_errors": 0, "bytes_kb": 0, "minutes_logged": 0,
        }
        self._player = None
        self._ref_channel_id = info["channel_id"] or self._ref_channel_id
        self._log(f"Schaue {channel} ({info['game']}) für {quest.german_name(lane.name)} "
                  f"— Tag {lane.grants + 1}/{lane.repeat_times}, {minutes0}/{lane.required_minutes} min",
                  event=True)

    def _channel_from_directory(self, token, proxies, fp, lane, exclude=None):
        """Fallback: größter (bevorzugt deutscher) Live-Stream einer zählenden Kategorie.
        Höchstens alle 2 Minuten abgefragt (Cache wie _stream_info)."""
        if time.monotonic() - self._last_dir_lookup < LIVE_CACHE:
            return None
        self._last_dir_lookup = time.monotonic()
        # Just Chatting zuerst (immer gut gefüllt), dann die übrigen Kategorien.
        cats = sorted(lane.categories, key=lambda c: (c != "509658", c))
        for cat in cats[:4]:
            for langs in (["DE"], None):
                try:
                    d = self._gql(token, proxies, fp, "QuestDirectory", quest.GQL_DIRECTORY,
                                  {"id": cat, "langs": langs})
                except Exception as e:  # noqa: BLE001
                    logger.debug("directory %s failed: %s", cat, e)
                    continue
                edges = ((d.get("game") or {}).get("streams") or {}).get("edges") or []
                login = quest.pick_from_directory(edges, lane.categories, exclude=exclude)
                if login:
                    self._live_cache.pop(login, None)  # frisch holen
                    return login
        return None

    def _stop_watch(self, reason: str):
        if self.watch:
            self._log(f"Watch beendet ({reason}): {self.watch['channel']}, "
                      f"{self.watch['beacons']} Minuten gemeldet")
        self.watch = None
        self._player = None
        self._last_dir_lookup = 0.0  # nächster Start darf sofort im Verzeichnis suchen

    # ------------------------------------------------------------ player session
    # Twitch zählt Reward-Drop-Minuten nur über die Player-Events an die Edge-
    # Tracking-URL aus dem Master-Manifest (siehe quest.parse_master_manifest);
    # das alte spade.twitch.tv-Beacon des Miners zählt hier nachweislich nicht
    # (Sonden 2026-11-04). Deshalb: echte Player-Session — Master-Manifest holen,
    # `video-play`, niedrigste Variante laufend laden, jede Minute `minute-watched`.
    def _web_headers(self, ua_web: str) -> dict:
        return {"User-Agent": ua_web or redeem._FALLBACK_TV_UA,
                "Origin": "https://www.twitch.tv", "Referer": "https://www.twitch.tv/"}

    def _open_player(self, token, proxies, fp, ua_web, login):
        pat = self._gql(token, proxies, fp, "PlaybackAccessToken", quest.GQL_PLAYBACK_TOKEN,
                        {"login": login, "isLive": True, "isVod": False, "vodID": "",
                         "playerType": "site"})
        tok = pat.get("streamPlaybackAccessToken") or {}
        if not tok.get("signature") or not tok.get("value"):
            raise RuntimeError("kein PlaybackAccessToken")
        sess = requests.Session()
        r = sess.get(f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8",
                     params={"sig": tok["signature"], "token": tok["value"],
                             "allow_source": "true", "player_backend": "mediaplayer",
                             "playlist_include_framerate": "true", "fast_bread": "true",
                             "p": str(random.randint(1000000, 9999999))},
                     headers=self._web_headers(ua_web), proxies=proxies, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"usher {r.status_code}")
        man = quest.parse_master_manifest(r.text)
        if not man.get("track_url") or not man.get("lowest_url"):
            raise RuntimeError("Master-Manifest ohne Tracking-URL/Variante")
        return {"manifest": man, "psid": quest.new_play_session_id(), "sess": sess,
                "seen": set(), "minutes": 0, "t0": time.monotonic(),
                "last_minute": time.monotonic(), "last_playlist": 0.0, "bytes": 0,
                "errors": 0}

    def _send_event(self, pl, event, login, channel_id, ua_web, proxies, w):
        payload = quest.player_event_payload(
            event, manifest=pl["manifest"], login=login, channel_id=channel_id,
            user_id=self.user_id, user_login=(self._username or "").lower(),
            device_id=self._device_id, user_agent=ua_web or redeem._FALLBACK_TV_UA,
            play_session_id=pl["psid"], game=w.get("game"), game_id=w.get("game_id"),
            minutes_logged=pl["minutes"], position=time.monotonic() - pl["t0"])
        r = pl["sess"].post(pl["manifest"]["track_url"], data={"data": payload},
                            headers=self._web_headers(ua_web), proxies=proxies, timeout=20)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"tracking {r.status_code}")

    def _player_tick(self, token, proxies, fp, ua_web):
        w = self.watch
        if not w:
            return
        login = w["channel"]
        mono = time.monotonic()
        try:
            # Live-/Kategorie-Check (gecacht, alle LIVE_CACHE Sekunden)
            info = self._stream_info(token, proxies, fp, login)
            lane = self._lane_by_group(w["group_id"])
            if not info or not info["live"] or lane is None or (
                    info["game_id"] not in lane.categories):
                self._stop_watch("Kanal offline oder Kategorie gewechselt")
                return
            pl = self._player
            if pl is None:
                pl = self._player = self._open_player(token, proxies, fp, ua_web, login)
                w["broadcast_id"] = pl["manifest"].get("broadcast_id") or w["broadcast_id"]
                self._send_event(pl, "video-play", login, w["channel_id"], ua_web, proxies, w)
                self._log(f"Player-Session für {login} geöffnet (Edge-Tracking)")
            if mono - pl["last_playlist"] >= PLAYLIST_INTERVAL:
                pl["last_playlist"] = mono
                r = pl["sess"].get(pl["manifest"]["lowest_url"], headers=self._web_headers(ua_web),
                                   proxies=proxies, timeout=20)
                if r.status_code != 200:
                    raise RuntimeError(f"playlist {r.status_code}")
                for seg in quest.new_segments(r.text, pl["seen"], limit=2):
                    pl["seen"].add(seg)
                    pl["bytes"] += len(pl["sess"].get(seg, headers=self._web_headers(ua_web),
                                                      proxies=proxies, timeout=20).content)
                if len(pl["seen"]) > 500:
                    pl["seen"] = set(list(pl["seen"])[-100:])
            if mono - pl["last_minute"] >= MINUTE_INTERVAL * random.uniform(0.97, 1.03):
                pl["last_minute"] = mono
                pl["minutes"] += 1
                self._send_event(pl, "minute-watched", login, w["channel_id"], ua_web, proxies, w)
                w["beacons"] += 1
            pl["errors"] = 0
            w["bytes_kb"] = pl["bytes"] // 1024
            w["minutes_logged"] = pl["minutes"]
            self.last_error = ""
        except Exception as e:  # noqa: BLE001
            w["beacon_errors"] += 1
            self.last_error = f"Player-Fehler: {e}"
            logger.warning("quest player failed: %s", e)
            if self._player is not None:
                self._player["errors"] += 1
                if self._player["errors"] >= 5:
                    self._log("Player-Session neu aufbauen (Fehlerserie)")
                    self._player = None
            if w["beacon_errors"] >= 20 and w["beacons"] == 0:
                self._stop_watch("Player-Session schlägt dauerhaft fehl")
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
                b = self.blocked.get(l.key)
                d["blocked_until"] = b.isoformat() if b and b > now else None
                lanes.append(d)
            listed = {c["id"] for c in self.discovered}
            campaigns = []
            owned = {b.get("set_id") for b in self.badges}
            for c in self.discovered:
                e = {k: v for k, v in c.items() if k != "raw"}
                e["in_inventory"] = c["id"] in self._inventory_ids
                e["state"] = quest.campaign_state(c["id"], c["auto_key"],
                                                  e["in_inventory"], self._cfg)
                # Fertige Kampagnen fallen aus dem Inventar und sähen sonst wie neu aus.
                e["complete"] = bool(c["badge_sets"]) and set(c["badge_sets"]) <= owned
                e["auto"] = bool(c["auto_key"]) and c["auto_key"] in (self._cfg.get("auto_keys") or [])
                campaigns.append(e)
            # Begonnene Kampagnen, die die Liste nicht (mehr) führt, bleiben schaltbar.
            for l in self.lanes:
                if l.campaign_id in listed:
                    continue
                listed.add(l.campaign_id)
                campaigns.append({
                    "id": l.campaign_id, "name": l.campaign_name, "brand": "", "game": "",
                    "image": "", "start": None,
                    "end": l.campaign_end.isoformat() if l.campaign_end else None,
                    "upcoming": False, "auto_key": "", "auto_label": "", "groups": [],
                    "in_inventory": True, "auto": False, "complete": False, "badge_sets": [],
                    "state": quest.campaign_state(l.campaign_id, "", True, self._cfg),
                })
            return {
                "account": self._username,
                "campaigns": campaigns,
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
