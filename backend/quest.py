# -*- coding: utf-8 -*-
"""Badge-Quests (Twitch Reward-Drops, z. B. Pokémon-Chat-Badges) — Logik.

Twitch zählt diese Badges NICHT über die klassischen `DropCampaign`/
`timeBasedDrops`, sondern über `DropsCampaign.rewardGroups` mit Tageszähler:
20 Minuten pro rollierendem 24-h-Fenster, an `repeatableTimes` Tagen, und nur
für die im Track ausgewählte Lane (`isAutoProgressable: false`). Jede Lane
(= ein Pokémon) führt ihr eigenes Fenster, deshalb lohnt die **Rotation**: ist
der Tag einer Lane voll, wird per `SetProgressionTrackSelection` die nächste
Lane ausgewählt und weitergeschaut. Sind alle Lanes für heute voll, wird
pausiert, bis das früheste Fenster abläuft.

Dieses Modul enthält nur reine Logik (Parsen, Planen, Kanalwahl, Payload) und
die GQL-Texte. Netz, Thread und Zustand liegen in quest_manager.py.
"""
import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlmodel import Session

from backend.models import AppSetting

# ---------------------------------------------------------------- Settings
ENABLED_KEY = "QUEST_ENABLED"
ACCOUNT_KEY = "QUEST_ACCOUNT"            # Username (Default: patriQcs)
PRIORITY_KEY = "QUEST_PRIORITY"          # Lane-Reihenfolge, Namen kommasepariert
POOL_KEY = "QUEST_POOL"                  # eigener Kanal-Pool (leer -> Tarn-Pool)
MAX_MINUTES_KEY = "QUEST_MAX_MINUTES"    # Deckel je Lane und Tag (Stall-Schutz)
STALL_MINUTES_KEY = "QUEST_STALL_MINUTES"  # ohne Fortschritt -> Lane heute sperren

DEFAULT_ACCOUNT = "patriQcs"
DEFAULT_MAX_MINUTES = 35
DEFAULT_STALL_MINUTES = 8

GERMAN_NAMES = {
    "pichu": "Pichu", "pikachu": "Pikachu", "raichu": "Raichu",
    "bulbasaur": "Bisasam", "ivysaur": "Bisaknosp", "venusaur": "Bisaflor",
    "charmander": "Glumanda", "charmeleon": "Glutexo", "charizard": "Glurak",
    "squirtle": "Shiggy", "wartortle": "Schillok", "blastoise": "Turtok",
    "gastly": "Nebulak", "haunter": "Alpollo", "gengar": "Gengar",
}


def german_name(name: str) -> str:
    return GERMAN_NAMES.get((name or "").strip().lower(), name)


def parse_priority(raw: str) -> list:
    out = []
    for part in (raw or "").replace("\n", ",").split(","):
        p = part.strip().lower()
        if p and p not in out:
            out.append(p)
    return out


def _get(session: Session, key: str, default=None):
    s = session.get(AppSetting, key)
    return s.value if s is not None else default


def set_setting(session: Session, key: str, value: str) -> None:
    s = session.get(AppSetting, key)
    if s is None:
        session.add(AppSetting(key=key, value=value))
    else:
        s.value = value
        session.add(s)


def _as_int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


def get_config(session: Session) -> dict:
    return {
        "enabled": (_get(session, ENABLED_KEY, "0") or "0").strip().lower()
        in ("1", "true", "yes", "on"),
        "account": (_get(session, ACCOUNT_KEY, DEFAULT_ACCOUNT) or DEFAULT_ACCOUNT).strip(),
        "priority": parse_priority(_get(session, PRIORITY_KEY, "") or ""),
        "priority_raw": _get(session, PRIORITY_KEY, "") or "",
        "pool": parse_priority(_get(session, POOL_KEY, "") or ""),
        "pool_raw": _get(session, POOL_KEY, "") or "",
        "max_minutes": _as_int(_get(session, MAX_MINUTES_KEY, DEFAULT_MAX_MINUTES),
                               DEFAULT_MAX_MINUTES, 20, 180),
        "stall_minutes": _as_int(_get(session, STALL_MINUTES_KEY, DEFAULT_STALL_MINUTES),
                                 DEFAULT_STALL_MINUTES, 3, 60),
    }


