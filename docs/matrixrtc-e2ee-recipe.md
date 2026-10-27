# MatrixRTC E2EE key-transport recipe (verified working)

Verified end-to-end: bot receives Element X media keys, installs into livekit KeyProvider,
caller audio decrypts and transcribes word-perfect. This file is the deep recipe; SKILL.md
has the summary rules.

## Setup (one-time per bot device)
1. nio[e2e] on py3.13+ uses vodozemac bindings — no libolm. Import check:
   `from nio.crypto import Olm, OlmMachine; import vodozemac`.
2. Client: `AsyncClient(homeserver, user_id, device_id=DEV, store_path=DIR)`.
   `restore_login(user, dev, token)` then `load_store()` — SYNC method, no await.
3. The access token MUST belong to the same device_id as the olm store. Symptom of mismatch:
   `keys_upload()` returns KeysUploadError "Provided device_id in device_keys does not match
   that of the authenticated user device" — silently swallowed if you only check for exceptions.
   Verify: `GET /account/whoami` → device_id must equal the store's device. Fix: admin
   reset_password (logout_devices=false) + `POST /login` with explicit device_id → new token.
4. OTKs are NOT auto-generated: `client.olm.account.generate_one_time_keys(50)` before
   `keys_upload()` (nio 0.26 name; there is no upload_keys()). Verify with sync's
   `device_one_time_keys_count` — 0 means Element cannot open olm sessions to you.
5. Trust: `await client.keys_query()` (raises LocalProtocolError "No key query required" when
   nothing new — catch it), then `client.verify_device(dev)` for every room member device,
   at startup AND at answer time. Outbound encryption to unverified devices raises
   OlmUnverifiedDeviceError (kills call summaries).

## Key events
- olm-decrypted to-device payloads: `io.element.call.encryption_keys` (Element),
  `m.rtc.encryption_key` (standardized), plus `m.room_key` (megolm session sharing).
- Live shape (Element X, call in an encrypted room):
  {"keys": {"index": 1, "key": "<b64 16 bytes>"},          # DICT, not a list
   "member": {"claimed_device_id": "TRNBXOZARK",
              "id": "@caller:matrix.hs.example:TRNBXOZARK"},  # canonical livekit identity
   "room_id": "...", "sent_ts": ..., "session": {"application": "m.call", ...}}
- Identity for `key_provider.set_key` MUST be `member.id` — it matches the livekit participant
  identity minted from the membership event; guessing sender:device breaks decryption.
- Hook: wrap nio machine `_handle_olm_event` via types.MethodType; log every decrypted olm
  payload type while debugging — the shape is the whole game.

## livekit wiring (receive-only E2EE, publish plaintext)
```python
opts = rtc.E2EEOptions(
    key_provider_options=rtc.KeyProviderOptions(
        shared_key=None, ratchet_salt=b"LKFrameEncryptionKey",
        ratchet_window_size=10, key_ring_size=256, failure_tolerance=-1,
        key_derivation_function=rtc.KeyDerivationFunction.HKDF),
    encryption_type=rtc.EncryptionType.NONE)   # default is GCM — leaving it breaks plaintext publish
room_opts = rtc.RoomOptions(auto_subscribe=True, e2ee=opts, encryption=opts)
# after connect: room.e2ee_manager.key_provider.set_key(identity, raw_bytes, index)
```
- Set keys whenever they arrive (before or after subscribe — key_ring caches per identity).
- Enum check: `rtc.EncryptionType.NONE == 0`, `Name(1) == "GCM"` — verify ints, don't trust memory.

## Diagnostic sequence (when audio is gated noise)
1. Spectrogram the dump — ciphertext shows gated broadband bursts, zero formants.
2. `grep device_one_time_keys_count` from a raw sync — 0 OTKs = no olm sessions to you.
3. `whoami` device vs store device — mismatch = silent upload failures.
4. Look for "Successfully decrypted olm message" + dump payload types — keys arrived but
   shape-parsing failed is the usual last-mile bug.
5. Homeserver auto-encryption: preset private_chat, trusted_private_chat AND preset-less rooms
   all get m.room.encryption on this Synapse — there is no unencrypted escape room; implement
   key transport instead.
