"""STT: whisper.cpp server on the sidecar (ggml-base, CPU) — zero downloads, no VRAM.

Uses the prebuilt /opt/whisper.cpp install (server binary + ggml-base.bin
already on disk). Wires 48kHz mono int16 PCM (from media.py's VAD tap) -> 16k wav
-> HTTP POST /inference. Start the server once:

  setsid nohup /opt/whisper.cpp/build/bin/whisper-server \\
    --host 127.0.0.1 --port 8901 -m /opt/whisper.cpp/models/ggml-base.bin \\
    </dev/null >>/tmp/whisper-server.log 2>&1 &

(fallback backend: faster-whisper small if JACK_STT_BACKEND=faster — kept for
portability to machines without the whisper.cpp install)
"""

from __future__ import annotations

import array
import asyncio
import logging
import os
import struct

log = logging.getLogger("jack.stt")

SAMPLE_RATE = 48000
WHISPER_RATE = 16000
STT_URL = os.environ.get("JACK_STT_URL", "http://127.0.0.1:8901/inference")
BACKEND = os.environ.get("JACK_STT_BACKEND", "whisper_cpp")


def _resample_to_16k(pcm16: bytes, src_rate: int = SAMPLE_RATE) -> array.array:
    a = array.array("h", pcm16[: len(pcm16) // 2 * 2])
    n = len(a)
    if src_rate == WHISPER_RATE:
        return a
    new_n = int(n * WHISPER_RATE / src_rate)
    out = array.array("h", bytes(new_n * 2))
    for i in range(new_n):
        src = i * src_rate / WHISPER_RATE
        i0 = int(src)
        frac = src - i0
        s0 = a[i0] if i0 < n else 0
        s1 = a[i0 + 1] if i0 + 1 < n else s0
        out[i] = int(s0 + (s1 - s0) * frac)
    return out


def _pcm_to_wav_bytes(a: array.array, rate: int = WHISPER_RATE) -> bytes:
    data = a.tobytes()
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", len(data))
    return hdr + data


def _whisper_cpp_sync(pcm16_48k: bytes, language: str, src_rate: int = SAMPLE_RATE) -> str:
    a = _resample_to_16k(pcm16_48k, src_rate)
    if len(a) < WHISPER_RATE // 4:  # <250ms: noise
        return ""
    wav = _pcm_to_wav_bytes(a)
    import json as _json
    import urllib.request
    import uuid

    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        + b"Content-Type: audio/wav\r\n\r\n" + wav + b"\r\n"
        + f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'
        + f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="language"\r\n\r\n' + language.encode() + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        STT_URL, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = _json.loads(resp.read())
    return (data.get("text") or "").strip()


def _faster_sync(pcm16_48k: bytes, language: str, src_rate: int = SAMPLE_RATE) -> str:
    import numpy as np

    a = _resample_to_16k(pcm16_48k, src_rate)
    if len(a) < WHISPER_RATE // 4:
        return ""
    audio = np.frombuffer(a.tobytes(), dtype=np.int16).astype(np.float32) / 32768.0
    from faster_whisper import WhisperModel
    global _FW_MODEL
    try:
        model = _FW_MODEL
    except NameError:
        model = None
    if model is None:
        log.info("loading faster-whisper small int8 (CPU)...")
        model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4)
        globals()["_FW_MODEL"] = model
        log.info("whisper ready")
    segments, _info = model.transcribe(
        audio, language=language, beam_size=1, vad_filter=True,
        condition_on_previous_text=False, temperature=0.0)
    parts = []
    kept_segments = []
    for seg in segments:
        kept_segments.append(seg)
        t = seg.text.strip()
        if not t:
            continue
        # soften the confidence filter: drop only if no_speech>0.6 OR
        # avg_logprob<-1.1 — the old (>0.5 / <-0.9) cut dropped real final
        # segments ("Rubik's Cube").
        if seg.no_speech_prob > 0.6 or seg.avg_logprob < -1.1:
            continue
        parts.append(t)
    # always keep the LAST non-empty segment — phrase tails are the most
    # commonly dropped words (final keyword of the sentence)
    if not parts and kept_segments:
        for seg in reversed(kept_segments):
            t = seg.text.strip()
            if t:
                log.info("stt: only low-confidence tail kept: %r (nsp=%.2f lp=%.2f)",
                         t[:80], seg.no_speech_prob, seg.avg_logprob)
                parts.append(t)
                break
    text = " ".join(parts).strip()
    if text:
        log.info("stt confidence-filtered transcript: %r", text[:120])
    return text


def _transcribe_sync(pcm16_48k: bytes, language: str = "en", src_rate: int = SAMPLE_RATE) -> str:
    if BACKEND == "faster":
        return _faster_sync(pcm16_48k, language, src_rate)
    return _whisper_cpp_sync(pcm16_48k, language, src_rate)


async def transcribe(pcm16_48k: bytes, language: str = "en", src_rate: int = SAMPLE_RATE) -> str:
    """Async wrapper — blocking HTTP/resample in a worker thread."""
    return await asyncio.get_running_loop().run_in_executor(
        None, _transcribe_sync, pcm16_48k, language, src_rate)
