"""Outbound MatrixRTC media-key distribution + encrypted publishing (the E2EE loop-closer).

Mirrors what we already parse on the receive side (jack/e2ee_keys.py):
every participant generates a random 16-byte media key per call and
olm-ships it to the other members' devices as
`io.element.call.encryption_keys` (Element shape) and
`m.rtc.encryption_key` (standardized shape). Outbound audio is then
frame-encrypted by the livekit KeyProvider (HKDF, GCM) using OUR key
installed under OUR livekit identity — the SFU only ever sees ciphertext.

Sender-side order matters:
    connect(room_options with EncryptionType.GCM)
      -> set_key(our_identity, key, 0)      # before ANY publish
      -> publish tracks
      -> distribute() via olm               # before membership publish

Private-API note (pinned matrix-nio 0.26.0): OlmMachine._olm_encrypt and the
auto-session-creation inside _handle_key_claiming are used; both verified
against the installed source. Recheck on nio upgrades.
"""

from __future__ import annotations

import base64
import logging
import os
import time

from nio import ToDeviceMessage

log = logging.getLogger("jack.e2ee_send")

ELEMENT_KEY_TYPE = "io.element.call.encryption_keys"
STANDARD_KEY_TYPE = "m.rtc.encryption_key"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


class OutboundKeys:
    """Our per-call media key: generate, install for our identity, olm-distribute."""

    def __init__(self, client, user_id: str, device_id: str):
        self.client = client
        self.user_id = user_id
        self.device_id = device_id
        self.key: bytes | None = None
        self.index = 0

    # -- key lifecycle ----------------------------------------------------
    def generate(self) -> bytes:
        self.key = os.urandom(16)
        log.info("outbound media key generated (%d bytes, index %d)", len(self.key), self.index)
        return self.key

    def install_own(self, room, livekit_identity: str) -> bool:
        """Register OUR key under OUR identity so the frame cryptor encrypts our publish."""
        from . import e2ee_keys
        if self.key is None:
            self.generate()
        assert self.key is not None  # narrowed for type checkers
        kp = e2ee_keys.key_provider_of(room)
        if kp is None:
            log.warning("no key provider; outbound audio would publish PLAINTEXT")
            return False
        try:
            kp.set_key(livekit_identity, self.key, self.index)
            log.info("own key installed for local frames (identity=%s idx=%d)",
                     livekit_identity[:30], self.index)
            return True
        except Exception:
            log.exception("install_own failed")
            return False

    # -- key distribution --------------------------------------------------
    def _payloads(self, room_id: str) -> list[tuple[str, dict]]:
        """Compat element event (modern+legacy fields, per current element-call
        sender) + the MSC4143 standardized event (correct schema: media_key
        singular). Receivers tolerate either; JS dispatches the experimental
        type, so it is the primary."""
        key_obj = {"index": self.index, "key": b64(self.key)}
        member_id = f"{self.user_id}:{self.device_id}"
        ts = int(time.time() * 1000)
        element = {
            "room_id": room_id,
            "member_id": member_id,                      # modern (preferred)
            "media_key": dict(key_obj),                  # modern (preferred)
            "keys": dict(key_obj),                       # legacy fallback
            "member": {                                  # legacy fallback
                "claimed_device_id": self.device_id,
                "id": member_id,
            },
            "session": {"call_id": "", "application": "m.call", "scope": "m.room"},
            "sent_ts": ts,
        }
        standardized = {
            "room_id": room_id,
            "member_id": member_id,
            "media_key": dict(key_obj),
        }
        return [(ELEMENT_KEY_TYPE, element), (STANDARD_KEY_TYPE, standardized)]

    def select_index(self, room, livekit_identity: str) -> bool:
        """Installing a key is NOT selecting it: pin the local sender cryptor
        to our key index before the first audio frame (astra-verified FFI
        behavior). Call AFTER publishing tracks."""
        try:
            mgr = getattr(room, "e2ee_manager", None)
            for cryptor in (mgr.frame_cryptors() if mgr else []):
                if getattr(cryptor, "participant_identity", "") == livekit_identity:
                    cryptor.set_key_index(self.index)
                    log.info("sender cryptor index pinned to %d (%s)", self.index,
                             livekit_identity[:30])
                    return True
            log.warning("no local frame cryptor found for %s; index not pinned",
                        livekit_identity[:30])
            return False
        except Exception:
            log.exception("select_index failed")
            return False

    async def distribute(self, room_id: str, member_users: list[str]) -> int:
        """olm-encrypt our key to every member device of the call. Returns devices messaged."""
        if self.key is None:
            self.generate()
        machine = getattr(self.client, "olm", None)
        if machine is None:
            log.warning("no olm machine; cannot distribute keys")
            return 0
        targets = [u for u in dict.fromkeys(member_users) if u != self.user_id]
        if not targets:
            log.info("no other members to distribute keys to")
            return 0

        # Claim OTKs -> _handle_key_claiming verifies + creates OutboundSessions.
        try:
            claim_set = {u: list(self._devices_of(u).keys()) or [""] for u in targets}
            claim_set = {u: devs for u, devs in claim_set.items() if devs}
            if claim_set:
                resp = await self.client.keys_claim(claim_set)
                if getattr(resp, "event_id", None) is None and not str(resp).startswith("KeysClaim"):
                    log.warning("keys_claim response: %s", str(resp)[:120])
        except Exception:
            log.exception("keys_claim failed; olm sessions may be missing")

        sent = 0
        for user in targets:
            devices = self._devices_of(user)
            for devid, dev in devices.items():
                session = machine.session_store.get(dev.curve25519)
                if session is None:
                    # claim response creates it lazily; if still missing, skip loudly
                    log.warning("no olm session for %s:%s — key not delivered", user[:20], devid[:10])
                    continue
                for inner_type, content in self._payloads(room_id):
                    try:
                        olm_content = machine._olm_encrypt(session, dev, inner_type, content)
                        msg = ToDeviceMessage(
                            type="m.room.encrypted",
                            recipient=user,
                            recipient_device=devid,
                            content=olm_content,
                        )
                        resp = await self.client.to_device(msg)
                        if isinstance(resp, Exception) or type(resp).__name__ == "ToDeviceError":
                            log.warning("to_device to %s:%s failed: %s", user[:20], devid[:10], str(resp)[:120])
                        else:
                            sent += 1
                    except Exception:
                        log.exception("key distribution to %s:%s failed", user[:20], devid[:10])
        log.info("outbound key distributed to %d device(s) in %s...", sent, room_id[:14])
        return sent

    def _devices_of(self, user_id: str) -> dict:
        try:
            return dict(self.client.device_store[user_id])
        except Exception:
            return {}
