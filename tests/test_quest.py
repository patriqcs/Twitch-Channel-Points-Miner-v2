# -*- coding: utf-8 -*-
"""Tests für backend/quest.py — Planung der Pokémon-Badge-Quests (Reward-Drops).

Reine Logik: Kampagnen-JSON parsen, Lane-Zustand bewerten, nächste Aktion planen,
Quest-Kanal wählen, Spade-Payload bauen. Kein Netz, keine DB.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend import quest

NOW = datetime(2026, 11, 4, 20, 0, 0, tzinfo=timezone.utc)
CATS = [{"id": "509658", "displayName": "Just Chatting"},
        {"id": "26936", "displayName": "Musik"}]


def _group(name, lane, minutes=0, grants=0, window=None, status="IN_PROGRESS",
           pre=True, earned=None, next_eligible=None, repeat=3, required=20):
    return {
        "id": f"grp-{name.lower()}", "name": name,
        "track": "pokemon#track-1", "lane": lane,
        "progressCriteria": {
            "requirementType": "WATCH",
            "requirements": {"minutesWatched": required, "subs": None},
            "isRepeatable": True, "isAutoProgressable": False,
            "repeatableConfig": {"repeatableTimes": repeat,
                                 "resetIntervalSeconds": 86400,
                                 "shouldCompleteAllRepeatsToEarn": True},
            "categories": CATS, "channels": None,
        },
        "self": {
            "status": status, "currentMinutesWatched": minutes, "currentSubs": 0,
            "grantCount": grants, "nextEligibleAt": next_eligible,
            "currentWindow": window, "hasPreconditionsMet": pre,
            "earnedReward": earned,
        },
        "rewards": [{"id": f"rew-{name.lower()}", "name": name,
                     "distributionType": "BADGE",
                     "dropDetail": {"badge": {"id": "x", "setID": name.lower()}},
                     "pool": None}],
    }


def _inventory(groups, campaign_id="camp-1", end="2027-01-30T22:59:59.999Z"):
    return {"currentUser": {"id": "73632915", "inventory": {
        "viewerRewardDropCampaignsInProgress": [{
            "id": campaign_id, "status": "ACTIVE", "name": "Pokemon Evolutions",
            "startAt": "2026-10-16T17:00:00Z", "endAt": end,
            "rewardGroups": groups,
        }]}}}


def _window(started: datetime):
    return {"startedAt": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiresAt": (started + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")}


# ---------------------------------------------------------------- parsing

def test_parse_campaigns_builds_one_lane_per_watch_group():
    inv = _inventory([_group("Charmeleon", "lane-char", minutes=8, grants=2,
                             window=_window(NOW - timedelta(minutes=36))),
                      _group("Pikachu", "lane-pichu")])
    lanes = quest.parse_campaigns(inv)
    assert [l.name for l in lanes] == ["Charmeleon", "Pikachu"]
    c = lanes[0]
    assert c.lane == "lane-char" and c.track == "pokemon#track-1"
    assert c.campaign_id == "camp-1"
    assert c.minutes == 8 and c.grants == 2 and c.repeat_times == 3
    assert c.required_minutes == 20 and c.reset_seconds == 86400
    assert c.window_end == NOW - timedelta(minutes=36) + timedelta(hours=24)
    assert c.categories == {"509658", "26936"}
    assert c.campaign_end.year == 2027


def test_parse_campaigns_skips_non_watch_and_sub_groups():
    g = _group("Gastly", "lane-gastly")
    g["progressCriteria"]["requirementType"] = "SUBSCRIBE"
    g["progressCriteria"]["requirements"] = {"minutesWatched": None, "subs": 1}
    lanes = quest.parse_campaigns(_inventory([g, _group("Pikachu", "lane-pichu")]))
    assert [l.name for l in lanes] == ["Pikachu"]


def test_parse_campaigns_skips_groups_without_progression_track():
    """Fremde Watch-Kampagnen (z. B. „WoW Grinder") haben weder track noch lane
    und dürfen nicht als Lane auftauchen — sonst versucht der Planer, Twitch auf
    eine leere Lane umzuschalten (INVALID_SELECTION)."""
    foreign = _group("WoW Grinder", "", required=480)
    foreign["track"] = None
    foreign["lane"] = None
    foreign["name"] = None
    lanes = quest.parse_campaigns(_inventory([foreign, _group("Pikachu", "lane-p")]), now=NOW)
    assert [l.name for l in lanes] == ["Pikachu"]


def test_parse_campaigns_tolerates_missing_self():
    g = _group("Charizard", "lane-char")
    g["self"] = None
    lanes = quest.parse_campaigns(_inventory([g]))
    assert lanes[0].minutes == 0 and lanes[0].grants == 0
    assert lanes[0].preconditions_met is False  # unbekannt -> nicht spielbar


# ---------------------------------------------------------------- lane state

def test_lane_done_when_all_grants_collected_or_reward_earned():
    done = quest.parse_campaigns(_inventory([_group("A", "l", grants=3)]))[0]
    earned = quest.parse_campaigns(_inventory([_group("B", "m", grants=1,
                                                      earned={"id": "r", "name": "B"})]))[0]
    open_ = quest.parse_campaigns(_inventory([_group("C", "n", grants=2)]))[0]
    assert quest.lane_done(done) and quest.lane_done(earned)
    assert not quest.lane_done(open_)


def test_day_complete_when_window_open_and_minutes_reached():
    lane = quest.parse_campaigns(_inventory([
        _group("A", "l", minutes=20, grants=1, window=_window(NOW - timedelta(hours=1)))]))[0]
    assert quest.day_complete(lane, NOW) is True
    # abgelaufenes Fenster -> neuer Tag möglich
    assert quest.day_complete(lane, NOW + timedelta(hours=24)) is False


def test_ready_at_is_now_without_window_and_window_end_when_day_done():
    fresh = quest.parse_campaigns(_inventory([_group("A", "l")]))[0]
    assert quest.ready_at(fresh, NOW) == NOW
    started = NOW - timedelta(hours=2)
    done_today = quest.parse_campaigns(_inventory([
        _group("B", "m", minutes=20, grants=1, window=_window(started))]))[0]
    assert quest.ready_at(done_today, NOW) == started + timedelta(hours=24)
    # nextEligibleAt gewinnt, wenn später als das Fensterende
    later = (NOW + timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gated = quest.parse_campaigns(_inventory([
        _group("C", "n", minutes=20, grants=1, window=_window(started),
               next_eligible=later)]))[0]
    assert quest.ready_at(gated, NOW) == NOW + timedelta(hours=30)


def test_ready_at_none_when_lane_done_or_preconditions_missing():
    done = quest.parse_campaigns(_inventory([_group("A", "l", grants=3)]))[0]
    locked = quest.parse_campaigns(_inventory([_group("B", "m", pre=False)]))[0]
    assert quest.ready_at(done, NOW) is None
    assert quest.ready_at(locked, NOW) is None


# ---------------------------------------------------------------- planning

def _lanes(*groups):
    return quest.parse_campaigns(_inventory(list(groups)))


def test_plan_keeps_selected_lane_while_it_still_needs_minutes_today():
    lanes = _lanes(_group("Charmeleon", "lane-char", minutes=8, grants=2,
                          window=_window(NOW - timedelta(minutes=30))),
                   _group("Pikachu", "lane-pichu"))
    d = quest.plan(lanes, selected_lane="lane-char", now=NOW, priority=["Pikachu"])
    assert d.action == "watch"
    assert d.lane.lane == "lane-char" and d.switch is False


def test_plan_switches_to_next_lane_when_selected_lane_done_for_today():
    lanes = _lanes(_group("Charmeleon", "lane-char", minutes=20, grants=2,
                          window=_window(NOW - timedelta(minutes=30))),
                   _group("Pikachu", "lane-pichu"),
                   _group("Wartortle", "lane-squirt"))
    d = quest.plan(lanes, selected_lane="lane-char", now=NOW,
                   priority=["Wartortle", "Pikachu"])
    assert d.action == "watch" and d.switch is True
    assert d.lane.lane == "lane-squirt"  # Priorität entscheidet


def test_plan_prefers_lane_closest_to_completion_when_no_priority_matches():
    lanes = _lanes(_group("Pikachu", "lane-pichu", grants=1),
                   _group("Wartortle", "lane-squirt", grants=2),
                   _group("Ivysaur", "lane-bulb", grants=0))
    d = quest.plan(lanes, selected_lane=None, now=NOW, priority=[])
    assert d.lane.lane == "lane-squirt"


def test_plan_idles_until_earliest_window_when_every_lane_done_for_today():
    s1 = NOW - timedelta(hours=3)
    s2 = NOW - timedelta(hours=1)
    lanes = _lanes(_group("A", "la", minutes=20, grants=1, window=_window(s1)),
                   _group("B", "lb", minutes=20, grants=1, window=_window(s2)))
    d = quest.plan(lanes, selected_lane="la", now=NOW, priority=[])
    assert d.action == "idle" and d.lane is None
    assert d.resume_at == s1 + timedelta(hours=24)


def test_plan_reports_done_when_nothing_is_left():
    lanes = _lanes(_group("A", "la", grants=3), _group("B", "lb", grants=3))
    d = quest.plan(lanes, selected_lane="la", now=NOW, priority=[])
    assert d.action == "done" and d.resume_at is None


def test_plan_skips_blocked_lanes_and_resumes_when_block_expires():
    lanes = _lanes(_group("A", "la"), _group("B", "lb"))
    until = NOW + timedelta(minutes=50)
    d = quest.plan(lanes, selected_lane="la", now=NOW, priority=["A"],
                   blocked={"la": until, "lb": until})
    assert d.action == "idle" and d.resume_at == until
    d2 = quest.plan(lanes, selected_lane="la", now=NOW, priority=["A"],
                    blocked={"la": until})
    assert d2.action == "watch" and d2.lane.lane == "lb" and d2.switch is True


def test_plan_ignores_expired_campaigns():
    lanes = quest.parse_campaigns(_inventory([_group("A", "la")],
                                             end="2026-11-01T00:00:00Z"))
    d = quest.plan(lanes, selected_lane=None, now=NOW, priority=[])
    assert d.action == "done"


# ---------------------------------------------------------------- channel pick

def test_pick_channel_prefers_current_channel_while_still_qualifying():
    live = {"a": ("509658", True), "b": ("26936", True), "c": ("32399", True)}
    assert quest.pick_channel(["a", "b", "c"], live, {"509658", "26936"},
                              current="b") == "b"
    # aktueller Kanal fällt aus der Kategorie -> nächster passender nach Pool-Reihenfolge
    live["b"] = ("32399", True)
    assert quest.pick_channel(["a", "b", "c"], live, {"509658", "26936"},
                              current="b") == "a"


def test_pick_channel_returns_none_when_nothing_qualifies():
    live = {"a": ("32399", True), "b": ("509658", False)}
    assert quest.pick_channel(["a", "b"], live, {"509658"}, current=None) is None


# ---------------------------------------------------------------- spade payload

def test_minute_watched_payload_matches_miner_format():
    raw = quest.minute_watched_payload(
        channel_id="78874179", broadcast_id="3192", user_id="73632915",
        login="krokoboss", game="Just Chatting", game_id="509658")
    decoded = json.loads(base64.b64decode(raw))
    assert decoded == [{"event": "minute-watched", "properties": {
        "channel_id": "78874179", "broadcast_id": "3192", "player": "site",
        "user_id": "73632915", "live": True, "channel": "krokoboss",
        "game": "Just Chatting", "game_id": "509658"}}]


# ---------------------------------------------------------------- config

def test_parse_priority_normalises_names():
    assert quest.parse_priority("Glurak, charizard\n Pikachu ,,") == [
        "glurak", "charizard", "pikachu"]


@pytest.mark.parametrize("name,expected", [
    ("Charmeleon", "Glutexo"), ("Charizard", "Glurak"), ("Ivysaur", "Bisaknosp"),
    ("Venusaur", "Bisaflor"), ("Wartortle", "Schillok"), ("Blastoise", "Turtok"),
    ("Pikachu", "Pikachu"), ("Haunter", "Alpollo"), ("Gengar", "Gengar"),
    ("Unknown Mon", "Unknown Mon"),
])
def test_german_name(name, expected):
    assert quest.german_name(name) == expected


# ---------------------------------------------------------------- watch outcome (Manager-Helfer)

def test_watch_outcome_continues_while_progress_arrives():
    lane = _lanes(_group("A", "la", minutes=12, grants=0, window=_window(NOW - timedelta(minutes=13))))[0]
    out = quest.watch_outcome(lane, start_minutes=0, started_at=NOW - timedelta(minutes=13),
                              last_progress_at=NOW - timedelta(minutes=1), now=NOW,
                              max_minutes=35, stall_minutes=8)
    assert out == "continue"


def test_watch_outcome_day_done_when_required_minutes_reached():
    lane = _lanes(_group("A", "la", minutes=20, grants=1, window=_window(NOW - timedelta(minutes=21))))[0]
    out = quest.watch_outcome(lane, start_minutes=0, started_at=NOW - timedelta(minutes=21),
                              last_progress_at=NOW, now=NOW, max_minutes=35, stall_minutes=8)
    assert out == "day_done"


def test_watch_outcome_stalled_when_no_progress_for_stall_window():
    lane = _lanes(_group("A", "la", minutes=5, grants=0, window=_window(NOW - timedelta(minutes=20))))[0]
    out = quest.watch_outcome(lane, start_minutes=5, started_at=NOW - timedelta(minutes=20),
                              last_progress_at=NOW - timedelta(minutes=9), now=NOW,
                              max_minutes=35, stall_minutes=8)
    assert out == "stalled"


def test_watch_outcome_capped_after_max_minutes():
    lane = _lanes(_group("A", "la", minutes=15, grants=0, window=_window(NOW - timedelta(minutes=40))))[0]
    out = quest.watch_outcome(lane, start_minutes=0, started_at=NOW - timedelta(minutes=40),
                              last_progress_at=NOW - timedelta(minutes=2), now=NOW,
                              max_minutes=35, stall_minutes=8)
    assert out == "capped"


# ---------------------------------------------------------------- expired campaigns / directory fallback

def test_parse_campaigns_drops_expired_campaigns():
    inv = _inventory([_group("HoB", "lane-hob")], campaign_id="old", end="2026-10-31T22:30:00Z")
    inv["currentUser"]["inventory"]["viewerRewardDropCampaignsInProgress"][0]["status"] = "EXPIRED"
    assert quest.parse_campaigns(inv, now=NOW) == []


def test_pick_from_directory_takes_biggest_stream_in_category():
    edges = [
        {"node": {"viewersCount": 900, "language": "DE", "broadcaster": {"id": "1", "login": "small"},
                  "game": {"id": "509658", "displayName": "Just Chatting"}}},
        {"node": {"viewersCount": 5000, "language": "DE", "broadcaster": {"id": "2", "login": "big"},
                  "game": {"id": "509658", "displayName": "Just Chatting"}}},
        {"node": {"viewersCount": 9000, "language": "DE", "broadcaster": {"id": "3", "login": "wrongcat"},
                  "game": {"id": "32399", "displayName": "Counter-Strike"}}},
    ]
    assert quest.pick_from_directory(edges, {"509658"}) == "big"
    assert quest.pick_from_directory([], {"509658"}) is None


# ---------------------------------------------------------------- player session (Edge-Tracking)

MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:NODE="dacaf1fd2c05.j.cloudfront.hls.ttvnw.net",MANIFEST-NODE-TYPE="weaver_cluster",MANIFEST-NODE="elastic-weaver.euc12",SUPPRESS="true",SERVER-TIME="1793825615.05",TRANSCODESTACK="transmux",TRANSCODEMODE="cbr_v1",USER-IP="1.2.3.4",SERVING-ID="2e36c4996c3f4bac94991abccb7cbf79",CLUSTER="cloudfront_prod_euc12_twitch_ispc",ABS="true",VIDEO-SESSION-ID="1903256118941097813",BROADCAST-ID="319264074342",STREAM-TIME="15756.0",FUTURE="true",B="false",USER-COUNTRY="DE",MANIFEST-CLUSTER="euc12",ORIGIN="euc12",C="aHR0cHM6Ly8zYjkwMzgucnVmaW8uaGxzLmxpdmUtdmlkZW8ubmV0L3YxL3NlZ21lbnQvQUJD"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
https://edge.example/chunked.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="160p30",NAME="160p",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=230000,RESOLUTION=284x160,CODECS="avc1.4D400C,mp4a.40.2",VIDEO="160p30",FRAME-RATE=30.000
https://edge.example/160p30.m3u8
"""


def test_parse_master_manifest_extracts_tracking_url_session_and_lowest_variant():
    m = quest.parse_master_manifest(MASTER)
    assert m["track_url"] == "https://3b9038.rufio.hls.live-video.net/v1/segment/ABC"
    assert m["serving_id"] == "2e36c4996c3f4bac94991abccb7cbf79"
    assert m["video_session_id"] == "1903256118941097813"
    assert m["broadcast_id"] == "319264074342"
    assert m["node"] == "dacaf1fd2c05.j.cloudfront.hls.ttvnw.net"
    assert m["lowest_url"] == "https://edge.example/160p30.m3u8"
    assert m["lowest_format"] == "160p30"


def test_parse_master_manifest_without_tracking_url_returns_none_track():
    text = MASTER.replace(',C="aHR0cHM6Ly8zYjkwMzgucnVmaW8uaGxzLmxpdmUtdmlkZW8ubmV0L3YxL3NlZ21lbnQvQUJD"', "")
    m = quest.parse_master_manifest(text)
    assert m["track_url"] is None and m["lowest_url"].endswith("160p30.m3u8")


def test_player_event_payload_carries_identity_session_and_minutes():
    m = quest.parse_master_manifest(MASTER)
    raw = quest.player_event_payload(
        "minute-watched", manifest=m, login="krokoboss", channel_id="78874179",
        user_id="73632915", user_login="patriqcs", device_id="dev-1", user_agent="UA/1",
        play_session_id="psid", game="Just Chatting", game_id="509658",
        minutes_logged=3, position=180.0)
    body = json.loads(base64.b64decode(raw))
    p = body["properties"]
    assert body["event"] == "minute-watched"
    assert p["minutes_logged"] == 3 and p["user_id"] == "73632915" and p["login"] == "patriqcs"
    assert p["serving_id"] == m["serving_id"] and p["video_session_id"] == m["video_session_id"]
    assert p["play_session_id"] == "psid" and p["device_id"] == "dev-1"
    assert p["platform"] == "web" and p["player"] == "site" and p["live"] is True
    assert p["channel"] == "krokoboss" and p["channel_id"] == "78874179"
    assert p["broadcast_id"] == "319264074342"
    assert p["game_id"] == "509658" and p["stream_format"] == "160p30"


def test_new_segments_returns_unseen_segment_urls_in_order():
    playlist = "#EXTM3U\n#EXTINF:2.0,\nhttps://e/s1.ts\n#EXTINF:2.0,\nhttps://e/s2.ts\n#EXTINF:2.0,\nhttps://e/s3.ts\n"
    assert quest.new_segments(playlist, seen={"https://e/s1.ts"}, limit=2) == ["https://e/s2.ts", "https://e/s3.ts"]
    assert quest.new_segments(playlist, seen={"https://e/s1.ts", "https://e/s2.ts", "https://e/s3.ts"}, limit=2) == []


def test_pick_channel_skips_excluded_channels():
    live = {"a": ("509658", True), "b": ("509658", True)}
    assert quest.pick_channel(["a", "b"], live, {"509658"}, current="a", exclude={"a"}) == "b"
    assert quest.pick_from_directory([
        {"node": {"viewersCount": 5, "language": "DE", "broadcaster": {"id": "1", "login": "a"},
                  "game": {"id": "509658", "displayName": "Just Chatting"}}}], {"509658"}, exclude={"a"}) is None


def test_plan_continues_on_same_lane_with_next_stage_after_first_stage_done():
    # Glutexo (fertig) und Glurak (offen) teilen sich dieselbe Lane-ID
    lanes = _lanes(_group("Charmeleon", "lane-char", minutes=20, grants=3,
                          window=_window(NOW - timedelta(minutes=5))),
                   _group("Charizard", "lane-char", minutes=0, grants=0),
                   _group("Pikachu", "lane-pichu"))
    d = quest.plan(lanes, selected_lane="lane-char", now=NOW, priority=[])
    assert d.action == "watch" and d.lane.name == "Charizard" and d.switch is False
