import json, os
import pytest

from jack.events import livekit_room_alias, parse_call_member_event, select_focus

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(n):
    with open(os.path.join(FIX, f"call_member_{n}.json")) as f:
        return json.load(f)


def test_real_join_event_parses():
    st = parse_call_member_event(load(0))
    assert st is not None and st.joined
    assert st.sender == "@zoltan:matrix.ayumi.town"
    assert st.device_id == "TRNBXOZARK"
    assert st.application == "m.call"
    assert st.intent == "audio"
    assert st.scope == "m.room"
    assert st.call_id == ""
    assert st.livekit_room == "!BSUomZfJHOyuaMzXoo:matrix.ayumi.town"
    assert st.livekit_service_url == "https://matrix.ayumi.town/livekit-jwt"


def test_real_leave_event_parses():
    st = parse_call_member_event(load(2))
    assert st is not None and not st.joined
    assert st.sender == "@zoltan:matrix.ayumi.town"
    assert select_focus(st) is None


def test_focus_selection_with_matching_service_url():
    st = parse_call_member_event(load(1))
    room = select_focus(st, "https://matrix.ayumi.town/livekit-jwt")
    assert room == "!BSUomZfJHOyuaMzXoo:matrix.ayumi.town"


def test_focus_selection_rejects_foreign_sfu():
    st = parse_call_member_event(load(1))
    assert select_focus(st, "https://other.example.com/livekit-jwt") is None


def test_non_call_event_returns_none():
    assert parse_call_member_event({"type": "m.room.message", "content": {}}) is None


def test_malformed_state_key_returns_none():
    ev = load(0); ev["state_key"] = "weird"
    assert parse_call_member_event(ev) is None


def test_intent_switch_is_a_join():
    st = parse_call_member_event(load(1))
    assert st.joined and st.intent == "video"


def test_livekit_room_alias_golden_vector():
    # captured from lk-jwt logs during a real Element X call, 2026-10-25
    assert livekit_room_alias("!ZSqYfNfG4n8G2S8iK2iESKVfNqbkn6dSrq9kNCTC1Ms") == \
        "D3LQlpbUxvGzbymMNLcZyTcagSEz4HO5K0amNyZUz0Y"
