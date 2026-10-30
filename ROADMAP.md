# Roadmap & open tasks

Status legend: `[ ]` open, `[~]` partially done, `[x]` done.

## Done (2026-10-27 release)

- [x] Outbound + inbound E2EE (AES-GCM frames, olm key distribution) — live-validated full-duplex
- [x] E2EE enforcement: call refused rather than plaintext fallback
- [x] Default-deny caller allowlist + invite gating
- [x] Media-key receiver bound per call; keys/bindings never survive a hangup
- [x] Caller-audio identity check (only the authorized participant is subscribed)
- [x] Olm hook delegates to nio's original handler (Megolm room keys unaffected)
- [x] Bootstrap: `.env` loading, whoami token/device verification, crypto-store creation (0700)
- [x] Agent-agnostic pass: neutral persona/voice/model defaults, bridge is a worked example
- [x] CI: pytest 3.11–3.13 + secrets scan, green

## Open — robustness

- [x] Strict sender_key verification (2026-10-30): unknown sender devices no
      longer fail open — the key event is stashed, a keys_query for the sender
      is scheduled, and the event is re-verified once the device store fills;
      still-unknown after the query is dropped (fail-closed). One inflight
      query per sender covers all events stashed during it. Deployed to
      /opt/jack and live.
- [ ] Bridge authentication: the `/task` endpoint is unauthenticated by design
      (LAN-only); add a shared-token option for exposure beyond the LAN
- [ ] Recording retention: sweep runs on write; consider a periodic janitor task
- [ ] Group-call support: watcher answers 1:1 DMs only; membership parsing,
      multi-speaker diarization and per-speaker keys are undesigned
- [ ] Reconnect/backoff: long homeserver outages need an exponential retry
      around the sync loop (currently simple retry)

## Open — features

- [ ] Outbound calls: let the agent *place* a call to a user (ring, wait, talk)
      — full feasibility + architecture study in `OUTBOUND-CALLS-ARCH.md`
      (2026-10-28): MatrixRTC notification-profile route grades HIGH on the
      pinned deployment; legacy-to-Element-X grades LOW; MVP is 6 gated tasks
- [ ] Video: publish an avatar video track (pair with a renderer; effigy-style
      VRM stage on a GPU host) — the three-repo "connector" story — design in
      `docs/AVATAR-VIDEO-ARCH.md` (2026-10-28): reuse a three-vrm renderer with
      restrained "presence" animation; canvas.captureStream → bridge media path
- [ ] Voice presets: per-language TTS voice mapping beyond `JACK_TTS_VOICE`
- [ ] Live interop CI: an optional job that runs a real call against a test
      homeserver (needs hosted Synapse+LiveKit in CI)
- [ ] Metrics: per-call Prometheus counters (turns, latency, STT/TTS timing)

## Open — community

- [ ] Scan issues/bugs/feature requests in the official Hermes repo
      (nousresearch/hermes-agent) for anyone needing agent voice calls on
      Matrix — where relevant, point them to bellmouth and offer the bridge
      as the worked example; capture recurring asks back into the roadmap

## Open — packaging

- [ ] Docs site / screenshots (the README architecture diagram deserves render)
- [ ] Example systemd unit + docker-compose for one-command deployment
- [ ] `pyproject.toml` + PyPI package (currently run-from-checkout)