# ---------------------------------------------------------------- GQL texts
# Volltext-Queries (aus den Twitch-Web-Bundles rekonstruiert). Volltext statt
# persisted hash, damit eine APQ-Rotation uns nicht trifft.
_FRAG_REWARDS = (
    "fragment questRewards on DropsReward { id name thumbnailURL distributionType "
    "dropDetail { badge { id setID } } "
    "pool { id rewards { id name thumbnailURL dropDetail { badge { id setID } } } } }"
)
_FRAG_GROUPS = (
    "fragment questCampaign on DropsCampaign { id status name brandName description "
    "detailsURL imageURL startAt endAt rewardGroups { id name track lane "
    "progressCriteria { requirementType requirements { minutesWatched subs } "
    "isRepeatable isAutoProgressable repeatableConfig { repeatableTimes "
    "resetIntervalSeconds shouldCompleteAllRepeatsToEarn } "
    "categories { id displayName } channels { id } } "
    "self { status currentMinutesWatched currentSubs grantCount nextEligibleAt "
    "currentWindow { startedAt expiresAt } hasPreconditionsMet "
    "earnedReward { id name thumbnailURL } } rewards { ...questRewards } } }"
)
GQL_INVENTORY = (
    "query ViewerRewardDropInventory { currentUser { id inventory { "
    "viewerRewardDropCampaignsInProgress { ...questCampaign } } } } "
    + _FRAG_GROUPS + " " + _FRAG_REWARDS
)
GQL_CHANNEL_PROGRESS = (
    "query DropChannelCampaignsProgress($channelID: ID!) { "
    "channelDropCampaignsProgress(channelID: $channelID) { ...questCampaign } } "
    + _FRAG_GROUPS + " " + _FRAG_REWARDS
)
GQL_TRACK_SELECTIONS = (
    "query ProgressionTrackSelections($tracks: [ID!]!) { "
    "trackSelections(tracks: $tracks) { id track lane } }"
)
GQL_SET_TRACK = (
    "mutation SetProgressionTrackSelection($input: SetTrackSelectionInput!) { "
    "setTrackSelection(input: $input) { error } }"
)
GQL_OWNED_BADGES = (
    "query PokemonOwnedBadges { currentUser { id availableBadges { id setID title "
    "imageURL(size: QUADRUPLE) } } }"
)
GQL_STREAM_INFO = (
    "query VideoPlayerStreamInfoOverlayChannel($channel: String!) { "
    "user(login: $channel) { id broadcastSettings { id title game { id name displayName } } "
    "stream { id viewersCount game { id displayName } } } }"
)
# Verzeichnis-Fallback: größte Live-Streams einer Kategorie (bevorzugt deutsch),
# falls im Kanal-Pool gerade niemand in einer zählenden Kategorie live ist.
GQL_DIRECTORY = (
    "query QuestDirectory($id: ID!, $langs: [String!]) { game(id: $id) { id displayName "
    "streams(first: 15, options: {sort: VIEWER_COUNT, languages: $langs}) { edges { node { "
    "id viewersCount language broadcaster { id login displayName } game { id displayName } } } } } }"
)
GQL_PLAYBACK_TOKEN = (
    "query PlaybackAccessToken($login: String!, $isLive: Boolean!, $vodID: ID!, "
    "$isVod: Boolean!, $playerType: String!) { "
    "streamPlaybackAccessToken(channelName: $login, params: {platform: \"web\", "
    "playerBackend: \"mediaplayer\", playerType: $playerType}) @include(if: $isLive) "
    "{ value signature } "
    "videoPlaybackAccessToken(id: $vodID, params: {platform: \"web\", "
    "playerBackend: \"mediaplayer\", playerType: $playerType}) @include(if: $isVod) "
    "{ value signature } }"
)


# ---------------------------------------------------------------- model
def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class Lane:
    """Eine rewardGroup mit WATCH-Kriterium = ein verdienbares Pokémon."""
    group_id: str
    name: str
    track: str
    lane: str
    campaign_id: str
    campaign_name: str
    campaign_end: "datetime | None"
    required_minutes: int = 20
    repeat_times: int = 3
    reset_seconds: int = 86400
    status: str = ""
    minutes: int = 0
    grants: int = 0
    window_start: "datetime | None" = None
    window_end: "datetime | None" = None
    next_eligible: "datetime | None" = None
    preconditions_met: bool = False
    earned: bool = False
    categories: set = field(default_factory=set)
    category_names: list = field(default_factory=list)
    thumbnail: str = ""
    badge_set: str = ""

    def to_dict(self) -> dict:
        iso = lambda d: d.isoformat() if d else None  # noqa: E731
        return {
            "group_id": self.group_id, "name": self.name,
            "name_de": german_name(self.name), "track": self.track,
            "lane": self.lane, "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name, "campaign_end": iso(self.campaign_end),
            "required_minutes": self.required_minutes, "repeat_times": self.repeat_times,
            "status": self.status, "minutes": self.minutes, "grants": self.grants,
            "window_start": iso(self.window_start), "window_end": iso(self.window_end),
            "next_eligible": iso(self.next_eligible),
            "preconditions_met": self.preconditions_met, "earned": self.earned,
            "done": lane_done(self), "categories": sorted(self.categories),
            "category_names": self.category_names, "thumbnail": self.thumbnail,
            "badge_set": self.badge_set,
        }


