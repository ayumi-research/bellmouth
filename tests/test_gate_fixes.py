"""Regression tests for the pre-release review findings."""
import json
from types import SimpleNamespace

from jack import e2ee_keys


class FakeMachine:
    """Minimal olm-machine stand-in: records delegated events."""

    def __init__(self):
        self.delegated = []
        self.device_store = {}

    def _handle_olm_event(self, sender, sender_key, payload):
        self.delegated.append((sender, sender_key, payload))


class FakeDevice:
    def __init__(self, curve):
        self.curve25519 = curve


def _client_with(machine):
    return SimpleNamespace(olm=machine)


def test_hook_delegates_non_key_events():
    """m.room_key and friends must still reach nio's handler (astra HIGH #2)."""
    machine = FakeMachine()
    holder = SimpleNamespace(receiver=None)
    e2ee_keys.make_olm_hook(_client_with(machine), holder)

    # hook replaced the method; call it the way nio would
    payload = {"type": "m.room_key", "content": {"room_id": "!r:hs"}}
    machine._handle_olm_event("@alice:hs.example", "curveA", payload)
    assert len(machine.delegated) == 1
    assert machine.delegated[0][2]["type"] == "m.room_key"


def test_hook_processes_key_events_and_delegates():
    machine = FakeMachine()
    recv = e2ee_keys.MediaKeyReceiver("!callroom:hs.example")
    holder = SimpleNamespace(receiver=recv)
    e2ee_keys.make_olm_hook(_client_with(machine), holder)

    key = "AQEBAQEBAQEBAQEBAQEBAQ"  # b64 of 16x 0x01
    payload = {"type": "io.element.call.encryption_keys", "content": {
        "room_id": "!callroom:hs.example",
        "member_id": "@bob:hs.example:DEV1",
        "media_key": {"index": 0, "key": key},
        "keys": {"index": 0, "key": key},
        "member": {"claimed_device_id": "DEV1", "id": "@bob:hs.example:DEV1"},
    }}
    machine._handle_olm_event("@bob:hs.example", "curveB", payload)
    # installed for the canonical identity
    assert recv.keys[("@bob:hs.example:DEV1", 0)] == b"\x01" * 16
    # AND delegated
    assert machine.delegated and machine.delegated[0][2]["type"] == "io.element.call.encryption_keys"


def test_hook_rejects_wrong_room_and_bad_sender_key():
    machine = FakeMachine()
    machine.device_store = {}
    recv = e2ee_keys.MediaKeyReceiver("!callroom:hs.example")
    holder = SimpleNamespace(receiver=recv)
    e2ee_keys.make_olm_hook(_client_with(machine), holder)

    def key_event(room):
        return {"type": "io.element.call.encryption_keys", "content": {
            "room_id": room,
            "media_key": {"index": 0, "key": "A" * 22},
            "member": {"claimed_device_id": "DEV9", "id": f"@bob:hs.example:DEV9"},
        }}

    # other room -> ignored entirely
    machine._handle_olm_event("@bob:hs.example", "curveB", key_event("!other:hs.example"))
    assert not recv.keys
    # known-device mismatch is checked against the store; unknown device fails
    # open (olm envelope already authenticates sender_key) -> still installs
    machine.device_store = {"@bob:hs.example": {"DEV9": FakeDevice("OTHERCURVE")}}
    machine._handle_olm_event("@bob:hs.example", "curveB", key_event("!callroom:hs.example"))
    assert not recv.keys  # mismatch -> dropped


def test_allowlist_default_deny():
    from jack.watcher import CallWatcher
    w = object.__new__(CallWatcher)
    w.cfg = {"allowlist_callers": [], "allowlist_rooms": []}
    assert w._allowed("!r:hs", "@anyone:hs.example") is False
    w.cfg["allowlist_callers"] = ["@friend:hs.example"]
    assert w._allowed("!r:hs", "@friend:hs.example") is True
    assert w._allowed("!r:hs", "@stranger:hs.example") is False


def test_recording_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("JACK_RECORD_UTTERANCES", raising=False)
    assert CallWatcherDump() is None


def CallWatcherDump():
    from jack.media import CallMedia
    return CallMedia._dump_utterance(b"\x00\x00" * 100, 48000)
