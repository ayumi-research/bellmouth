"""call-watcher — the Matrix side of the jack (§4.1).

matrix-nio AsyncClient on the agent's voice account. Sync loop scans DMs for
org.matrix.msc3401.call.member state events; on a LIVEKIT join from an
allowlisted caller in a 1:1 DM: mint -> join via CallMedia + AgentAdapter.
On leave / caller-vanish: end session + hangup sink.

Secrets/config via env only. No tokens or credentialed URLs in logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from nio import AsyncClient, InviteEvent, SyncResponse

from .adapters.base import CallEndReason, CallSession, CallSummary
from .adapters.echo import EchoAdapter
from .adapters.openai_compat import OpenAICompatAdapter
from .events import CALL_MEMBER_TYPE, CallMemberState, livekit_room_alias, parse_call_member_event, select_focus
from . import e2ee_keys
from .media import CallMedia
from .mint import mint_participant_token
from .sink import post_call_summary

log = logging.getLogger("jack.watcher")


def load_config() -> dict:
    return {
        "homeserver": os.environ["MATRIX_HOMESERVER_URL"],
        "user_id": os.environ["MATRIX_VOICE_ACCOUNT"],           # @mei_voice:matrix.ayumi.town
        "access_token": os.environ["MATRIX_VOICE_ACCESS_TOKEN"],
        "livekit_url": os.environ.get("LIVEKIT_URL", "wss://matrix.ayumi.town/lk"),
        "livekit_key": os.environ.get("LIVEKIT_API_KEY", ""),
        "livekit_secret": os.environ.get("LIVEKIT_API_SECRET", ""),
        "livekit_service_url": os.environ.get("LIVEKIT_JWT_SERVICE_URL", "https://matrix.ayumi.town/livekit-jwt"),
        "device_id": os.environ.get("JACK_DEVICE_ID", "JACK01"),
        "allowlist_callers": [u.strip() for u in os.environ.get("JACK_ALLOWLIST_CALLERS", "").split(",") if u.strip()],
        "allowlist_rooms": [r.strip() for r in os.environ.get("JACK_ALLOWLIST_ROOMS", "").split(",") if r.strip()],
        "adapter": os.environ.get("JACK_ADAPTER", "echo"),
        "adapter_bot_decline_text": os.environ.get("JACK_BUSY_TEXT", "I'm on another call right now — try again in a minute."),
        "retention_s": int(os.environ.get("JACK_RETENTION", "0")),
    }


def make_adapter(kind: str):
    if kind == "echo":
        return EchoAdapter()
    if kind == "openai_compat":
        return OpenAICompatAdapter()
    raise ValueError(f"unknown adapter {kind}")


class CallState:
    """One active call (single-call policy)."""

    def __init__(self, room_id: str, caller: str, media: CallMedia, adapter):
        self.room_id = room_id
        self.caller = caller
        self.media = media
        self.adapter = adapter
        self.session: Optional[CallSession] = None
        self.task: Optional[asyncio.Task] = None


class CallWatcher:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = AsyncClient(
            cfg["homeserver"], cfg["user_id"],
            device_id=cfg["device_id"],
            store_path=os.environ.get("JACK_CRYPTO_STORE", "/opt/jack/crypto_store"))
        self.client.access_token = cfg["access_token"]
        self.active: Optional[CallState] = None
        self._seen_events: set = set()  # (room_id, event_id) dedup
        self._room_members: dict = {}   # room_id -> set of joined MXIDs (from m.room.member)
        self._caller_left = False       # set when active caller publishes an empty membership
        self.start_ms = int(time.time() * 1000)

    # ---- filtering (§6.1: allowlist before answer, default deny) ----
    def _allowed(self, room_id: str, sender: str) -> bool:
        if self.cfg["allowlist_callers"] and sender not in self.cfg["allowlist_callers"]:
            return False
        if self.cfg["allowlist_rooms"] and room_id not in self.cfg["allowlist_rooms"]:
            return False
        return True

    async def _is_dm_with(self, room_id: str, sender: str) -> bool:
        # 1:1-DM-only default is a security control (§6.1)
        # primary source: member cache built from m.room.member state in syncs
        members = self._room_members.get(room_id)
        if not members or len(members) != 2:
            members = await self._fetch_members_raw(room_id)  # authoritative on doubt
        if not members:
            log.warning("no member list for %s...; denying by default", room_id[:14])
            return False
        return len(members) == 2 and sender in members and self.cfg["user_id"] in members

    async def _fetch_members_raw(self, room_id: str) -> set:
        """nio's JoinedMembers validation breaks on Synapse's shape — raw HTTP."""
        import aiohttp, json as _json
        url = f"{self.cfg['homeserver'].rstrip('/')}/_matrix/client/v3/rooms/{room_id}/joined_members"
        headers = {"Authorization": f"Bearer {self.cfg['access_token']}"}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers=headers) as r:
                    data = await r.json()
            joined = set((data.get("joined") or {}).keys())
            if not joined:
                log.warning("joined_members empty for %s...: HTTP %s %s", room_id[:14], r.status, str(data)[:150])
            self._room_members[room_id] = joined
            return joined
        except Exception:
            log.exception("joined_members fetch failed for %s...", room_id[:14])
            return set()

    def _track_member_event(self, room_id: str, src: dict) -> None:
        if src.get("type") != "m.room.member":
            return
        members = self._room_members.setdefault(room_id, set())
        content = src.get("content") or {}
        if content.get("membership") == "join":
            members.add(src.get("sender"))
        else:
            members.discard(src.get("sender"))

    # ---- call lifecycle ----
    async def _answer(self, st: CallMemberState) -> None:
        # service-URL sanity from the event, then the REAL SFU name (lk-jwt derivation)
        if select_focus(st, self.cfg.get("livekit_service_url")) is None:
            log.info("join event without usable livekit focus; ignoring (sender=%s room=%s...)",
                     st.sender[:20], st.room_id[:14])
            return
        room = livekit_room_alias(st.room_id)
        minted = mint_participant_token(
            api_key=self.cfg["livekit_key"],
            api_secret=self.cfg["livekit_secret"],
            room=room,
            identity=f'{self.cfg["user_id"]}:{self.cfg["device_id"]}',  # MUST match membershipID suffix or Element drops our audio
            ttl_seconds=2 * 3600,
            name="Agent",
        )
        media = CallMedia(self.cfg["livekit_url"], minted.token, self.cfg["user_id"])
        adapter = make_adapter(self.cfg["adapter"])
        state = CallState(st.room_id, st.sender, media, adapter)
        self.active = state
        state.session = CallSession(
            room_id=st.room_id, caller=st.sender, livekit_room=room,
            livekit_url=self.cfg["livekit_url"], token=minted.token,
            identity=f'{self.cfg["user_id"]}:{self.cfg["device_id"]}',  # MUST match membershipID suffix or Element drops our audio
        )
        await adapter.start(state.session)
        state.task = asyncio.ensure_future(self._run_call(state))
        log.info("ANSWERING call: room=%s... caller=%s adapter=%s lkroom=%s",
                 st.room_id[:14], st.sender[:20], self.cfg["adapter"], room[:24])

    async def _run_call(self, state: CallState) -> None:
        st = state
        try:
            await st.media.connect(room_options=e2ee_keys.e2ee_room_options())
            if self.receiver is not None:
                self.receiver.matrix_room_id = st.room_id
                st.media.receiver = self.receiver
                self.receiver.bind_room(st.media.room)
            await st.media.publish_dummy_video()
            await st.media.publish_audio_track()
            await self._publish_membership(st.room_id)  # the actual "answer" signal for Element X

            async def handle_utterance(pcm: bytes):
                """Caller finished a phrase: STT -> adapter turn -> TTS reply."""
                from . import stt
                try:
                    rate = st.media._remote_rate or 48000
                    text = await stt.transcribe(pcm, src_rate=rate)
                    log.info("caller said: %r", text[:120])
                    if text:
                        await st.adapter.on_transcript(text)
                except Exception:
                    log.exception("utterance handling failed")

            st.media.on_utterance(handle_utterance)

            async def utterance_pump():
                stream = st.adapter.transcript_stream()
                async for utt in stream:
                    log.info("agent utterance (%s chars)", len(utt.text))
                    try:
                        await st.media.speak(utt.text)  # TTS render time doubles as grace
                    except Exception:
                        log.exception("speak failed")

            pump = asyncio.ensure_future(utterance_pump())

            # wait for caller leave or adapter end
            while True:
                await asyncio.sleep(1)
                if not self.room_alive(st):
                    break
            await st.adapter.end(CallEndReason.CALLER_LEFT)
            pump.cancel()
        except Exception as e:
            log.exception("call crashed: %s", e)
            try:
                await st.adapter.end(CallEndReason.ADAPTER_CRASH)
            except Exception:
                pass
        finally:
            await st.media.close()
            try:
                await self._publish_membership(st.room_id, joined=False)
            except Exception:
                pass

    async def _publish_membership(self, room_id: str, joined: bool = True) -> None:
        """Publish our own m.call membership state — Element X shows 'connected'
        only once the callee's device has a membership event in the room (§4.1)."""
        import time as _time
        dev = self.cfg.get("device_id", "JACK01")
        me = self.cfg["user_id"]
        content = {
            "call_id": "",
            "application": "m.call",
            "scope": "m.room",
            "device_id": dev,
            "membershipID": f"{me}:{dev}",
            "m.call.intent": "audio",
            "focus_active": {"type": "livekit", "focus_selection": "multi_sfu"},
            "foci_preferred": [{
                "type": "livekit",
                "livekit_alias": room_id,
                "livekit_service_url": self.cfg["livekit_service_url"],
            }],
            "created_ts": int(_time.time() * 1000),
            "expires": 14400000,
        }
        if not joined:
            content = {}
        state_key = f"_{me}_{dev}_m.call"  # mirrors Element: _@user_DEVICE_m.call
        resp = await self.client.room_put_state(
            room_id, CALL_MEMBER_TYPE, content, state_key=state_key)
        if getattr(resp, "event_id", None):
            log.info("membership %s published (%s)", "join" if joined else "leave", getattr(resp, 'event_id', '')[:12])

    def room_alive(self, st: CallState) -> bool:
        # v0.1: refined in lifecycle.py (delayed-leave + membership re-check)
        return not self._caller_left

    async def _end_active(self, summary: CallSummary) -> None:
        try:
            await post_call_summary(self.client, summary,
                                    text_env=self.cfg["adapter_bot_decline_text"])
        except Exception:
            log.exception("hangup sink failed")
        self.active = None

    # ---- sync handling ----
    async def handle_member_state(self, ev: dict) -> None:
        st = parse_call_member_event(ev)
        if st is None:
            return
        self._track_member_event(st.room_id, ev)
        if int(ev.get("origin_server_ts") or 0) and int(ev["origin_server_ts"]) < self.start_ms - 60_000:
            return  # stale backfill (watcher started after the call ended)
        key = (st.room_id, ev.get("event_id") or ev.get("origin_server_ts"))
        if key in self._seen_events:
            return
        self._seen_events.add(key)

        if st.joined and st.application == "m.call" and st.sender != self.cfg["user_id"]:
            if self.active is not None:
                return  # polite decline policy (§4.1 concurrency)
            if not self._allowed(st.room_id, st.sender):
                log.info("caller not allowlisted: %s...", st.sender[:16])
                return
            if not await self._is_dm_with(st.room_id, st.sender):
                log.info("not a 1:1 DM; skipping (group opt-in pending)")
                return
            self._caller_left = False
            try:
                try:
                    await self.client.keys_query()
                except Exception:
                    pass
                for uid, dm in self.client.room_devices(st.room_id).items():
                    for dev in dm.values():
                        self.client.verify_device(dev)
            except Exception:
                log.exception("device trust refresh failed")
            sk = ev.get("state_key", "")
            self.active_caller_device = sk.rsplit("_", 2)[-2] if sk.count("_") >= 2 else ""
            await self._answer(st)
        elif not st.joined and self.active and st.room_id == self.active.room_id:
            if st.sender == self.active.caller:
                self._caller_left = True
                summary = await self.active.adapter.end(CallEndReason.CALLER_LEFT)
                await self._end_active(summary)

    async def _crypto_init(self) -> None:
        for noisy in ("nio.rooms", "nio.client", "nio.crypto.log", "peewee"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        """Restore olm device for @mei_voice and upload keys once."""
        try:
            self.client.restore_login(
                self.cfg["user_id"], self.cfg["device_id"], self.cfg["access_token"])
            loaded = self.client.load_store()  # sync in nio 0.26
            log.info("crypto store loaded=%s device=%s", bool(loaded), self.cfg["device_id"])
            self.receiver = e2ee_keys.MediaKeyReceiver(self.cfg.get("matrix_room_id", ""))
            e2ee_keys.make_olm_hook(self.client, self.receiver)
            try:
                try:
                    await self.client.keys_query()  # raises if nothing new
                except Exception:
                    pass
                devs = self.client.device_store
                users = list(getattr(devs, "users", []) or [])
                n = 0
                for uid in users:
                    dm = devs[uid] if not isinstance(devs.users, dict) else devs.users[uid]
                    for devid, dev in dm.items():
                        try:
                            self.client.verify_device(dev); n += 1
                        except Exception:
                            pass
                log.info("devices queried+verified: %d", n)
            except Exception:
                log.exception("keys_query/verify failed")
            try:
                if hasattr(self.client, "olm") and self.client.olm:
                    self.client.olm.account.generate_one_time_keys(50)
                await self.client.keys_upload()  # uploads device keys + OTKs
                log.info("device keys + OTKs uploaded")
                # verify upload really stuck: query our own keys
                try:
                    q = await self.client.keys_query() if self.client.should_query_keys else None
                except Exception:
                    q = None
                try:
                    dk = self.client.device_store[self.cfg["user_id"]]
                    log.info("own device keys on record: %s", {d: k.ed25519[:8] for d, k in dk.items()})
                except Exception:
                    log.info("own device keys: none visible yet")
            except Exception as e:
                if "No key upload needed" not in str(e):
                    log.exception("key upload failed")
        except Exception:
            log.exception("crypto init failed (E2EE media will stay encrypted)")

    async def run(self) -> None:
        await self._crypto_init()
        log.info("watcher up: %s as %s (adapter=%s)", self.cfg["homeserver"], self.cfg["user_id"], self.cfg["adapter"])
        while True:
            try:
                resp = await self.client.sync(timeout=30000, full_state=True)
                if isinstance(resp, SyncResponse):
                    await self._scan_sync(resp)
            except Exception as e:
                log.error("sync error: %s — retrying in 5s", e)
                await asyncio.sleep(5)

    async def _scan_sync(self, resp: SyncResponse) -> None:
        # state events live in rooms.join[].state + timeline; check both
        for room_id, room in (resp.rooms.join or {}).items():
            evs = list(getattr(room.state, "events", []) or []) + list(getattr(room.timeline, "events", []) or [])
            if not evs:
                continue
            types = []
            for ev in evs:
                src = ev.source if hasattr(ev, "source") else None
                if isinstance(src, dict):
                    types.append(src.get("type") or "?")
                else:
                    types.append(type(ev).__name__)
            log.info("scan %s...: %d evs %s", room_id[:14], len(evs), types[:10])
            for ev in evs:
                src = ev.source if isinstance(getattr(ev, "source", None), dict) else {}
                if not isinstance(src, dict):
                    continue
                src.setdefault("room_id", room_id)  # sync events carry no room_id
                if src.get("type") == "m.room.member":
                    self._track_member_event(room_id, src)
                if src.get("type") in e2ee_keys.KEY_TYPES:
                    content = getattr(ev, "decrypted_payload", None) or src.get("content") or {}
                    dev = content.get("device_id", "")
                    ident = f"{src.get('sender', '')}:{dev}"
                    log.info("room key event seen: type=%s sender=%s dev=%s keys=%d",
                             src.get("type"), src.get("sender", "")[:20], dev[:12],
                             len(e2ee_keys.extract_keys_from_content(content, src.get("sender", ""))))
                    if hasattr(self, "receiver") and self.receiver:
                        for mk in e2ee_keys.extract_keys_from_content(content, src.get("sender", "")):
                            self.receiver.install(ident, mk)
                if src.get("type") == CALL_MEMBER_TYPE:
                    await self.handle_member_state(src)


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()
    w = CallWatcher(cfg)
    # auto-accept invites so Z can create the voice DM without manual steps
    def on_invite(room, event):  # noqa
        asyncio.ensure_future(w.client.join(room.room_id))
    w.client.add_event_callback(on_invite, (InviteEvent,))
    await w.run()


if __name__ == "__main__":
    asyncio.run(amain())