def parse_campaigns(data: dict, now: "datetime | None" = None) -> list:
    """`ViewerRewardDropInventory`-Antwort -> Lanes (nur WATCH-Gruppen aktiver Kampagnen).

    Twitch lässt abgelaufene Kampagnen (status EXPIRED, endAt vorbei) noch im
    Inventar stehen; die fliegen hier raus, damit sie weder geplant noch angezeigt werden."""
    now = now or datetime.now(timezone.utc)
    lanes = []
    inv = ((data or {}).get("currentUser") or {}).get("inventory") or {}
    for camp in inv.get("viewerRewardDropCampaignsInProgress") or []:
        if (camp.get("status") or "").upper() == "EXPIRED":
            continue
        end = _parse_dt(camp.get("endAt"))
        if end is not None and end <= now:
            continue
        for g in camp.get("rewardGroups") or []:
            lane = _lane_from_group(camp, g)
            if lane is not None:
                lanes.append(lane)
    return lanes


def _lane_from_group(camp: dict, g: dict):
    pc = g.get("progressCriteria") or {}
    req = pc.get("requirements") or {}
    if (pc.get("requirementType") or "").upper() != "WATCH":
        return None
    if not req.get("minutesWatched"):
        return None
    rc = pc.get("repeatableConfig") or {}
    me = g.get("self") or {}
    win = me.get("currentWindow") or {}
    rewards = g.get("rewards") or []
    thumb = ""
    badge_set = ""
    for r in rewards:
        thumb = thumb or (r.get("thumbnailURL") or "")
        badge_set = badge_set or (((r.get("dropDetail") or {}).get("badge") or {}).get("setID") or "")
    return Lane(
        group_id=g.get("id") or "", name=g.get("name") or "",
        track=g.get("track") or "", lane=g.get("lane") or "",
        campaign_id=camp.get("id") or "", campaign_name=camp.get("name") or "",
        campaign_end=_parse_dt(camp.get("endAt")),
        required_minutes=int(req.get("minutesWatched") or 20),
        repeat_times=int(rc.get("repeatableTimes") or 1),
        reset_seconds=int(rc.get("resetIntervalSeconds") or 86400),
        status=me.get("status") or "",
        minutes=int(me.get("currentMinutesWatched") or 0),
        grants=int(me.get("grantCount") or 0),
        window_start=_parse_dt(win.get("startedAt")),
        window_end=_parse_dt(win.get("expiresAt")),
        next_eligible=_parse_dt(me.get("nextEligibleAt")),
        preconditions_met=bool(me.get("hasPreconditionsMet")) if g.get("self") else False,
        earned=bool(me.get("earnedReward")),
        categories={str(c.get("id")) for c in (pc.get("categories") or []) if c.get("id")},
        category_names=[c.get("displayName") for c in (pc.get("categories") or [])
                        if c.get("displayName")],
        thumbnail=thumb, badge_set=badge_set,
    )


# ---------------------------------------------------------------- lane state
def lane_done(lane: Lane) -> bool:
    return lane.earned or lane.grants >= lane.repeat_times


def day_complete(lane: Lane, now: datetime) -> bool:
    """Heutiges Fenster offen und 20 Minuten voll -> heute nichts mehr zu holen."""
    if lane.window_end is None or now >= lane.window_end:
        return False
    return lane.minutes >= lane.required_minutes


def ready_at(lane: Lane, now: datetime):
    """Ab wann die Lane wieder Minuten annimmt. None = nie (fertig/gesperrt)."""
    if lane_done(lane) or not lane.preconditions_met:
        return None
    if lane.campaign_end is not None and now >= lane.campaign_end:
        return None
    candidates = []
    if day_complete(lane, now):
        candidates.append(lane.window_end)
    if lane.next_eligible is not None and lane.next_eligible > now:
        candidates.append(lane.next_eligible)
    return max(candidates) if candidates else now


