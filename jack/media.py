"""Media layer: LiveKit join + audio plumbing (§4.3 media side).

One implementation serves all adapters:
- join the derived room (token from jack.mint),
- publish a dummy/silent video track -> the agent appears as a participant
  tile in Element X (openclaw-matrix-voice trick, credited in docs),
- subscribe to remote audio -> frames -> VAD/endpointing -> adapter.on_transcript,
- adapter.transcript_stream() -> TTS -> publish audio track.

v0.1 scope: join + tile + remote-audio tap + play a local wav/pcm as our track.
STT/TTS wiring is config-driven and lands with the adapters.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import wave
from typing import AsyncIterator, Callable, Optional

from livekit import rtc

log = logging.getLogger("jack.media")

AUDIO_RATE = 48000
FRAME_MS = 20
FRAME_SAMPLES = AUDIO_RATE * FRAME_MS // 1000  # 960


class CallMedia:
    """Owns the rtc.Room for one call session."""

    def __init__(self, url: str, token: str, identity: str):
        self.url = url
        self.token = token
        self.identity = identity
        self.room = rtc.Room()
        self._audio_source: Optional[rtc.AudioSource] = None
        self._on_user_audio: Optional[Callable] = None  # async fn(frames: list[bytes])
        self._on_utterance: Optional[Callable] = None   # async fn(pcm16_48k: bytes)
        self._remote_buf: asyncio.Queue = asyncio.Queue()
        self._tts_queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list = []
        self._remote_frames = 0
        self._remote_subs = 0
        self._tts_busy = False   # True while OUR voice plays -> suppress VAD/utterances
        self._speak_lock = asyncio.Lock()
        self._remote_rate: Optional[int] = None  # actual SFU mic track rate (don't assume 48k!)

    def on_user_audio(self, cb: Callable) -> None:
        self._on_user_audio = cb

    receiver = None  # MediaKeyReceiver, set by watcher for E2EE key installs

    async def connect(self, room_options=None) -> None:
        @self.room.on("track_subscribed")
        def _on_track(track: rtc.Track, pub: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                self._remote_subs += 1
                log.info("remote audio subscribed from %s", participant.identity)
                self._tasks.append(asyncio.ensure_future(self._read_remote_audio(track)))

        @self.room.on("participant_connected")
        def _on_participant(p):
            log.info("participant connected: %s", p.identity)

        import inspect
        kwargs = {}
        if room_options is not None and "options" in str(inspect.signature(self.room.connect)):
            kwargs["options"] = room_options
        await self.room.connect(self.url, self.token, **kwargs)
        log.info("connected to %s as %s (room=%s)", self.url, self.identity, self.room.name)

    async def publish_dummy_video(self) -> None:
        """Silent video track so Element X shows the agent as a call tile."""
        try:
            source = rtc.VideoSource(width=320, height=240)
            track = rtc.LocalVideoTrack.create_video_track("jack-cam", source)
            await self.room.local_participant.publish_track(track)
        except Exception as e:  # tile is cosmetic; never fail the call over it
            log.warning("dummy video publish failed (tile hidden): %s", e)

    async def publish_audio_track(self) -> None:
        self._audio_source = rtc.AudioSource(AUDIO_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("jack-voice", self._audio_source)
        await self.room.local_participant.publish_track(track)
        t = asyncio.ensure_future(self._tts_pump())
        def _tts_done(tk):
            if tk.cancelled() or tk.exception() is None:
                return
            log.error("tts task died: %r", tk.exception())
        t.add_done_callback(_tts_done)
        self._tasks.append(t)

    async def _tts_pump(self) -> None:
        """Queue of pcm byte chunks -> audio frames."""
        try:
            await self._tts_pump_inner()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("TTS PUMP DIED")

    async def _tts_pump_inner(self) -> None:
        while True:
            pcm = await self._tts_queue.get()
            if pcm is None:
                break
            # chunk into FRAME_SAMPLES int16 frames
            step = FRAME_SAMPLES * 2
            n = 0
            for i in range(0, len(pcm), step):
                chunk = pcm[i:i + step]
                if len(chunk) < step:
                    chunk = chunk + b"\x00" * (step - len(chunk))
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=AUDIO_RATE,
                    num_channels=1,
                    samples_per_channel=FRAME_SAMPLES,
                )
                self._tts_busy = True
                await self._audio_source.capture_frame(frame)
                n += 1
            if self._tts_queue.empty():
                self._tts_busy = False
            log.info("pumped %d bytes -> %d audio frames", len(pcm), n)

    async def speak(self, text: str, voice: str = "emma") -> None:
        """text -> Chatterbox wav -> PCM16 48k -> tts queue (blocking TTS in thread)."""
        import aiohttp, tempfile, os as _os
        body = {"input": text, "voice": voice, "response_format": "wav"}
        url = _os.environ.get("JACK_TTS_URL", "http://localhost:8003/v1/audio/speech")
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=body, timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                wav = await r.read()
        def _convert():
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav)
                path = f.name
            try:
                return CallMedia.wav_to_pcm16(path)
            finally:
                _os.unlink(path)
        pcm = await asyncio.get_running_loop().run_in_executor(None, _convert)
        async with self._speak_lock:
            log.info("tts %d chars -> %d bytes pcm, playing", len(text), len(pcm))
            self.enqueue_tts_pcm(pcm)

    def enqueue_tts_pcm(self, pcm16_bytes: bytes) -> None:
        log.info("enqueued %d bytes to tts queue", len(pcm16_bytes))
        self._tts_queue.put_nowait(pcm16_bytes)

    async def finish_tts(self) -> None:
        await self._tts_queue.put(None)

    def on_utterance(self, cb) -> None:
        """cb(pcm16_48k: bytes) coroutine — fired when the caller finishes a phrase."""
        self._on_utterance = cb

    async def _read_remote_audio(self, track: rtc.Track) -> None:
        buf = b""
        frames = []
        utt = []            # 0.5s batches accumulated for the current phrase
        rms_hist = []
        noise_hist = []   # rolling RMS window for adaptive noise floor (car/street noise)
        speech = False
        silence_batches = 0
        speech_batches = 0
        buf_batches = 0
        barge_streak = 0  # consecutive loud batches while our TTS plays (barge-in watch)
        import os as _os
        end_silence = int(_os.environ.get("JACK_ENDPOINT_SILENCE_BATCHES", "3"))
        rate = None
        import array as _array
        try:
            async for frame in rtc.AudioStream(track):
                if rate is None:
                    rate = frame.frame.sample_rate
                    self._remote_rate = rate
                    _raw = bytes(frame.frame.data)
                    log.info("remote mic frame: rate=%d ch=%d spc=%d len(mv)=%d BYTELEN=%d first8=%s",
                             rate, frame.frame.num_channels, frame.frame.samples_per_channel,
                             len(frame.frame.data), len(_raw), _raw[:8].hex())
                    self._remote_channels = frame.frame.num_channels
                data = bytes(frame.frame.data)
                if getattr(self, "_remote_channels", 1) == 2:
                    # stereo interleaved s16 -> mono (average L/R) before anything else
                    import array as _arr2
                    aa = _arr2.array("h", data[: len(data) // 2 * 2])
                    data = array.array("h", ((aa[i] + aa[i + 1]) // 2 for i in range(0, len(aa) - 1, 2))).tobytes()
                buf += data
                chunk = rate // 2  # 0.5s at the TRUE rate (mono now)
                while len(buf) >= chunk * 2:
                    frames.append(buf[:chunk * 2])
                    buf = buf[chunk * 2:]
                if len(frames) >= 2:  # process in 0.5s batches
                    batch = b"".join(frames)
                    frames = []
                    self._remote_frames += rate // 40  # ~20ms frames tally
                    buf_batches += 1
                    a = _array.array("h", batch)
                    rms = (sum(x * x for x in a) / max(1, len(a))) ** 0.5
                    rms_hist.append(int(rms))
                    # adaptive noise floor: 25th pct of recent RMS, speech = 3.5x floor
                    # (updated only from non-TTS batches so our own playback doesn't
                    # inflate the caller's floor; during TTS the last floor is reused)
                    if not self._tts_busy:
                        noise_hist.append(int(rms))
                        noise_hist = noise_hist[-24:]
                    floor = sorted(noise_hist)[max(0, len(noise_hist) // 4)] if noise_hist else 170
                    thr = max(600, min(int(floor * 3.5), 9000))
                    if self._tts_busy:  # our own voice — don't transcribe ourselves
                        # BARGE-IN: caller clearly louder than 3x their adaptive
                        # threshold for 2+ consecutive 0.5s batches -> interrupt TTS
                        if rms > thr * 3:
                            barge_streak += 1
                        else:
                            barge_streak = 0
                        if barge_streak >= 2:
                            drained = 0
                            while not self._tts_queue.empty():
                                if self._tts_queue.get_nowait() is not None:
                                    drained += 1
                            self._tts_busy = False
                            barge_streak = 0
                            speech = True
                            speech_batches = 1
                            silence_batches = 0
                            utt.clear()
                            utt.append(batch)  # capture the interrupting speech
                            log.info("barge-in: caller rms=%d > 3x thr=%d for 2+ batches, "
                                     "drained %d tts chunks, playback interrupted", int(rms), thr, drained)
                        else:
                            utt.clear()
                            speech = False
                            silence_batches = 0
                            speech_batches = 0
                            buf_batches = 0
                    else:
                        if rms > thr:
                            speech = True
                            silence_batches = 0
                            speech_batches += 1
                        elif speech:
                            silence_batches += 1
                        if speech and buf_batches > 30:  # hard cap 15s
                            silence_batches = 99
                        if speech and silence_batches >= end_silence:  # trailing silence
                            pcm = b"".join(utt)
                            had_speech = speech_batches >= 2
                            if rms_hist:
                                log.info("vad stats: rms min/mean/max = %d/%d/%d floor=%d thr=%d over %d batches",
                                         min(rms_hist), sum(rms_hist) // len(rms_hist),
                                         max(rms_hist), floor, thr, len(rms_hist))
                            rms_hist = []
                            utt = []
                            speech = False
                            silence_batches = 0
                            speech_batches = 0
                            buf_batches = 0
                            if had_speech and len(pcm) > rate:  # >0.5s of audio
                                log.info("utterance endpointed: %d bytes pcm", len(pcm))
                                _wavpath = self._dump_utterance(pcm, rate)
                                if _wavpath:
                                    log.info("utterance audio saved: %s", _wavpath)
                                if self._on_utterance:
                                    self._tasks.append(asyncio.ensure_future(
                                        self._on_utterance(pcm)))
                            elif not had_speech:
                                log.info("endpoint without enough speech (noise only), dropped")
                        elif speech:
                            utt.append(batch)
                    if buf_batches % 40 == 0:  # heartbeat every ~20s
                        log.info("reader pulse: %d batches, last rms %d, busy %s, utt %d",
                                 buf_batches, int(rms), self._tts_busy, len(utt))
        except Exception:
            log.exception("audio reader died")

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        log.info("remote audio stats: %d subscriptions, %d x0.5s batches received", self._remote_subs, self._remote_frames)
        try:
            await self.room.disconnect()
        except Exception:
            pass
        log.info("media closed")

    @staticmethod
    def _dump_utterance(pcm16: bytes, rate: int) -> str | None:
        import array as _array
        import time as _time
        try:
            path = "/tmp/jack_utt_%d.wav" % int(_time.time())
            with open(path, "wb") as f:
                f.write(CallMedia._pcm_to_wav_bytes(_array.array("h", pcm16), rate))
            return path
        except Exception:
            return None

    @staticmethod
    def _pcm_to_wav_bytes(a, rate: int = 48000) -> bytes:
        data = a.tobytes()
        import struct as _struct
        hdr = b"RIFF" + _struct.pack("<I", 36 + len(data)) + b"WAVE"
        hdr += b"fmt " + _struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        hdr += b"data" + _struct.pack("<I", len(data))
        return hdr + data

    @staticmethod
    def wav_to_pcm16(path: str) -> bytes:
        """Read mono wav (PCM16 or IEEE-float32), linear-resample to AUDIO_RATE.

        Chatterbox emits format-3 (float32) wavs which the stdlib wave module
        refuses — parse RIFF manually and convert to int16.
        """
        import array
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise ValueError(f"not a RIFF/WAVE file: {path}")
        fmt = data = None
        pos = 12
        while pos + 8 <= len(raw):
            cid = raw[pos:pos + 4]
            size = int.from_bytes(raw[pos + 4:pos + 8], "little")
            if cid == b"fmt ":
                fmt = raw[pos + 8:pos + 8 + size]
            elif cid == b"data":
                data = raw[pos + 8:pos + 8 + size]
            pos += 8 + size + (size & 1)
        if fmt is None or data is None:
            raise ValueError(f"missing fmt/data chunk: {path}")
        tag = int.from_bytes(fmt[0:2], "little")
        channels = int.from_bytes(fmt[2:4], "little")
        rate = int.from_bytes(fmt[4:8], "little")
        bits = int.from_bytes(fmt[14:16], "little")
        if tag == 1 and bits == 16:  # PCM16
            a = array.array("h", data[: len(data) // 2 * 2])
        elif tag == 3 and bits == 32:  # float32
            a = array.array("h", (
                max(-32768, min(32767, int(x * 32767)))
                for x in array.array("f", data[: len(data) // 4 * 4])))
        else:
            raise ValueError(f"unsupported wav: format tag={tag} bits={bits}")
        if channels > 1:  # downmix: average channel pairs
            a = array.array("h", (
                (a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, channels)))
        n = len(a)
        if rate == AUDIO_RATE:
            return a.tobytes()
        new_n = int(n * AUDIO_RATE / rate)
        out = array.array("h", bytes(new_n * 2))
        for i in range(new_n):
            src = i * rate / AUDIO_RATE
            i0 = int(src)
            frac = src - i0
            s0 = a[i0] if i0 < n else 0
            s1 = a[i0 + 1] if i0 + 1 < n else s0
            out[i] = int(s0 + (s1 - s0) * frac)
        return out.tobytes()
