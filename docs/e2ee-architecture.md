# E2EE audio architecture — how the keys and the audio meet

The SFU (LiveKit) relays encrypted audio and never holds a key. Keys travel
exclusively over Matrix (olm to-device events). The two roads only meet inside
the client SDK of each endpoint. This doc is the plain-language map; the
implementation details live in `docs/matrixrtc-e2ee-recipe.md`.

## The two roads

```
                        ┌──────────────────────────┐
        KEY ROAD        │      Matrix homeserver   │
        (olm to-device) │  relays keys, cannot     │
                        │  read them               │
                        └───────────┬──────────────┘
                                    │  io.element.call.encryption_keys /
                                    │  m.rtc.encryption_key (olm-encrypted)
              ┌─────────────────────┴─────────────────────┐
              │                                           │
        ┌─────┴──────┐   media key, olm-encrypted  ┌─────┴──────┐
        │   CALLER   │────────────────────────────▶│    BOT     │
        │ (Element X)│                             │ (device +  │
        └─────┬──────┘                             │  olm store)│
              │                                    └─────┬──────┘
              │ SFrame-locked audio                      │
              ▼                                          │
        ┌──────────────────────────────────────────────────┐
        │        AUDIO ROAD — SFU (LiveKit)                │
        │        relays ciphertext, deaf mailman           │
        └───────────────────────┬──────────────────────────┘
                                │  locked frames
                                ▼
                          ┌───────────┐
                          │    BOT    │
                          └───────────┘
```

## Where everything meets

Inside each endpoint's RTC client (bot process shown; the caller mirrors it):

```
 RTC client (bot)
 ├─ key ring      ← filled by the bot from Matrix olm events (set_key)
 ├─ admission     ← JWT from the RTC auth service (identity + room access)
 ├─ locked audio  ← from the SFU, still ciphertext
 └─ per frame:  lookup sender identity → derive frame key (HKDF) → decrypt
                                        │
                                        ▼
                          clean audio → STT → brain → TTS →
                          re-locked with the BOT's own key → SFU → caller
```

## The three components, one job each

| Component | Job | Never does |
|---|---|---|
| Matrix homeserver | carries keys (olm) | sees SFU traffic |
| RTC auth service (JWT) | mints the bot's admission token + participant identity | touches keys or audio |
| SFU | relays encrypted frames | decrypts, holds keys |

The only E2EE-relevant requirement on the auth service: the participant
identity it mints MUST be the canonical MatrixRTC form (`@user:hs:DEVICE`) —
the key events carry the same string in `member.id`, and `set_key()` hangs the
key on that identity. A drifted identity = ciphertext-noise audio.

## Properties

- **The SFU is structurally locked out**: it routes ciphertext; keys never
  traverse it.
- **Both directions are encrypted**: every participant generates its own
  16-byte media key per call, distributes it via olm, and publishes
  frame-encrypted audio (GCM). The bot's key is installed before its first
  publish — order matters.
- **Server compromise yields audio nothing**: the SFU never sees plaintext or
  key material.

See `docs/matrixrtc-e2ee-recipe.md` for the verified implementation recipe
(key-event shapes, device setup, diagnostic sequence).
