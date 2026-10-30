"""Regression tests for the pre-release review findings."""
import json
from types import SimpleNamespace

from jack import e2ee_keys


class FakeMachine:
    """Minimal olm-machine stand-in: records delegated events."""

    def __init__(self):
        self.delegated = []
        self.device_store = {}
        self.users_for_key_query = set()

    def _handle_olm_event(self, sender, sender_key, payload):
        self.delegated.append((sender, sender_key, payload))


class FakeDevice:
    def __init__(self, curve):
        self.curve25519 = curve


def _client_with(machine, query=None):
    client = SimpleNamespace(olm=machine)
    if query is not None:
        client.keys_query = query
    return client


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
    machine.device_store = {"@bob:hs.example": {"DEV1": FakeDevice("curveB")}}
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
    # known-device mismatch is checked against the store -> dropped
    machine.device_store = {"@bob:hs.example": {"DEV9": FakeDevice("OTHERCURVE")}}
    machine._handle_olm_event("@bob:hs.example", "curveB", key_event("!callroom:hs.example"))
    assert not recv.keys  # mismatch -> dropped


def _key_event(claimed="DEV1", room="!callroom:hs.example", key="AQEBAQEBAQEBAQEBAQEBAQ"):
    return {"type": "io.element.call.encryption_keys", "content": {
        "room_id": room,
        "media_key": {"index": 0, "key": key},
        "member": {"claimed_device_id": claimed, "id": f"@bob:hs.example:{claimed}"},
    }}


def test_unknown_device_fail_closed_query_then_install():
    """Store miss: no trust, keys_query scheduled, installed once verified (astra #1)."""
    import asyncio

    machine = FakeMachine()
    machine.device_store = {}  # DEV1 unknown

    async def fake_query():
        machine.device_store["@bob:hs.example"] = {"DEV1": FakeDevice("curveB")}

    client = _client_with(machine, fake_query)
    recv = e2ee_keys.MediaKeyReceiver("!callroom:hs.example")
    holder = SimpleNamespace(receiver=recv)
    e2ee_keys.make_olm_hook(client, holder)

    async def main():
        machine._handle_olm_event("@bob:hs.example", "curveB", _key_event())
        # fail-closed: NOT installed while the device is unknown
        assert not recv.keys
        # query was requested for the sender
        assert "@bob:hs.example" in machine.users_for_key_query
        # delegation still happened even though the event is stashed
        assert machine.delegated
        # let the scheduled keys_query + retry run
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert tasks, "keys_query task was not scheduled"
        await asyncio.gather(*tasks)
        # now verified against the (freshly filled) store and installed
        assert recv.keys[("@bob:hs.example:DEV1", 0)] == b"\x01" * 16

    asyncio.run(main())


def test_unknown_device_still_unknown_after_query_dropped():
    """Store miss that survives the keys_query is dropped, not trusted."""
    import asyncio

    machine = FakeMachine()
    machine.device_store = {}

    async def empty_query():
        return None  # store stays empty

    client = _client_with(machine, empty_query)
    recv = e2ee_keys.MediaKeyReceiver("!callroom:hs.example")
    holder = SimpleNamespace(receiver=recv)
    e2ee_keys.make_olm_hook(client, holder)

    async def main():
        machine._handle_olm_event("@bob:hs.example", "curveB", _key_event())
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*tasks)
        assert not recv.keys  # fail-closed: dropped after the retry

    asyncio.run(main())


def test_two_events_while_query_inflight_both_retried():
    """A second miss during an inflight query is covered by the same retry."""
    import asyncio

    machine = FakeMachine()
    machine.device_store = {}
    queries = []

    async def fake_query():
        queries.append(1)
        machine.device_store["@bob:hs.example"] = {"DEV1": FakeDevice("curveB")}

    client = _client_with(machine, fake_query)
    recv = e2ee_keys.MediaKeyReceiver("!callroom:hs.example")
    holder = SimpleNamespace(receiver=recv)
    e2ee_keys.make_olm_hook(client, holder)

    async def main():
        machine._handle_olm_event("@bob:hs.example", "curveB", _key_event())
        machine._handle_olm_event("@bob:hs.example", "curveB", _key_event(key="AQIDAgMEBQYHCAkKCwwNDg"))
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*tasks)
        assert len(queries) == 1, "one query must cover both stashed events"
        assert len(recv.keys) == 1
        import base64 as _b64
        assert recv.keys[("@bob:hs.example:DEV1", 0)] == _b64.b64decode("AQIDAgMEBQYHCAkKCwwNDg==")

    asyncio.run(main())


def test_seam_scheduler_used_when_provided():
    """The query_scheduler seam is called synchronously with the sender."""
    machine = FakeMachine()
    machine.device_store = {}
    recv = e2ee_keys.MediaKeyReceiver("!callroom:hs.example")
    holder = SimpleNamespace(receiver=recv)
    seen = []
    e2ee_keys.make_olm_hook(_client_with(machine), holder,
                            query_scheduler=seen.append)
    machine._handle_olm_event("@bob:hs.example", "curveB", _key_event())
    assert seen == ["@bob:hs.example"]
    assert not recv.keys  # still pending — the seam owns the retry


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
