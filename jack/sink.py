"""Hangup sink (§4.4): bounded call summary -> the DM + optional file/webhook.

Privacy default: transcript lives in memory only; what lands in the DM is a
short summary, not a dump.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from .adapters.base import CallSummary

log = logging.getLogger("jack.sink")


def summarize(cs: CallSummary) -> str:
    dur = f"{cs.duration_s // 60}m{cs.duration_s % 60:02d}s"
    turns = len([t for t in cs.transcript if t["role"] == "user"])
    head = f"📞 Call ended ({dur}, {turns} turns, {cs.reason.value})"
    if cs.flagged:
        head += " — answered by fallback"
    lines = [head]
    for t in cs.transcript[-12:]:  # bounded: last 12 turns
        who = "them" if t["role"] == "user" else "me"
        text = t["text"][:200]
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


async def post_call_summary(client, cs: CallSummary, text_env: str = "", webhook: str = "") -> None:
    body = summarize(cs)
    room_id = cs.room_id
    if room_id:
        await client.room_send(room_id, "m.room.message", {"msgtype": "m.text", "body": body})
        log.info("summary posted to %s...", room_id[:14])
    if webhook:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(webhook, json={"summary": body, "room_id": room_id})
        except Exception:
            log.exception("webhook sink failed")
    if os.environ.get("JACK_SUMMARY_FILE"):
        path = os.environ["JACK_SUMMARY_FILE"]
        with open(path, "a") as f:
            f.write(json.dumps({"ts": time.time(), "summary": body}) + "\n")
