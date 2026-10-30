"""MatrixRTC media-key transport: lets the jack decrypt Element X's SFrame audio.

Root cause this fixes: encrypted rooms make Element X publish the caller's mic with
SFrame E2EE; keys travel in `io.element.call.encryption_keys` / `m.rtc.encryption_key`
events (olm to-device AND/OR megolm room events). We install them into the livekit
E2EE KeyProvider per participant identity.

Device identity: the voice account (@agent:hs.example) with device_id JACK01,
olm store in /opt/jack/crypto_store (JACK_CRYPTO_STORE).
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


def make_olm_hook(client, receiver_holder, query_scheduler=None):
    """Intercept decrypted olm to-device events (nio-private hook, version-sensitive).

    receiver_holder: object with a `.receiver` attribute (the watcher) so the
    active per-call receiver can be swapped without re-installing the hook.
    query_scheduler: optional test seam `schedule(sender)`; default fires a real
    keys_query on the running loop and retries stashed events afterwards.
    The ORIGINAL handler is ALWAYS invoked afterwards — dropping it silently
    kills ordinary Megolm room-key delivery (found by pre-release review).

    Unknown-device policy (fail-closed): a key event whose claimed device is
    missing from the device store is NOT trusted — it is stashed, a keys_query
    for the sender is scheduled, and the event is re-verified once the store
    fills. Still unknown after the query -> dropped (a later republished key
    event from Element X succeeds then).
    """
    from types import MethodType

    machine = getattr(client, "olm", None)
    if machine is None:
        log.warning("no olm machine on client; key hook not installed")
        return None
    original = machine._handle_olm_event  # bound method; capture BEFORE replacing

    def sender_key_matches(sender: str, device_id: str, sender_key: str):
        """True: match. False: store contradicts. None: device not in store."""
        try:
            dev = machine.device_store[sender][device_id]
            return dev.curve25519 == sender_key
        except KeyError:
            return None

    pending: dict[str, list] = {}   # sender -> [payload, ...] awaiting verification
    inflight: set[str] = set()      # senders with a keys_query in flight
    MAX_PENDING_PER_USER = 50

    def retry_stashed(sender: str) -> None:
        for sender_key, pl in pending.pop(sender, []):
            process(sender, sender_key, pl, last_chance=True)

    def schedule_query(sender: str) -> None:
        if query_scheduler is not None:      # test seam
            query_scheduler(sender)
            return
        if sender in inflight:
            return  # the running task's retry covers everything stashed so far
        if hasattr(machine, "users_for_key_query"):
            machine.users_for_key_query.add(sender)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("no running loop for keys_query (%s) — key event held", sender[:20])
            return
        inflight.add(sender)

        async def _run():
            try:
                try:
                    await client.keys_query()
                except Exception as e:
                    log.warning("keys_query for %s failed: %s", sender[:20], e)
            finally:
                inflight.discard(sender)
            retry_stashed(sender)

        loop.create_task(_run())

    def process(sender, sender_key, payload, last_chance=False) -> bool:
        """Handle one key event; False means stashed pending verification."""
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not (isinstance(payload, dict) and payload.get("type") in KEY_TYPES):
            return True
        recv = getattr(receiver_holder, "receiver", None)
        content = payload.get("content") or payload
        # bind to the ACTIVE call: ignore keys for other rooms
        active_room = getattr(recv, "matrix_room_id", "") if recv else ""
        if active_room and content.get("room_id") not in (None, "", active_room):
            log.info("key event for inactive room %s ignored", str(content.get("room_id"))[:14])
            return True
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
        # authenticate the claim: sender_key must be the claimed device's
        # curve25519 (defends a forged member.id)
        claimed_dev = (member or {}).get("claimed_device_id") if isinstance(member, dict) else ""
        claimed_dev = claimed_dev or content.get("device_id") or ""
        if claimed_dev:
            match = sender_key_matches(sender, claimed_dev, sender_key)
            if match is False:
                log.warning("key event sender_key mismatch for %s:%s — dropped",
                            sender[:20], str(claimed_dev)[:10])
                return True
            if match is None:
                # device not in store yet: fail-closed — stash, query, re-verify
                if last_chance:
                    log.warning("device %s:%s still unknown after keys_query — "
                                "key event dropped (fail-closed)", sender[:20],
                                str(claimed_dev)[:10])
                    return True
                q = pending.setdefault(sender, [])
                if len(q) >= MAX_PENDING_PER_USER:
                    q.pop(0)
                q.append((sender_key, payload))
                schedule_query(sender)
                return False
        if recv is not None:
            mks = extract_keys_from_content(content, sender)
            # NOTE: never log raw content — it carries decrypted media keys.
            log.info("call key event: ident=%s nkeys=%d types=%s",
                     ident, len(mks), [sorted(k.keys()) for k in mks])
            for mk in mks:
                recv.install(ident, mk)
        return True

    def dispatch(self, sender, sender_key, payload):
        try:
            process(sender, sender_key, payload)
        except Exception:
            log.exception("olm key-event dispatch failed")
        # ALWAYS delegate: ordinary Megolm room keys etc. must reach nio.
        return original(sender, sender_key, payload)

    setattr(machine, "_handle_olm_event", MethodType(dispatch, machine))
    log.info("olm hook installed (delegating to original handler)")
    return dispatch
