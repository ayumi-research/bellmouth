"""AgentAdapter contract (§4.3) — the open-source seam of the jack.

The jack phones an EXISTING agent into the call; the adapter defines how a
given agent talks. Turn-taking (VAD/barge-in/endpointing) lives in the MEDIA
layer (jack/media.py), not here — adapters receive clean user transcripts and
yield utterances.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional, Protocol, runtime_checkable


class CallEndReason(str, Enum):
    CALLER_LEFT = "caller_left"
    AGENT_HANGUP = "agent_hangup"
    ADAPTER_CRASH = "adapter_crash"
    WATCHER_CRASH = "watcher_crash"
    SFU_LOST = "sfu_lost"
    SECOND_CALL = "second_call_declined"


@dataclass
class CallSession:
    room_id: str
    caller: str
    livekit_room: str
    livekit_url: str
    token: str
    identity: str
    started_at: float = field(default_factory=time.time)


@dataclass
class AgentUtterance:
    text: str
    done: bool = True          # False while streaming


@dataclass
class CallSummary:
    room_id: str
    caller: str
    started_at: float
    ended_at: float
    reason: CallEndReason
    transcript: list = field(default_factory=list)   # [{role, text, ts}]
    flagged: bool = False         # e.g. answered by fallback adapter
    note: str = ""

    @property
    def duration_s(self) -> int:
        return int(self.ended_at - self.started_at)


@runtime_checkable
class AgentAdapter(Protocol):
    async def start(self, session_ctx: CallSession) -> None: ...
    async def on_transcript(self, text: str) -> None: ...
    def transcript_stream(self) -> AsyncIterator[AgentUtterance]: ...
    async def end(self, reason: CallEndReason) -> CallSummary: ...
