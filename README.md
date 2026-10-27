# the jack — a Matrix voice-call agent that answers with a real brain

**the jack** is a daemon that sits on a Matrix account, waits for an
[Element Call](https://element.io/call) (MatrixRTC) ring, auto-answers,
joins the LiveKit SFU, and holds a real *spoken* conversation — speech in,
agent brain, speech out. No bot-framework puppet strings: the caller talks
to an actual agent with tools and memory.

Built and battle-tested on a self-hosted stack (Synapse + LiveKit + lk-jwt,
E2EE end to end, zero cloud in the loop), but any Matrix homeserver with an
Element-Call-capable RTC stack works.

```
 caller (Element X, E2EE)
        │  org.matrix.msc3401.call.member state events
        ▼
 ┌──────────────┐   olm to-device media keys   ┌─────────────┐
 │   watcher    │◀─────────────────────────────│  Synapse HS │
 │  (the jack)  │                              └─────────────┘
 └──────┬───────┘
        │ SFU room (name derived, not the alias!)   ┌───────────┐
        ├──────────────────────────────────────────▶│  LiveKit  │
        │                                           └───────────┘
        │  VAD → STT → brain → TTS
        ▼
 ┌─────────────────────────────────────────────┐
 │ small LLM (chat) ── [TASK] ──▶ full agent   │
 │                            (tools, memory)  │
 └─────────────────────────────────────────────┘
```

## What makes it interesting

- **Answers MatrixRTC calls properly.** Derives the *real* SFU room name from
  the room ID (the `livekit_alias` in the membership event is a trap — it 404s),
  publishes its own membership state so Element X stops ringing, speaks with
  the participant identity `<mxid>:<DEVICE>` (a bare mxid identity = "audio in
  SFU, caller deaf" — the bug that cost us a whole evening).
- **E2EE media keys.** Decrypts `io.element.call.encryption_keys` olm
  to-device messages and feeds the LiveKit frame encryption — the call is
  encrypted end to end, keys never touch the server.
- **Two brains, one phone call.** A small local LLM handles the chat at
  ~1-2s latency; anything that needs *real* tools escalates to a full
  agent session (the bridge) whose verified answer is spoken when it lands.
  Or set `JACK_ROUTE=hermes` and let the full agent carry every turn
  (warm-session turns measured at ~2s).
- **Car-proof audio.** Adaptive VAD (rolling RMS percentile), barge-in
  (talk over the agent and it stops, listens, and transcribes the
  interruption), STT confidence filtering so silence doesn't hallucinate
  pleasantries into the transcript.
- **Crash-honest.** Supervisor restart loop, silent-task exception logging,
  a triage decision tree for "no replies" calls, and utterance dumps for
  forensic STT debugging.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # fill in your values; see the table below
python -m jack          # watcher up: scans rooms, waits for rings
```

Call the voice account from Element X (or any MatrixRTC client). First
utterance is slow while faster-whisper warms its model cache; afterwards
~1-2s per turn on CPU.

### Configuration

Everything is env-driven (`.env` in the working directory) — see
[`.env.example`](.env.example) for the full annotated list. The moving parts:

| Piece | What it is | Where |
|---|---|---|
| Matrix account | the identity that rings and answers | your HS (admin-create it, mint token with `device_id` matching the crypto store) |
| RTC stack | Synapse + livekit-server + lk-jwt | any Element-Call deployment |
| Chat brain | any OpenAI-compatible endpoint (ollama, vLLM, …) | `OPENAI_COMPAT_*` |
| Full agent | [`hermes-agent`](https://github.com/nousresearch/hermes-agent) via `bridge/hermes_bridge.py` | optional, Mac/laptop side |
| STT | faster-whisper (CPU int8 default, GPU turbo if you have VRAM) | bundled |
| TTS | any `/v1/audio/speech` endpoint (Chatterbox recommended) | `JACK_TTS_*` |

> **Deploy tip (paid for in dead calls): land one change per live call.**
> Stack three untested layers and you get a jack with no replies and an
> unfindable root cause.

## The bridge (full-brain escalation)

`bridge/hermes_bridge.py` is a tiny Mac-side HTTP service:

```
POST /task   {task, context[], session_id?} → {ok, reply, seconds, session}
GET  /health → {ok, hermes}
GET  /selftest → runs a REAL `date` through the full path and checks it
```

Each turn becomes one `hermes chat` subprocess. With a `session_id`, turns
ride a persistent named session (`--continue <name> --create-if-missing`):
the prompt-prefix cache stays warm and follow-ups ("did you send it?") see
the whole call. The bridge only reports `ok=true` when the agent subprocess
exited 0 with non-empty text — the jack never speaks an unverified result.

## Operations

```bash
# tests (fixtures are real captured call-member events, anonymized)
python -m pytest tests/ -q

# restart discipline: kill the supervisor loop FIRST, then the watcher
pkill -f "[r]un.sh"; pkill -f "[p]ython -m jack"   # ...then relaunch
pgrep -cf "[p]ython -m jack"                       # must be exactly 1
```

Two watchers sharing one nio crypto store = `database is locked` loops and a
deaf jack. After ANY restart: exactly one watcher process, zero new lock
lines in the log.

## Status / roadmap

Proven live end-to-end (E2EE key transport, adaptive VAD, tool escalation,
barge-in v1): see `docs/` for the protocol notes and debugging trees.
Roadmap: Silero VAD, sentence-streaming TTS, delayed-leave heartbeats
(MSC4140), encrypted hangup summaries, jack-control MCP server.

## Security

Secrets live in `.env` (gitignored); `.env.example` ships keys without
values. See [SECURITY.md](SECURITY.md) for the disclosure policy and the
deployment hardening notes (container egress, ACLs, token scoping).

## Credits

The jack stands on other people's shoulders — see [ATTRIBUTIONS.md](ATTRIBUTIONS.md)
for the full list: dependencies and their authors, the MatrixRTC protocol
work, and the reference implementations we verified against (no copied
code, credited anyway).

## License

Apache-2.0 — see [LICENSE](LICENSE).
