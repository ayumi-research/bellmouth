# Security Policy

## Reporting a vulnerability

Open a private security advisory via GitHub (Security → Report a
vulnerability) or contact the maintainers directly. Please include a
reproduction and the affected commit.

## Deployment hardening (read before exposing anything)

The jack is a **voice agent with tool access** — treat it like a remote
shell with a microphone:

- **Secrets**: `.env` only, never committed. The Matrix access token must
  belong to the same `device_id` as the olm crypto store (mismatched tokens
  fail key upload *silently* — see the protocol notes in `docs/`).
- **Token scoping**: mint the voice account token with the least device
  scope you can; the LiveKit participant token is least-privilege
  (join + publish/subscribe in one room, minted per call).
- **Container egress**: if you run the jack in Docker on a trusted LAN,
  consider an iptables/nftables egress allowlist for the solver/brain
  hosts — the default posture here is "brain on localhost/LAN, no
  inbound ports".
- **Caller allowlist**: `JACK_ALLOWED_USERS` (comma-separated full user ids).
  **Default-deny**: with no allowlist configured the jack answers NOBODY and
  refuses all invites; set the variable to exactly the users who may spawn
  sessions. The watcher also only ever answers 1:1 DMs; group rooms are
  opt-in future work.
- **The bridge is powerful**: `bridge/hermes_bridge.py` executes agent
  turns with full tool access on its host. Do NOT expose :8620 beyond
  your LAN/tailnet; there is no auth on the endpoint by design — it is a
  loopback-class service. Put it behind an ACL (Tailscale, firewall) if
  the bridge host and the jack live on different machines.
- **lk-jwt / LiveKit**: version-pin your RTC stack and review its
  deployment guidance; the jack trusts the SFU it is configured for.

## Secrets history

This repository was cut fresh from a private working repo; the git history
was scanned for live credentials before publishing. If you find anything
that looks like a live secret, please report it per the channel above.
