"""Echo adapter — the integration-rig workhorse. No LLM, zero deps."""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

from .base import AgentUtterance, CallEndReason, CallSession, CallSummary


class EchoAdapter:
    """Speaks back a fixed greeting + echoes the user's last transcript.

    Used by the testrig to verify: mint -> join -> audio path -> hangup sink,
    without any model in the loop.
    """

    def __init__(self, greeting: str = "Jack connected. I can hear you. Echo adapter active."):
        self._greeting = greeting
        self._session: CallSession | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._transcript: list = []
        self._last_user: str | None = None

    async def start(self, session_ctx: CallSession) -> None:
        self._session = session_ctx
        await self._queue.put(AgentUtterance(text=self._greeting, done=True))

    async def on_transcript(self, text: str) -> None:
        self._transcript.append({"role": "user", "text": text, "ts": time.time()})
        self._last_user = text
        await self._queue.put(AgentUtterance(text=f"Echo: {text}", done=True))

    def transcript_stream(self) -> AsyncIterator[AgentUtterance]:
        async def _gen():
            while True:
                yield await self._queue.get()

        return _gen()

    async def end(self, reason: CallEndReason) -> CallSummary:
        now = time.time()
        return CallSummary(
            room_id=self._session.room_id if self._session else "",
            caller=self._session.caller if self._session else "",
            started_at=self._session.started_at if self._session else now,
            ended_at=now,
            reason=reason,
            transcript=list(self._transcript),
        )
