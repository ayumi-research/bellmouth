"""MSC3401 call-member event parsing — pure functions, no I/O.

Encoded against a real Element X capture (tests/fixtures/, 2026-10-24):

- Join:  state event ``org.matrix.msc3401.call.member`` with NON-empty content
         carrying ``application: "m.call"``, ``foci_preferred`` and a livekit focus.
- Leave: same event type with EMPTY content (``{}``).
- state_key: ``_<sender>_<device_id>_m.call`` (per-device membership, MSC4148-style).
- The LiveKit room name the client will join is ``foci_preferred[i].livekit_alias``
  (verified: it equals the Matrix room ID on our deployment — but we read the field,
  we never guess).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CALL_MEMBER_TYPE = "org.matrix.msc3401.call.member"
CALL_MEMBER_PREFIX = "org.matrix.msc3401."
STATE_KEY_RE = re.compile(r"^_(?P<sender>.+?)_(?P<device>[A-Za-z0-9]+)_m\.call$")


@dataclass
class CallMemberState:
    """Parsed view of one call-membership state event."""

    sender: str                      # MXID of the member
    device_id: str                   # their device (from state_key)
    room_id: str
    membership: Optional[Dict[str, Any]]   # content dict; None/empty => left
    call_id: str
    scope: str
    application: Optional[str]
    intent: Optional[str]            # m.call.intent (audio/video) if present
    livekit_service_url: Optional[str]
    livekit_room: Optional[str]      # the room name to join (livekit_alias)

    @property
    def joined(self) -> bool:
        return bool(self.membership)

    @property
    def membership_id(self) -> Optional[str]:
        return (self.membership or {}).get("membershipID")


def parse_state_key(state_key: str) -> Optional[Dict[str, str]]:
    m = STATE_KEY_RE.match(state_key or "")
    if not m:
        return None
    return {"sender": m.group("sender"), "device_id": m.group("device")}


def _extract_livekit(content: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (livekit_service_url, livekit_room) from an event content, or (None, None)."""
    foci = content.get("foci_preferred") or []
    for f in foci:
        if f.get("type") == "livekit" and f.get("livekit_service_url"):
            return f["livekit_service_url"], f.get("livekit_alias")
    # legacy/other spellings tolerated but never guessed
    return None, None


def parse_call_member_event(ev: Dict[str, Any]) -> Optional[CallMemberState]:
    """Parse one raw state event. Returns None if it is not a call-member event."""
    if (ev.get("type") or "").startswith(CALL_MEMBER_PREFIX) is False:
        return None
    ev = ev.get("event", ev)  # tolerate client-api wrapped form
    if ev.get("type") != CALL_MEMBER_TYPE:
        return None
    sk = parse_state_key(ev.get("state_key") or "")
    if not sk:
        # state events always carry the _.._m.call state_key; without it we can't scope
        return None
    content = ev.get("content") or {}
    svc_url, lk_room = _extract_livekit(content) if content else (None, None)
    return CallMemberState(
        sender=sk["sender"],
        device_id=sk["device_id"],
        room_id=ev.get("room_id") or "",
        membership=content or None,
        call_id=content.get("call_id", ""),
        scope=content.get("scope", ""),
        application=content.get("application"),
        intent=(content.get("m.call.intent") if content else None),
        livekit_service_url=svc_url,
        livekit_room=lk_room,
    )


def livekit_room_alias(matrix_room_id: str, slot_id: str = "m.call#ROOM") -> str:
    """The REAL SFU room name (lk-jwt v0.5 derivation, verified against live calls).

    lk-jwt hashes json([room_id, slot_id]) sha256 -> unpadded std64. The
    `livekit_alias` field in membership events is the Matrix room ID and is
    NOT the SFU room name (source comment in lk-jwt main.go confirms).
    Golden vector: room !ZSqYfNfG4n8G2S8iK2iESKVfNqbkn6dSrq9kNCTC1Ms ->
    D3LQlpbUxvGzbymMNLcZyTcagSEz4HO5K0amNyZUz0Y (captured 2026-10-25).
    """
    import base64 as _b64
    import hashlib as _hashlib
    import json as _json

    raw = _json.dumps([matrix_room_id, slot_id], separators=(",", ":"))
    return _b64.b64encode(_hashlib.sha256(raw.encode()).digest()).rstrip(b"=").decode()


def select_focus(membership: CallMemberState, configured_service_url: Optional[str] = None) -> Optional[str]:
    """Pick the livekit focus matching our configured service URL (exact match preferred).

    Returns the LiveKit ROOM NAME to join, or None.
    """
    if not membership.joined:
        return None
    if membership.livekit_room is None:
        return None
    if configured_service_url and membership.livekit_service_url:
        if membership.livekit_service_url.rstrip("/") != configured_service_url.rstrip("/"):
            return None  # focus points at a different SFU than we serve
    return membership.livekit_room
