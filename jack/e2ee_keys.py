"""MatrixRTC media-key transport: lets the jack decrypt Element X's SFrame audio.

Root cause this fixes: encrypted rooms make Element X publish the caller's mic with
SFrame E2EE; keys travel in `io.element.call.encryption_keys` / `m.rtc.encryption_key`
events (olm to-device AND/OR megolm room events). We install them into the livekit
E2EE KeyProvider per participant identity.

Device identity: @mei_voice with device_id JACK01, olm store in /opt/jack/crypto_store.
First run uploads device keys; Element X then olm-encrypts key events to us.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

log = logging.getLogger("jack.e2ee")

KEY_TYPES = {"io.element.call.encryption_keys", "m.rtc.encryption_key"}
STORE_PATH = os.environ.get("JACK_CRYPTO_STORE", "/opt/jack/crypto_store")
DEVICE_ID = os.environ.get("JACK_DEVICE_ID", "JACK01")


def e2ee_room_options(encrypt_outbound: bool = False):
    """RoomOptions with an HKDF key provider.

    encrypt_outbound=True: publish frame-encrypted (GCM) — the OutboundKeys
    module must install our key under our identity BEFORE the first publish.
    False (legacy): publish plaintext, decrypt only.
    """
    from livekit import rtc
    provider = rtc.KeyProviderOptions(
        shared_key=None,
        ratchet_salt=b"LKFrameEncryptionKey",
        ratchet_window_size=10,
        key_ring_size=256,
        failure_tolerance=-1,
        key_derivation_function=rtc.KeyDerivationFunction.HKDF,
    )
    etype = rtc.EncryptionType.GCM if encrypt_outbound else rtc.EncryptionType.NONE
    opts = rtc.E2EEOptions(key_provider_options=provider, encryption_type=etype)
    import inspect
    fields = {f.name for f in getattr(rtc.RoomOptions, "__dataclass_fields__", {}).values()} \
        if hasattr(rtc.RoomOptions, "__dataclass_fields__") else []
    kwargs = {"auto_subscribe": True}
    if "encryption" in fields:
        kwargs["encryption"] = opts
    if "e2ee" in fields:
        kwargs["e2ee"] = opts
    return rtc.RoomOptions(**kwargs)


def key_provider_of(room):
    for attr in ("e2ee_manager",):
        mgr = getattr(room, attr, None)
        if mgr is not None:
            kp = getattr(mgr, "key_provider", None)
            if kp is not None:
                return kp
    return None


class MediaKeyReceiver:
    """Collects (livekit_identity, index) -> 16-byte key; installs into the room."""

    def __init__(self, matrix_room_id: str):
        self.matrix_room_id = matrix_room_id
        self.keys: dict[tuple[str, int], bytes] = {}   # (identity, index) -> raw
        self.room = None                                # set when the call starts

    def bind_room(self, room):
        self.room = room
        self.flush()

    def install(self, livekit_identity: str, media_key: dict) -> bool:
        try:
            index = media_key["index"]
            raw = base64.b64decode(media_key["key"] + "=" * (-len(media_key["key"]) % 4))
            if not (0 <= index < 256 and len(raw) in (16, 32)):
                log.warning("odd media key index/len: %s %d", index, len(raw))
                return False
            self.keys[(livekit_identity, index)] = raw
            log.info("media key installed: %s idx=%d len=%d", livekit_identity[:30], index, len(raw))
            self._push(livekit_identity, index, raw)
            return True
        except Exception:
            log.exception("media key install failed")
            return False

    def _push(self, identity: str, index: int, raw: bytes) -> None:
        kp = key_provider_of(self.room) if self.room else None
        if kp is None:
            return
        try:
            kp.set_key(identity, raw, index)
            log.info("key_provider.set_key OK for %s idx=%d", identity[:30], index)
        except Exception:
            log.exception("set_key failed")

    def flush(self) -> None:
        for (identity, index), raw in list(self.keys.items()):
            self._push(identity, index, raw)


def extract_keys_from_content(content: dict, sender: str) -> list[dict]:
    """Pull media_key dicts from a decrypted key event; tolerate shape variants."""
    out = []
    v0 = content.get("keys")
    if isinstance(v0, dict) and "key" in v0:
        out.append(v0)
    for k in ("keys", "media_keys"):
        v = content.get(k)
        if isinstance(v, dict) and "key" in v:
            continue  # already captured above
        if isinstance(v, list):
            for x in v:
                if isinstance(x, dict) and "key" in x:
                    out.append(x)
                elif isinstance(x, str):
                    out.append({"key": x, "index": 0})
    if isinstance(content.get("media_key"), dict):
        out.append(content["media_key"])
    if isinstance(content.get("key"), str) and "index" in content:
        out.append({"key": content["key"], "index": content["index"]})
    return out


def make_olm_hook(client, receiver: MediaKeyReceiver):
    """Intercept decrypted olm to-device events (nio-private hook, version-sensitive).

    user_device_identities: {(user_id, device_id): livekit_identity}
    Returns the installed callable.
    """
    from types import MethodType

    def dispatch(self, sender, sender_key, payload):
        try:
            if isinstance(payload, str):
                payload = json.loads(payload)
            if isinstance(payload, dict):
                log.info("olm payload seen: type=%s keys=%s", payload.get("type"),
                         sorted((payload.get("content") or {}).keys())[:6])
            if not isinstance(payload, dict) or payload.get("type") not in KEY_TYPES:
                pass
            else:
                content = payload.get("content") or payload
                member = content.get("member")
                if isinstance(member, dict) and member.get("id", "").count(":") >= 2:
                    ident = member["id"]  # canonical livekit identity
                elif isinstance(member, str) and member.startswith("@") and member.count(":") >= 2:
                    ident = member  # already the livekit identity @user:domain:device
                elif isinstance(member, dict):
                    ident = member.get("claimed_device_id", "") or f"{sender}:?"
                else:
                    dev = content.get("device_id", "") or ""
                    ident = f"{sender}:{dev}"
                if isinstance(ident, str) and not ident.startswith("@"):
                    ident = f"{sender.split(':')[0]}:{ident}"  # device-only -> @user:device
                mks = extract_keys_from_content(content, sender)
                # NOTE: never log raw content here — it carries the decrypted media keys.
                log.info("call key event: ident=%s nkeys=%d types=%s",
                         ident, len(mks), [sorted(k.keys()) for k in mks])
                for mk in mks:
                    receiver.install(ident, mk)
        except Exception:
            log.exception("olm key-event dispatch failed")
        return None

    machine = getattr(client, "olm", None)
    if machine is None:
        log.warning("no olm machine on client; key hook not installed")
        return None
    for name in ("_handle_olm_event", "_handle_olm_events"):
        fn = getattr(machine, name, None)
        if fn is None:
            continue
        if name.endswith("_events"):  # plural signature: (events)
            def wrapper(events, _orig=fn, _self=machine):
                for ev in events:
                    try:
                        sender = getattr(ev, "sender", "")
                        sender_key = getattr(ev, "sender_key", "")
                        payload = getattr(ev, "payload", None)
                        if payload is not None:
                            dispatch(_self, sender, sender_key, payload)
                    except Exception:
                        log.exception("key event unwrap failed")
                return _orig(events)
            setattr(machine, name, MethodType(wrapper, machine))
        else:
            setattr(machine, name, MethodType(dispatch, machine))
        log.info("olm hook installed on %s", name)
        return dispatch
    return None
