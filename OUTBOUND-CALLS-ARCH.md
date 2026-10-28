# Outbound Matrix calls: feasibility and architecture

Assessment: 2026-10-28. Code review and upstream research; no outbound interoperability tests performed. Grades describe engineering feasibility, not measured ringing reliability.

**Verdict**

| Signaling path / target | Grade | Judgment |
| --- | --- | --- |
| Existing MatrixRTC/LiveKit deployment, pinned Element X/Web builds | HIGH | Best reuse; prove outbound ringing first. |
| MatrixRTC across arbitrary current Element deployments | MED | Protocol revisions, push delivery and SFU authorization need interoperability work. |
| Legacy `m.call.*`, Element Web with legacy enabled | MED | Feasible protocol, but a new media backend here. |
| Legacy `m.call.*` as the route to Element X | LOW | Not a viable compatibility strategy. |
| “MSC4144 per-call state” | LOW | Incorrect premise: [MSC4144 concerns per-message profiles](https://github.com/matrix-org/matrix-spec-proposals/pull/4144). |

Build a small, opt-in MatrixRTC dialer for existing encrypted DMs on the tested deployment. Gate further investment on foreground and locked-phone ringing tests. This is worth building for human “call me when ready” hooks and explicitly requested reminders, where hands-free interaction adds value. Agent-to-agent voice is technically possible but usually inferior to structured messages: extra latency, transcription errors and no useful machine interface benefit. Do not invest in universal telephone-like reliability or a second legacy stack without demonstrated demand.

**What this repository actually implements**

The brief's SDP-answer premise is false. `bridge/agent_bridge.py` only exposes Hermes task/health/selftest HTTP endpoints; `hermes_bridge.py` is a compatibility shim. Calling lives in `jack/`.

`watcher.py:handle_member_state()` detects `org.matrix.msc3401.call.member` joins, checks the allowlist and two-member DM, then `_answer()` mints a LiveKit token. `_run_call()` connects, installs/distributes frame keys, publishes tracks and its own membership. There is no legacy invite handler, SDP parser, candidate gatherer or direct SRTP implementation to invert. LiveKit's SDK owns transport negotiation. ROADMAP's completed “outbound E2EE” means outgoing audio encryption, not dialing.

`events.py:livekit_room_alias()` derives the SFU room; `_answer()` uses it despite an outdated module comment implying the advertised alias is the actual SFU room. `mint.py` uses local SFU secrets in production; its optional `mint_via_lkjwt()` helper is not wired into the watcher and is not evidence of current authorization interoperability.

**Signaling and encryption**

Legacy calling uses timeline `m.call.invite` with an SDP offer, then `m.call.answer`, `m.call.candidates` and hangup/reject events. The [current JS call implementation](https://github.com/matrix-org/matrix-js-sdk/blob/develop/src/webrtc/call.ts) uses version `"1"`, call/party IDs, invite expiry and answer selection; `placeVoiceCall()` needs a real WebRTC peer connection and media source. A bot can implement this as an ordinary joined device, but sending an invite alone supplies no media implementation. In encrypted rooms, room signaling is carried inside Megolm `m.room.encrypted`; Olm distributes Megolm session keys. SDP fingerprints authenticate DTLS peers; DTLS-SRTP secures direct/ TURN-relayed media. A headless WebRTC library would supply ICE, certificates, codecs and SRTP. See the [VoIP and E2EE specification](https://spec.matrix.org/latest/client-server-api/#voice-over-ip).

Element Web now defaults to Element Call, with legacy availability configurable through `element_call.use_exclusively` ([official release notes](https://docs.element.io/latest/element-server-suite-pro/release-notes/)). Element X's calling route is MatrixRTC; legacy support must not be inferred from Web's SDK ([Element's compatibility explanation](https://element.io/blog/mas-migration-unleashes-element-x-on-matrix-org/)).

MatrixRTC separates membership from ringing. [MSC4143](https://github.com/matrix-org/matrix-spec-proposals/pull/4143) defines sessions; MSC4075 covers invites/notifications. The [SDK event constants](https://github.com/matrix-org/matrix-js-sdk/blob/develop/src/@types/event.ts) currently retain `org.matrix.msc4075.rtc.notification`, older `org.matrix.msc4075.call.notify`, and `org.matrix.msc4310.rtc.decline`. MSC4310 was [subsumed into MSC4075](https://github.com/matrix-org/matrix-spec-proposals/pull/4310). Do not assume bare stable names work everywhere.

The [SDK notification sender](https://github.com/matrix-org/matrix-js-sdk/blob/develop/src/matrixrtc/MatrixRTCSession.ts) publishes membership first, references its event ID, and supplies slot, mentions, timestamp and lifetime; it prefers sticky events with a regular-event fallback. Its [receiver schema](https://github.com/matrix-org/matrix-js-sdk/blob/develop/src/matrixrtc/types.ts) already deprecates `notification_type` and intent fields retained by the sender. Older profiles use `notification_type: "ring"`. Therefore pin the complete client-compatible profile, including slot/membership/notification delivery, rather than synthesizing a timeless “ring” JSON. Membership alone is not a ringing guarantee.

On bellmouth's path, no SDP offer travels to the callee over Matrix: each endpoint negotiates with LiveKit. DTLS-SRTP protects endpoint–SFU hops; additional AES-GCM frame encryption provides endpoint-to-endpoint confidentiality through the SFU. `e2ee_send.py` sends media keys through Olm to-device messages; `e2ee_keys.py` installs participant keys. This is distinct from Megolm room encryption. [MSC4195](https://github.com/matrix-org/matrix-spec-proposals/pull/4195) now includes identity derivation and token/room changes, so the repo's fixed room hash and `<mxid>:<device>` assumptions need version pinning.

Prior art exists: [matrix-voip-agent](https://github.com/AEON-7/matrix-voip-agent) documents authenticated outbound `/call` and `/hangup` APIs. This is useful control-plane precedent, not proof of Element X interoperability. Element Call itself is the relevant SFU reference: a two-person call still uses MatrixRTC/LiveKit; conference topology does not require a legacy 1:1 exception.

**Recommended architecture**

```text
Hermes tool / scheduled hook
  -> authenticated local dial(room, user, purpose, request_id)
  -> existing watcher: authorize, reserve single-call slot, prepare E2EE/SFU
  -> Matrix: publish own membership + compatible targeted notification
  -> callee client: ring -> accept -> publish device membership
  -> watcher: validate device/session, exchange keys, confirm media readiness
  <-> LiveKit SFU <-> callee (frame-encrypted audio)
  -> CallMedia PCM/VAD -> STT -> adapter -> Hermes bridge
  <- CallMedia audio publication <- TTS <- adapter response
```

Use `PREPARING → INVITING → CONNECTING → CONNECTED → ENDED`, with rejection, timeout, cancellation and failure outcomes. An accepted send is not confirmed ringing; membership is not confirmed audible media. Keep one sync/crypto-store owner. Delay greetings until acceptance and encryption readiness.

**MVP tasks, smallest first; each has a separate acceptance test**

1. Capture outbound events from pinned Element X and Web builds in the existing deployment. Extend `tests/fixtures/` and `tests/test_events.py` around `jack/events.py`; verify notification correlation, expiry and actual ringing before expanding scope.
2. Extract membership construction from `watcher.py:_publish_membership()`, return/check the event ID, and add a profile-specific notification builder. Test payloads against captures, including failure to publish membership; never notify after failure.
3. Add lifecycle state around `CallState` and `handle_member_state()`. Currently active calls ignore joins, which would discard outbound acceptance. Test accept, decline, timeout, duplicate events, simultaneous inbound/outbound and late acceptance using fake time.
4. Reuse `mint.py`, `media.py`, `e2ee_send.py` and `e2ee_keys.py` for one manual outbound echo call. Discover the answering device from authenticated membership, bind `expected_remote_identity`, resend keys after its join, and handle tracks arriving before identity selection. Test two-way encrypted audio and missing-key refusal; current `distribute()>0` only counts sends, not recipient decryption.
5. Add cancellation and guaranteed cleanup of tasks, receiver, keys, membership and active state. Test repeated calls and process loss; implement deployment-compatible [MSC4140 delayed leave](https://github.com/element-hq/synapse/issues/20254) to prevent ghost calls.
6. Expose authenticated dial/status/hangup to the agent, with recipient allowlist, idempotency and rate limits. Reuse `stt.py`, `adapters/base.py`, `adapters/openai_compat.py` and the existing bridge unchanged where possible. Test “call me when ready,” greeting-after-acceptance and truthful no-answer reporting.

**Risks and release gates**

Record exact client/server versions for X iOS, X Android, Web MatrixRTC and Web legacy-enabled. Test foreground, background/locked, offline, DND, multi-device acceptance, decline and federated recipients. None has outbound validation here. Pinning is essential: newer sticky memberships/slots are not parsed by this watcher.

E2EE gaps include automatic device verification, unknown sender-key fail-open, missing modern `member_id` identity handling, and one-shot key distribution. Require authenticated device binding and per-call cleanup; reject plaintext configuration for outbound production. Validate key rotation and reconnects.

Never race legacy and MatrixRTC invites: duplicate ringing and two accepted media sessions are possible. Use an explicit tested recipient profile; absent a reliable capability signal, return unsupported rather than guessing. Push policy, expired notifications, state permissions, SFU discovery/authorization and network reachability remain independent failure modes.
