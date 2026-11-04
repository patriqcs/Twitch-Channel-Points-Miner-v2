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
