import os

import pytest

from jack.mint import mint_participant_token

KEY = "test-key"
SECRET = "test-secret-that-is-long-enough-for-hs256"


def claims(tok):
    import jwt
    return jwt.decode(tok, SECRET, algorithms=["HS256"])


def test_basic_claims():
    t = mint_participant_token(KEY, SECRET, "!room:hs", "@agent:hs", ttl_seconds=600)
    c = claims(t.token)
    assert c["iss"] == KEY and c["sub"] == "@agent:hs"
    assert c["video"]["room"] == "!room:hs"
    assert c["video"]["roomJoin"] is True
    assert c["exp"] - t.expires_at == 0


def test_no_admin_grants():
    t = mint_participant_token(KEY, SECRET, "!room:hs", "@agent:hs")
    c = claims(t.token)
    v = c["video"]
    for forbidden in ("roomCreate", "roomList", "roomAdmin", "roomRecord"):
        assert forbidden not in v or v[forbidden] in (False, None)


def test_short_ttl_floor():
    t = mint_participant_token(KEY, SECRET, "!room:hs", "@agent:hs", ttl_seconds=1)
    c = claims(t.token)
    assert c["exp"] - c.get("nbf", c["iat"] if "iat" in c else 0) >= 60
