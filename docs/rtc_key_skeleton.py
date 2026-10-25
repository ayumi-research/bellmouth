"""Research integration skeleton; not wired into jack and not call-tested.

API construction checked with livekit 1.1.21 and matrix-nio 0.26.0.
Olm and live-call interoperability require validation on the deployment host.
The caller supplies validated membership -> LiveKit identity bindings.
Use one instance per call; discard keys and bindings when the call ends.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from types import MethodType

from livekit import rtc
from nio import AsyncClient, AsyncClientConfig, UnknownEvent

KEY_TYPES = {"io.element.call.encryption_keys", "m.rtc.encryption_key"}
# Conservative native limit documented by element-hq/matrix-rust-rtc,
# crates/matrix-rtc-livekit/src/keys.rs (NATIVE_KEY_RING_MAX).
# JS accepts 256 slots; some native builds instead abort on invalid indexes.
NATIVE_RING_SIZE = 255


@dataclass(frozen=True)
class Binding:
    user_id: str
    device_id: str
    member_id: str | None
    livekit_identity: str
    curve25519: str
    ed25519: str


def receive_options() -> rtc.RoomOptions:
    # Fail explicitly on SDKs lacking selectable HKDF; PBKDF2 is incompatible.
    provider = rtc.KeyProviderOptions(
        shared_key=None,
        ratchet_salt=b"LKFrameEncryptionKey",
        ratchet_window_size=10,
        key_ring_size=NATIVE_RING_SIZE,
        failure_tolerance=-1,
        key_derivation_function=rtc.KeyDerivationFunction.HKDF,
    )
    return rtc.RoomOptions(
        auto_subscribe=False,
        encryption=rtc.E2EEOptions(
            key_provider_options=provider,
            # Initializes receive cryptors but advertises local media as plain.
            encryption_type=rtc.EncryptionType.NONE,
        ),
    )


def crypto_client(homeserver, user_id, device_id, access_token, store_path):
    # device_id MUST belong to access_token, as returned by /account/whoami.
    Path(store_path).mkdir(parents=True, exist_ok=True, mode=0o700)
    client = AsyncClient(
        homeserver, user_id, device_id=device_id, store_path=store_path,
        config=AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True),
    )
    client.restore_login(user_id, device_id, access_token)
    if client.olm is None:
        raise RuntimeError("Install matrix-nio[e2e] and initialize crypto storage")
    return client


def install_olm_hook(client: AsyncClient, queue: asyncio.Queue):
    """Version-sensitive private hook, installed before the first sync.

    nio invokes this AFTER Olm decrypt + envelope validation. Preserve the
    ordinary room-key dispatcher. Never feed raw to-device JSON into this hook.
    The production replacement should be a small pinned nio patch/extension.
    """
    machine = client.olm
    original = machine._handle_olm_event

    def dispatch(self, sender, sender_key, payload):
        if payload.get("type") in KEY_TYPES:
            queue.put_nowait((sender, sender_key, payload))
            return None
        return original(sender, sender_key, payload)

    machine._handle_olm_event = MethodType(dispatch, machine)


class ReceiveKeys:
    def __init__(self, matrix_room_id: str, room: rtc.Room):
        self.matrix_room_id = matrix_room_id
        self.room = room
        self.bindings: list[Binding] = []
        self.keys: dict[tuple[str, int], bytes] = {}
        self.connected = False

    def accept_olm(self, sender, sender_key, payload):
        if payload.get("type") not in KEY_TYPES:
            return
        c = payload["content"]
        if c.get("room_id") != self.matrix_room_id:
            return
        member = c.get("member") or {}
        member_id = c.get("member_id", member.get("id"))
        claimed_device = member.get("claimed_device_id")
        if payload["type"] == "io.element.call.encryption_keys":
            if not isinstance(claimed_device, str) or not claimed_device:
                raise ValueError("Missing claimed device ID")
        elif not isinstance(member_id, str) or not member_id:
            raise ValueError("Missing RTC member ID")
        session = c.get("session")
        if session is not None and (
            session.get("call_id") != ""
            or session.get("application") != "m.call"
            or session.get("scope") != "m.room"
        ):
            return
        candidates = [b for b in self.bindings
                      if b.user_id == sender and b.curve25519 == sender_key
                      and b.ed25519 == payload.get("keys", {}).get("ed25519")
                      and (claimed_device is None or b.device_id == claimed_device)
                      and (member_id is None or b.member_id == member_id)]
        if len(candidates) != 1:
            # Production: buffer briefly and retry after device/membership sync.
            raise ValueError("RTC key cannot be bound to one active caller device")
        media_key = c.get("media_key", c.get("keys"))
        self.install(candidates[0], media_key)

    def install(self, binding: Binding, media_key: dict):
        index = media_key["index"]
        encoded = media_key["key"]
        if type(index) is not int or not 0 <= index < 256:
            raise ValueError("Invalid RTC key index")
        if index >= NATIVE_RING_SIZE:
            # Do not remap modulo 255: the frame carries the original index.
            raise ValueError("Native ring cannot safely represent index 255")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        if len(raw) not in (16, 32):
            raise ValueError("Unexpected sender-key length; inspect client version")
        self.keys[binding.livekit_identity, index] = raw
        if self.connected:
            self.room.e2ee_manager.key_provider.set_key(
                binding.livekit_identity, raw, index,
            )
            self.subscribe(binding.livekit_identity)

    def subscribe(self, identity):
        participant = self.room.remote_participants.get(identity)
        if participant is not None:
            for publication in participant.track_publications.values():
                if publication.kind == rtc.TrackKind.KIND_AUDIO:
                    publication.set_subscribed(True)

    def flush_after_connect(self):
        self.connected = True
        for (identity, index), raw in self.keys.items():
            self.room.e2ee_manager.key_provider.set_key(identity, raw, index)
            self.subscribe(identity)


def install_legacy_room_callback(client, receiver):
    async def on_event(matrix_room, event):
        if matrix_room.room_id != receiver.matrix_room_id:
            return
        if not event.decrypted or event.source.get("type") not in KEY_TYPES:
            return
        c = event.source["content"]
        if c.get("call_id") != "" or not isinstance(c.get("keys"), list):
            return
        candidates = [b for b in receiver.bindings
                      if b.user_id == event.sender and b.device_id == c.get("device_id")
                      and b.curve25519 == event.sender_key]
        if len(candidates) != 1:
            return
        # Production: reject stale room events using call generation and timestamps;
        # preserve old indexes during rotation, and handle Megolm missing-key retries.
        for key in c["keys"]:
            receiver.install(candidates[0], key)
    client.add_event_callback(on_event, UnknownEvent)


# Integration order (reuse the jack's existing membership and track handlers):
# 1. crypto_client(...); install_olm_hook(...); install_legacy_room_callback(...).
# 2. Upload keys if client.should_upload_keys, checking the response for errors.
# 3. Run ONE sync loop. sync_forever manages key upload/query housekeeping; if
#    retaining jack's manual sync(), explicitly service should_upload_keys and
#    should_query_keys. Do not run both loops against the same device.
# 4. Populate bindings from active call membership + queried/authenticated device
#    keys + the corresponding actual LiveKit participant.identity.
# 5. await room.connect(url, token, options=receive_options()).
#    receiver.flush_after_connect(); publish membership using the SAME device ID.
# 6. Drain queue: receiver.accept_olm(*await queue.get()). Handle/retry unknown
#    bindings without terminating sync. On track_published, call subscribe again
#    only if that identity has a key. track_subscribed -> rtc.AudioStream(track).
# 7. Hangup: unpublish membership, disconnect, cancel tasks, discard receiver.
