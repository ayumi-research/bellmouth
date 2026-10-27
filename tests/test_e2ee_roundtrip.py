"""Round-trip: outbound key payloads parse through our own receive path.

The sender (e2ee_send) must produce payloads the receiver (e2ee_keys)
accepts — that's the interop contract with ourselves and, by construction,
with Element X (we mirror the shapes we decode live).
"""
import base64

from jack import e2ee_keys, e2ee_send

ROOM = "!testroom:hs.example"
ME = "@agent:hs.example"
MY_DEV = "JACK01"


def test_element_shape_roundtrip():
    ok = e2ee_send.OutboundKeys(None, ME, MY_DEV)
    ok.generate()
    payloads = dict(ok._payloads(ROOM))
    content = payloads[e2ee_send.ELEMENT_KEY_TYPE]

    # receiver-side extraction (the live-proven path)
    mks = e2ee_keys.extract_keys_from_content(content, ME)
    assert len(mks) == 1
    raw = base64.b64decode(mks[0]["key"] + "=" * (-len(mks[0]["key"]) % 4))
    assert raw == ok.key
    assert mks[0]["index"] == 0
    # receiver identity derivation must land on our canonical livekit identity
    assert content["member"]["id"] == f"{ME}:{MY_DEV}"
    assert content["member"]["claimed_device_id"] == MY_DEV


def test_standardized_shape_roundtrip():
    ok = e2ee_send.OutboundKeys(None, ME, MY_DEV)
    ok.generate()
    payloads = dict(ok._payloads(ROOM))
    content = payloads[e2ee_send.STANDARD_KEY_TYPE]

    mks = e2ee_keys.extract_keys_from_content(content, ME)
    assert len(mks) == 1
    raw = base64.b64decode(mks[0]["key"] + "=" * (-len(mks[0]["key"]) % 4))
    assert raw == ok.key
    assert content["member_id"] == f"{ME}:{MY_DEV}"


def test_keys_are_random_per_call():
    a = e2ee_send.OutboundKeys(None, ME, MY_DEV); a.generate()
    b = e2ee_send.OutboundKeys(None, ME, MY_DEV); b.generate()
    assert a.key != b.key and len(a.key) == 16


def test_room_options_gcm_toggle():
    from livekit import rtc
    none_opts = e2ee_keys.e2ee_room_options(encrypt_outbound=False)
    gcm_opts = e2ee_keys.e2ee_room_options(encrypt_outbound=True)
    opts = getattr(gcm_opts, "encryption", None) or getattr(gcm_opts, "e2ee", None)
    opts_n = getattr(none_opts, "encryption", None) or getattr(none_opts, "e2ee", None)
    assert opts.encryption_type == rtc.EncryptionType.GCM
    assert opts_n.encryption_type == rtc.EncryptionType.NONE
