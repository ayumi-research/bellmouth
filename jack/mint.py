"""Local participant-token minting (HS256) — §4.2 of the design.

Default posture: the watcher holds the LiveKit API key/secret and mints the
agent's participant token itself. Alternative (documented tradeoff): the
lk-jwt OpenID path — implemented as `mint_via_lkjwt` for deployments that do
not want the API secret on the watcher host.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt


@dataclass
class MintedToken:
    token: str
    room: str
    identity: str
    expires_at: float


def mint_participant_token(
    api_key: str,
    api_secret: str,
    room: str,
    identity: str,
    ttl_seconds: int = 3600,
    name: str | None = None,
) -> MintedToken:
    """Least-privilege participant token: join + publish/subscribe in ONE room.

    No roomCreate, no roomAdmin, no hidden-grants. Short TTL (call + margin).
    """
    now = int(time.time())
    exp = now + max(60, int(ttl_seconds))
    payload = {
        "exp": exp,
        "iss": api_key,
        "sub": identity,
        "nbf": now - 5,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
            # explicit omissions: roomCreate / roomList / roomAdmin / roomRecord
        },
    }
    if name:
        payload["name"] = name
    token = jwt.encode(payload, api_secret, algorithm="HS256")
    return MintedToken(token=token, room=room, identity=identity, expires_at=exp)


async def mint_via_lkjwt(
    lkjwt_url: str,
    room: str,
    identity: str,
    openid_token: str,
    http_session=None,
) -> MintedToken:
    """Reference client for the MatrixRTC authorization flow (lk-jwt).

    POST <lkjwt_url> with the homeserver OpenID token; lk-jwt returns
    {"accessToken": "<livekit jwt>", "url": "<ws endpoint>"}.
    """
    import aiohttp  # optional dep, only needed on this path

    body = {"id_token": openid_token, "room": room, "identity": identity}
    async with http_session or aiohttp.ClientSession() as s:
        async with s.post(lkjwt_url.rstrip("/") + "/", json=body) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return MintedToken(token=data["accessToken"], room=room, identity=identity, expires_at=0)
