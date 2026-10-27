"""Hermes bridge — Mac-side service that runs the FULL Hermes agent for the
Matrix voice-call jack (adapter v2, Door 3).

Runs on this Mac (100% local: GLM on the Spark, tools on the Mac, no cloud).
Listens on :8620 (localhost + tailscale/LAN IPs) and:

  POST /task   {task, context: [..], session_id?}  -> {ok, reply, seconds}
  GET  /health                                     -> {ok, hermes: bool}
  GET  /selftest                                   -> runs a REAL tool (date)
                                                      through the full path

Design:
- Each task = one `hermes chat --query-file -` subprocess (non-TTY => oneshot).
  The prompt embeds the last ~6 call turns as a context envelope, so
  follow-ups work without session plumbing (per-call persistent Hermes
  sessions via --resume are the documented next step).
- Reply verification rule: a 200 with ok=true is only returned when the
  hermes subprocess exited 0 AND produced non-empty text. The jack never
  speaks "done" from an unverified result.
- No secrets in this file; nothing to configure. The jack side points here
  via JACK_TASK_BRIDGE_URL in /opt/jack/.env on the sidecar.

Run (Mac):
  cd ~/workspace/matrix-call-jack
  nohup .venv/bin/python -m bridge.hermes_bridge >> /tmp/jack-hermes-bridge.log 2>&1 &
  (launcher script bridge/run.sh does exactly this)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time

from aiohttp import web

log = logging.getLogger("hermes_bridge")

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8620
HERMES_TIMEOUT_S = 180
SELFTEST_TOLERANCE_S = 120


def _hermes_bin() -> str:
    """Absolute path to the hermes CLI (bridge may run with a slim PATH)."""
    found = shutil.which("hermes")
    if found:
        return found
    for cand in (
        os.path.expanduser("~/.hermes/hermes-agent/.hermes/bin/hermes"),
        "/opt/homebrew/bin/hermes",
        "/usr/local/bin/hermes",
    ):
        if os.path.isfile(cand):
            return cand
    return "hermes"


HERMES_BIN = _hermes_bin()

SYSTEM_SUFFIX = (
    # Appended context for every voice task; keeps replies phone-sized.
    "Answer as if speaking on a phone call: 1-3 short sentences, no markdown, "
    "no lists, no emoji. Include the concrete result."
)


def _run_hermes(query: str, timeout_s: int = HERMES_TIMEOUT_S,
                session_id: str | None = None) -> str:
    """Run a Hermes query synchronously; return final text or raise.

    With session_id the call rides a PERSISTENT Hermes session (--resume
    --create-if-missing): the prefix cache stays warm (~3s/turn instead of
    ~9s cold) and follow-ups like "did you send it?" see the earlier turns.
    """
    cmd = [
        HERMES_BIN, "chat",
        "--query-file", "-",   # query via stdin: safe for arbitrary text
        "-Q",                  # quiet => oneshot, answer and exit
        # NOTE: --source <tag> is broken on this build (any non-"cli" source
        # fails the model connection with invalid_params); omitted.
    ]
    if session_id:
        # --create-if-missing pairs with --continue <NAME> (resume-by-name);
        # --resume wants an existing session ID and fails on new names.
        cmd += ["--continue", session_id, "--create-if-missing"]
    proc = subprocess.run(
        cmd, input=query.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"hermes exit {proc.returncode}: {err}")
    out = proc.stdout.decode(errors="replace").strip()
    if not out:
        raise RuntimeError("hermes produced no output")
    return out


def _sanitize_session(session_id) -> str | None:
    """Room ids contain !/: — Hermes session ids want [A-Za-z0-9_-]."""
    if not session_id:
        return None
    import re
    s = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))
    return f"voice-{s[:48]}" if s else None


# One hermes process at a time per session (two writers on one session id
# would fork the transcript).
_session_locks: dict[str, asyncio.Lock] = {}


def _build_query(payload: dict) -> str:
    task = (payload.get("task") or "").strip()
    if not task:
        raise ValueError("missing 'task'")
    envelope = "\n".join(str(c) for c in (payload.get("context") or []) if str(c).strip())
    parts = [
        "LIVE PHONE CALL with Zoltan (voice turn, transcribed — he may be driving).",
        "",
        "Recent call context (for pronouns and follow-ups only):",
        envelope or "(call just started — no prior turns)",
        "",
        f"Zoltan just said: {task}",
        "",
        "Rules: answer for SPEECH — 1-3 short sentences, no markdown, no lists, "
        "no emoji, concrete result included. If the request needs a tool "
        "(terminal, files, lookups), actually run it and report the real "
        "result; if a tool fails, say what failed in plain words — never "
        "claim success without the result in hand. If it's plain chat, just "
        "be Mei: warm, direct, a little cheeky.",
    ]
    return "\n".join(parts)


async def handle_task(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
    try:
        query = _build_query(payload)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)

    session = _sanitize_session(payload.get("session_id"))
    loop = asyncio.get_running_loop()
    t0 = time.time()
    try:
        if session:
            lock = _session_locks.setdefault(session, asyncio.Lock())
            async with lock:
                reply = await loop.run_in_executor(
                    None, _run_hermes, query, HERMES_TIMEOUT_S, session)
        else:
            reply = await loop.run_in_executor(None, _run_hermes, query)
    except Exception as e:
        log.exception("task failed")
        return web.json_response({"ok": False, "error": str(e)[:300]}, status=200)
    seconds = round(time.time() - t0, 1)
    log.info("task done in %.1fs (session=%s): %.80s -> %.80s",
             seconds, session or "oneshot", payload.get("task", ""), reply)
    return web.json_response({"ok": True, "reply": reply, "seconds": seconds,
                              "session": session})


async def handle_health(request: web.Request) -> web.Response:
    try:
        v = subprocess.run([HERMES_BIN, "--version"], capture_output=True, timeout=15)
        hermes_ok = v.returncode == 0
        version = v.stdout.decode().strip()[:80]
    except Exception:
        hermes_ok, version = False, ""
    return web.json_response({"ok": True, "hermes": hermes_ok, "version": version})


async def handle_selftest(request: web.Request) -> web.Response:
    """Run a REAL tool (date) through the FULL path and verify the output.

    Sends 'run date' as a caller-style task, then compares the agent's reply
    against the Mac's own `date`. ok=true only if the tool result made it
    through and matches within tolerance.
    """
    query = _build_query({
        "task": "Run the shell command `date '+%H:%M, %A %B %-d'` in the "
                "terminal and reply with exactly what it printed (the time "
                "and date on this Mac).",
        "context": [],
    })
    loop = asyncio.get_running_loop()
    t0 = time.time()
    try:
        reply = await loop.run_in_executor(None, _run_hermes, query, 240)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"hermes run failed: {e}"[:300]})
    expected = subprocess.run(
        ["date", "+%H:%M"], capture_output=True, text=True
    ).stdout.strip()

    def _hhmm(s: str) -> str | None:
        import re
        m = re.search(r"\b(\d{1,2}:\d{2})\b", s)
        return m.group(1) if m else None

    got = _hhmm(reply)
    match = False
    if got:
        gh, gm = (int(x) for x in got.split(":"))
        eh, em = (int(x) for x in expected.split(":"))
        diff = abs((gh * 60 + gm) - (eh * 60 + em)) % (24 * 60)
        diff = min(diff, 24 * 60 - diff)
        match = diff <= SELFTEST_TOLERANCE_S // 60
    seconds = round(time.time() - t0, 1)
    result = {
        "ok": match,
        "reply": reply,
        "expected_hhmm": expected,
        "parsed_hhmm": got,
        "seconds": seconds,
    }
    log.info("selftest: ok=%s in %.1fs", match, seconds)
    return web.json_response(result)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = web.Application()
    app.router.add_post("/task", handle_task)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/selftest", handle_selftest)
    # Bind loopback + all interfaces (tailscale ACLs + LAN firewall gate access;
    # the Mac firewall prompts once for the python binary).
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