# ---------------------------------------------------------------- planning
@dataclass
class Decision:
    action: str                     # watch | idle | done
    lane: "Lane | None" = None
    switch: bool = False            # Lane muss erst ausgewählt werden
    resume_at: "datetime | None" = None
    reason: str = ""


def _priority_rank(lane: Lane, priority: list) -> int:
    n = lane.name.lower()
    de = german_name(lane.name).lower()
    for i, p in enumerate(priority):
        p = (p or "").strip().lower()
        if p and (p == n or p == de or p in n or p in de):
            return i
    return len(priority)


def plan(lanes: list, selected_lane, now: datetime, priority: list,
         blocked: "dict | None" = None) -> Decision:
    """Nächste Aktion: weiter auf der gewählten Lane, umschalten, warten, fertig."""
    blocked = blocked or {}
    open_lanes = []
    waits = []
    for lane in lanes:
        r = ready_at(lane, now)
        if r is None:
            continue
        until = blocked.get(lane.lane)
        if until is not None and until > now:
            r = max(r, until)
        if r <= now:
            open_lanes.append(lane)
        else:
            waits.append(r)
    if not open_lanes and not waits:
        return Decision("done", reason="keine offene Lane mehr")
    if not open_lanes:
        return Decision("idle", resume_at=min(waits),
                        reason="alle Lanes für heute voll")
    # Angefangene (ausgewählte) Lane nicht wechseln, solange sie heute noch zählt.
    for lane in open_lanes:
        if selected_lane and lane.lane == selected_lane:
            return Decision("watch", lane=lane, switch=False,
                            reason="gewählte Lane braucht heute noch Minuten")
    # Sonst: Priorität, dann die mit den meisten Tagen, dann mit den meisten Minuten.
    best = sorted(open_lanes, key=lambda l: (_priority_rank(l, priority),
                                             -l.grants, -l.minutes, l.name))[0]
    return Decision("watch", lane=best, switch=(best.lane != selected_lane),
                    reason="nächste Lane nach Priorität/Fortschritt")


# ---------------------------------------------------------------- channel pick
def pick_channel(pool: list, live: dict, categories: set, current):
    """live: login -> (game_id, is_live). Bleibt beim aktuellen Kanal, solange
    er live und in einer zählenden Kategorie ist; sonst erster passender im Pool."""
    def ok(login):
        info = live.get(login)
        return bool(info and info[1] and str(info[0]) in categories)
    if current and ok(current):
        return current
    for login in pool:
        if ok(login):
            return login
    return None


# ---------------------------------------------------------------- payload
def minute_watched_payload(channel_id, broadcast_id, user_id, login,
                           game=None, game_id=None) -> str:
    props = {
        "channel_id": channel_id, "broadcast_id": broadcast_id, "player": "site",
        "user_id": user_id, "live": True, "channel": login,
    }
    if game is not None and game_id is not None:
        props["game"] = game
        props["game_id"] = game_id
    body = json.dumps([{"event": "minute-watched", "properties": props}],
                      separators=(",", ":"))
    return base64.b64encode(body.encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------- watch outcome
def watch_outcome(lane: Lane, start_minutes: int, started_at: datetime,
                  last_progress_at: datetime, now: datetime,
                  max_minutes: int, stall_minutes: int) -> str:
    """Bewertet eine laufende Watch-Sitzung auf einer Lane.

    day_done  -> Tagesziel erreicht (Fenster offen, Minuten voll)
    stalled   -> seit `stall_minutes` kein Minuten-Fortschritt (Twitch zählt nicht)
    capped    -> länger als `max_minutes` dran -> Notbremse
    continue  -> weiterschauen
    """
    if day_complete(lane, now):
        return "day_done"
    if (now - last_progress_at).total_seconds() >= stall_minutes * 60:
        return "stalled"
    if (now - started_at).total_seconds() >= max_minutes * 60:
        return "capped"
    return "continue"


def pick_from_directory(edges: list, categories: set):
    """Login des größten Live-Streams aus einer Verzeichnis-Antwort, dessen Kategorie zählt."""
    best, best_v = None, -1
    for e in edges or []:
        n = (e or {}).get("node") or {}
        game = n.get("game") or {}
        login = (n.get("broadcaster") or {}).get("login")
        if not login or str(game.get("id")) not in categories:
            continue
        v = int(n.get("viewersCount") or 0)
        if v > best_v:
            best, best_v = login, v
    return best
