# Attributions

the jack was built on the work of many open-source projects and
communities. This file credits the ones whose work made it possible —
as dependencies, as protocol references, and as prior art. If we missed
you, please open an issue; unintentional non-attribution is a bug like
any other.

## Runtime dependencies

| Project | What we use it for | License | By |
|---|---|---|---|
| [matrix-nio](https://github.com/matrix-org/matrix-nio) | Matrix client: sync, rooms, **E2EE (olm/vodozemac)** key transport | ISC | matrix.org / community |
| [livekit Python SDK](https://github.com/livekit/python-sdks) | RTC: joining the SFU, publishing/subscribing tracks | Apache-2.0 | [LiveKit](https://livekit.io/) |
| [aiohttp](https://github.com/aio-libs/aiohttp) | async HTTP (watcher, adapters, bridge) | Apache-2.0 | aio-libs |
| [PyJWT](https://github.com/jpadilla/pyjwt) | participant-token minting (HS256) | MIT | José Padilla |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | speech-to-text (CTranslate2 reimplementation of OpenAI's Whisper) | MIT | [SYSTRAN](https://www.systran.net/) |
| [Whisper](https://github.com/openai/whisper) | the model family behind the above | MIT | OpenAI |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | optional server-mode STT backend | MIT | Georgi Gerganov & contributors |

Models that answered during development: NVIDIA **Bolt/Nemotron E4B**
(NVIDIA open model license, served via [Ollama](https://ollama.com/)),
**GLM** (Z.ai), and **Chatterbox TTS** (MIT, [Resemble AI](https://www.resemble.ai/))
for the voice. The example bridge runs [hermes-agent](https://github.com/NousResearch/hermes-agent)
(MIT, [Nous Research](https://nousresearch.com/)).

## Protocol work we build on

- **MatrixRTC** — [MSC3401](https://github.com/matrix-org/matrix-spec-proposals/pull/3401)
  (native VoIP/RTC), the `org.matrix.msc3401.call.member` state-event
  contract, per-device membership ([MSC4148](https://github.com/matrix-org/matrix-spec-proposals/pull/4148))
  and delayed leaves ([MSC4140](https://github.com/matrix-org/matrix-spec-proposals/pull/4140)).
- **Element Call / Element X** — the reference client whose real calls
  shaped every behavior in `jack/events.py` (and whose anonymized
  membership events are the test fixtures).
- [lk-jwt-service](https://github.com/element-hq/lk-jwt-service) (AGPL-3,
  element-hq) — the MatrixRTC authorization service. **No source code was
  copied**; the room-name derivation in `jack/events.py` was verified
  against its v0.5 source and its own comments. We interoperate, we don't
  link — but the derivation credit is theirs.
- [LiveKit](https://livekit.io/) server — the SFU itself (Apache-2.0).

## Prior art & inspiration

- The **two-brain architecture** (small chat model + escalation to a full
  tool-using agent) was built from scratch for this project, but the
  pattern — voice front-end, real agent behind it — is old and good.
- **Chatterbox**, **faster-whisper** and the whole local-AI voice scene
  made the "no cloud in the loop" constraint achievable on modest
  hardware.

## Test fixtures

`tests/fixtures/` are **anonymized captures of real Element X calls** —
real protocol shapes, replaced identities. Thanks to everyone who let a
robot answer their phone call during development.

## License

the jack itself is Apache-2.0 (see [LICENSE](LICENSE)). The table above
is a compatibility summary, not legal advice — each project's own
LICENSE governs it.
