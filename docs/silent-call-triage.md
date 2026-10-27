# Silent-call triage — 'no replies' decision tree

Run these IN ORDER on `/tmp/jack.log` after a failed call. Each step rules out one layer;
do not skip ahead — the layers mask each other.

## Step 0 — exactly one watcher?
`pgrep -cf 'python -m jack'` must be 1. Count > 1 = duplicate watchers sharing one nio session DB:
'sync error: database is locked' loops and a DEAF jack (both processes stall, zero replies).
Kill the run.sh supervisor loops FIRST (they outlive their python children and respawn within
seconds), then the watchers, then start exactly one. Verify the count in a second ssh session.

## Step 1 — fresh lock errors?
`grep 'database is locked' /tmp/jack.log` and compare against the last restart timestamp.
Locks AFTER the restart = another process holds the session DB (stale supervisor loop, a stray
bootstrap script, a second watcher). Fix the holder before touching anything else.

## Step 2 — did we answer?
`grep ANSWERING /tmp/jack.log | tail`. No ANSWERING line = detection problem (go to the
'no answer' ladder in integration-debugging.md). ANSWERING present → continue.

## Step 3 — did the media keys arrive at call START?
Compare each `media key installed` timestamp against its call's `ANSWERING` timestamp.
- Keys within ~1-5s of ANSWERING = key transport healthy; skip to step 4.
- Keys only at call END, or missing entirely = crypto identity divergence: Element X still
  encrypts to-device key messages to a stale JACK01 identity. Re-bootstrap the device
  (references/crypto-rebootstrap.md) AND have the user fully log out + back into Element X —
  a swipe-restart does NOT clear its device-key cache. Log signature of the stale-cache state:
  `Olm event doesn't contain ciphertext for our key`.

## Step 4 — was inbound audio non-silent?
`reader pulse: ... last rms 0` across a whole call = every frame decoded to zeros = silent or
undecryptable track (crypto, see step 3) — NOT a VAD bug. rms peaks but still 'endpoint without
enough speech (noise only), dropped' = threshold/speech-count tuning, not crypto.

## Step 5 — were utterances captured but never transcribed?
`ls -la /tmp/jack_utt_*.wav` — dumps newer than the failed call with NO matching `caller said`
line = the STT layer, not the audio path. stt.py picks its backend from `JACK_STT_BACKEND` and
DEFAULTS to `whisper_cpp`, whose server is a husk on the sidecar — every transcript silently
returns empty. Fix: `JACK_STT_BACKEND=faster` in /opt/jack/.env. Verify offline first:
`cd /opt/jack && JACK_STT_BACKEND=faster .venv/bin/python -c` transcribing a fresh dump via
`st._transcribe_sync(raw[44:], src_rate=48000)` — a real transcript proves the path before any
restart.

## Step 6 — transcript but no spoken reply?
Check the adapter chain in order: GLM endpoint RTT from the sidecar (`curl` the chat completion,
watch `content` vs `reasoning_content` and the max_tokens budget), task-bridge health if
`JACK_TASK_BRIDGE_URL` is set (`Connection refused` = bridge down on the Mac — chat turns still
work, task turns fail honest), then the TTS pump (`pumped N bytes -> N audio frames` lines).

## Step 7 — reply pumped but caller hears nothing?
TTS publish leg: check `tts ... playing` + `enqueued` logs, then the audio source/track state.
This is the v0.1-era 'audio in SFU, caller deaf' family — verify the LiveKit participant identity
matches the membership `membershipID` suffix before anything else.

## Meta-rules
- One layer per live test. After ANY restart: exactly one watcher, imports OK, zero NEW lock lines.
- Grep by logger name (`jack.(watcher|media|sink|e2ee)`), not by keyword — crash lines don't say 'error'.
- The utterance dumps are ground truth: when transcripts and logs disagree, re-mine the wav.
