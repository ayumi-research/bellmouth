"""Thin async client for the Mac-side Hermes bridge (adapter v2, Door 3).

The bridge (bridge/agent_bridge.py, LAN host :8620) runs the full agent
with real tools. This module only speaks HTTP to it — all logic lives on
the bridge host so the phone host stays lean.

Env:
  JACK_TASK_BRIDGE_URL  e.g. http://<bridge-host>:8620 (LAN/tailnet IP of the
                        machine running bridge/agent_bridge.py)
  JACK_TASK_TIMEOUT_S   per-task budget in seconds (default 120)
"""

from __future__ import annotations

import os
from typing import Optional

import aiohttp


class TaskBridgeError(RuntimeError):
    """Bridge unreachable, timed out, or returned a failure."""


def bridge_url() -> str:
    return os.environ.get("JACK_TASK_BRIDGE_URL", "").rstrip("/")


def task_timeout() -> int:
    return int(os.environ.get("JACK_TASK_TIMEOUT_S", "120"))


async def run_task(
    task: str,
    context: list[str] | None = None,
    session_id: Optional[str] = None,
    url: Optional[str] = None,
    timeout_s: Optional[int] = None,
) -> str:
    """POST a task turn to the bridge; return the agent's final spoken text.

    Raises TaskBridgeError on any failure — callers convert that to a honest
    spoken fallback. The bridge only returns 200 with a non-empty reply when
    the Hermes run actually exited 0 with output.
    """
    base = (url or bridge_url()).rstrip("/")
    if not base:
        raise TaskBridgeError("JACK_TASK_BRIDGE_URL not set")
    payload = {
        "task": task,
        "context": context or [],
        "session_id": session_id,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s or task_timeout())
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{base}/task", json=payload) as r:
                if r.status != 200:
                    detail = (await r.text())[:200]
                    raise TaskBridgeError(f"bridge HTTP {r.status}: {detail}")
                data = await r.json()
    except TaskBridgeError:
        raise
    except Exception as e:  # aiohttp errors, JSON decode, ...
        raise TaskBridgeError(f"bridge unreachable: {e}") from e
    if not data.get("ok"):
        raise TaskBridgeError(f"bridge task failed: {data.get('error', 'unknown')}")
    reply = (data.get("reply") or "").strip()
    if not reply:
        raise TaskBridgeError("bridge returned empty reply")
    return reply
