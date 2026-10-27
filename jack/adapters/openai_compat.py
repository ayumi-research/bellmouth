"""OpenAI-compatible chat-completions adapter.

Impl #2 of the contract: any /v1/chat/completions endpoint. This is the slot
plugs into ANY OpenAI-compatible endpoint (ollama, vLLM, llama.cpp server,
OpenAI itself). Small local models recommended for call latency.
its native reasoning burns the token budget before content, see the
bolt-e4b-sidecar skill).

v1 posture (per design §9.5): voice-only, NO tool calls on the fallback
adapter — simple and honest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import AsyncIterator, Optional

import aiohttp

from .base import AgentUtterance, CallEndReason, CallSession, CallSummary
from . import task_client
from .task_router import build_context_envelope, extract_task, heuristic_is_task

log = logging.getLogger("jack.adapters.openai_compat")

DEFAULT_SYSTEM_PROMPT = (
    "You are a warm, playful AI agent on a live phone call with the person who "
    "deployed you. They may call from a car or a noisy place; if you mishear, ask "
    "them to repeat one word. "
    "Speak in short natural sentences (1-2, never more than 3). No markdown, no lists, no emoji. "
    "Keep replies under 40 words unless they ask for detail. Never ramble, never mention being a language model. "
    "Be curious, direct, a little cheeky."
)

# Adapter v2 (Door 3): when the caller asks something that needs REAL tools
# (time/date on the host, files, email, lookups), the small brain answers conversationally
# AND tags the turn so the full Hermes agent runs it on the Mac. Byte-stable
# suffix — never mutate it per turn (vLLM prefix cache).
TASK_TAG_SUFFIX = (
    " If his request needs a real tool on the Mac (run a command, check time/"
    "date/files, send something, look something up), end your reply with "
    "exactly one tag: [TASK: <the request restated precisely>]. "
    "For plain conversation, no tag."
)


class OpenAICompatAdapter:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        system_prompt: Optional[str] = None,
        think: bool | None = None,
        max_history: int = 24,
        max_tokens: int | None = None,
    ):
        self.base_url = (base_url or os.environ.get("OPENAI_COMPAT_BASE_URL", "")).rstrip("/")
        self.model = model or os.environ.get("OPENAI_COMPAT_MODEL", "")
        self.api_key = api_key or os.environ.get("OPENAI_COMPAT_API_KEY", "no-key-required")
        self.system_prompt = system_prompt or os.environ.get("OPENAI_COMPAT_SYSTEM", DEFAULT_SYSTEM_PROMPT)
        # Some models burn the token budget on thinking; disable by default.
        if think is None:
            think = os.environ.get("OPENAI_COMPAT_THINK", "false").lower() == "true"
        self.think = think
        # Reasoning models burn tokens on reasoning_content before content —
        # too small a budget makes content null (silence on the call).
        if max_tokens is None:
            max_tokens = int(os.environ.get("OPENAI_COMPAT_MAX_TOKENS", "500"))
        self.max_tokens = max(500, max_tokens)
        self.max_history = max_history
        self.task_bridge = bool(os.environ.get("JACK_TASK_BRIDGE_URL"))
        # JACK_ROUTE=bridge: EVERY turn rides the full agent brain via the
        # bridge (no picker, no [TASK:] tagging). The small brain stays as
        # bridge-down fallback only. Default "hybrid" picks per turn.
        self.route = {"bridge": "bridge", "hermes": "bridge"}.get(
            os.environ.get("JACK_ROUTE", "hybrid").strip().lower(),
            os.environ.get("JACK_ROUTE", "hybrid").strip().lower())
        self._session: CallSession | None = None
        self._history: list = [{
            "role": "system",
            "content": self.system_prompt + (TASK_TAG_SUFFIX if self.task_bridge else ""),
        }]
        self._queue: asyncio.Queue = asyncio.Queue()
        self._transcript: list = []
        self._flagged = False
        self._pending_tasks: set[asyncio.Task] = set()

    async def start(self, session_ctx: CallSession) -> None:
        self._session = session_ctx

    async def _chat(self, messages: list) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body: dict = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if self.think is False:
            body["think"] = False  # ollama-style flag; ignored elsewhere
            # vLLM-served reasoning models: disable thinking via the chat
            # template, else reasoning_content eats the budget / adds latency.
            body["chat_template_kwargs"] = {"enable_thinking": False}
            body["num_predict"] = self.max_tokens  # ollama's alias for max_tokens
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, headers=headers, json=body) as r:
                r.raise_for_status()
                data = await r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or msg.get("reasoning_content")
        if not content and reasoning:
            # budget-exhausted fallback: speak the reasoning tail rather than silence
            self._flagged = True
            content = reasoning[-400:]
        return content.strip()

    async def on_transcript(self, text: str) -> None:
        self._transcript.append({"role": "user", "text": text, "ts": time.time()})
        self._history.append({"role": "user", "content": text})
        if len(self._history) > self.max_history + 1:
            self._history = [self._history[0]] + self._history[-(self.max_history):]
        if self.route == "hermes" and self.task_bridge:
            # bridge mode: no small-brain turn at all, no picker — the full brain
            # answers every turn (async; result queued to TTS when it lands).
            t = asyncio.ensure_future(self._run_task_turn(text))
            self._pending_tasks.add(t)
            t.add_done_callback(self._pending_tasks.discard)
            return
        try:
            reply = await self._chat(self._history)
        except Exception as e:  # never crash the call on one bad turn
            reply = "Sorry, I glitched for a second — say that again?"
            self._flagged = True
        if reply:
            # never speak control tags: strip [TASK: ...] before queueing
            spoken = reply
            if self.task_bridge:
                _task, spoken = extract_task(reply)
            self._history.append({"role": "assistant", "content": reply})
            self._transcript.append({"role": "assistant", "text": spoken, "ts": time.time()})
            await self._queue.put(AgentUtterance(text=spoken, done=True))
        self._maybe_dispatch_task(text, reply)

    def _maybe_dispatch_task(self, user_text: str, reply: str) -> None:
        """Door 3: route a task turn to the full Hermes agent on the Mac.

        Layer 1: the brain self-tags [TASK: ...] in its reply. Layer 2: imperative
        heuristic on the caller's text (brain forgot to tag). The bridge call
        runs in a background task — the conversational reply is already
        speaking, the verified result is queued when it lands.
        """
        if not self.task_bridge:
            return
        task, _spoken = extract_task(reply or "")
        if task is None:
            if not heuristic_is_task(user_text):
                return
            task = user_text  # fallback: the caller's own words are the task
        t = asyncio.ensure_future(self._run_task_turn(task))
        self._pending_tasks.add(t)
        t.add_done_callback(self._pending_tasks.discard)

    async def _run_task_turn(self, task: str) -> None:
        bridge_failed = False
        result: str | None = None
        try:
            result = await task_client.run_task(
                task,
                context=[build_context_envelope(self._history, turns=6)],
                session_id=self._session.room_id if self._session else None,
            )
        except task_client.TaskBridgeError as e:
            self._flagged = True
            bridge_failed = True
            log.warning("task bridge failed (%s); falling back to small brain", e)
        except Exception:
            self._flagged = True
            log.exception("task turn crashed")
            bridge_failed = True
        if bridge_failed:
            # Door 1 as failover: a soulless answer beats silence on a call.
            try:
                result = await self._chat(self._history)
            except Exception:
                log.exception("small-brain fallback failed too")
                result = "That one didn't come back from the workshop — I can try again in a moment."
        if not result:
            result = "That one didn't come back from the workshop — I can try again in a moment."
        self._history.append({"role": "assistant", "content": result})
        self._transcript.append({"role": "assistant", "text": result, "ts": time.time()})
        await self._queue.put(AgentUtterance(text=result, done=True))

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
            flagged=self._flagged,
        )
