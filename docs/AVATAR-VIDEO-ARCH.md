# Anime avatar video-call architecture

Research: 2026-10-28. Current primary documentation, including 2025–2026 developments; older sources retained for established tools. Hardware budgets below are engineering estimates, not measurements.

## 1. Verdict

**Choose B: reuse the working VRM renderer, with deliberately restrained animation.** Keep A (layered PNG) as a fallback only if operational testing fails. The [staged reference](existing-three-vrm-renderer-skill.md) already supplies lip sync, expressions, breathing, blinking, gestures, and WebSocket controls. Rebuilding these in Live2D buys little; new artwork/rigging could dominate effort.

Worth building as an **optional presence/fun feature**, particularly for companionship and demos. It adds speaker identity and visible turn-taking, not better reasoning or meeting productivity. Timebox the browser proof to one day and a call prototype to 3–5 engineering days; reliable unattended deployment could take 1–2 weeks, longer if video/E2EE support is missing from the bridge. These are estimates assuming access to the documented implementation.

At 320×240, clean PNG mouth swaps can be just as readable; at 720p, VRM gains coherent head rotation, hair, and lighting. Both should look intentionally illustrated. My judgment: fixed eye-level, shoulders-up framing, a quiet opaque background, soft lighting, occasional blinks, tiny head movement, and breathing create convincing *call presence*. Large random gestures, constant eye oscillation, and a realistic room behind a plastic-looking character increase uncanniness. Start at 640×360/30 fps; disable orbit controls and most existing auto-gestures. Full Cubism is not equivalent to a cheap PNG talker in either authoring or runtime cost.

## 2. Prior art

| Existing option | License/terms | How close to done-for-us? |
|---|---|---|
| [VSeeFace](https://www.vseeface.icu/) | Proprietary freeware; commercial use allowed, modification restricted | VRM, audio lip sync, and virtual webcam for conferencing already exist. Windows-first; Wine notes warn virtual camera probably fails. Desktop proof, not our Linux daemon. |
| [OBS](https://github.com/obsproject/obs-studio) + [v4l2loopback](https://github.com/v4l2loopback/v4l2loopback) | GPL-2.0-or-later / GPL-2.0 | Mature composition/device plumbing; still needs avatar animation, audio routing, and supervision. [OBS Linux virtual camera uses loopback](https://obsproject.com/kb/virtual-camera-troubleshooting). |
| [three-vrm](https://github.com/pixiv/three-vrm) | MIT; avatar/animation assets separately licensed | Already our renderer. Add synchronized media input and outgoing video integration. |
| [Cubism Web SDK](https://www.live2d.com/en/sdk/download/web/) + [MotionSync](https://docs.live2d.com/cubism-sdk-manual/cubism-sdk-motionsync-plugin-for-web/) | Live2D proprietary Core plus Open Software terms; [release licensing](https://www.live2d.com/en/sdk/license/) depends on use | WebGL and audio motion exist; requires rigged model and bridge. Web R5 released April 2026. Plain PNG swaps need none of Cubism. |
| [NVIDIA Audio2Face-3D SDK](https://github.com/NVIDIA/Audio2Face-3D-SDK) | MIT SDK; models/dependencies have separate terms | Streaming facial inference exists; CUDA/TensorRT and retargeting add work. Five vowel shapes discard much of its output. Not MVP. |
| [Wav2Lip](https://github.com/Rudrabha/Wav2Lip) / [SadTalker](https://github.com/OpenTalker/SadTalker) | Wav2Lip research/personal/noncommercial restriction; SadTalker Apache-2.0, dependencies separately | Video lip editing / portrait-to-video pipelines, not VRM drivers. Stock file-oriented workflows are not turnkey interruptible calls; real-time adaptations require buffering/optimization. No verified 5060 Ti latency here. |
| [Tavus CVI](https://www.tavus.io/pricing) | Proprietary hosted service | Real-time avatars, WebRTC, and advertised Meet/Zoom integration. Closest meeting product; not a local anime renderer or drop-in Matrix E2EE endpoint. |

## 3. Minimal pipeline and integration

`streaming TTS PCM + timestamps → bounded playout queue → mouth weights + audio clock → VRM canvas → video track → existing encrypted call sender`

Use the **same outgoing audio**, never room/microphone audio. Initially derive mouth openness from short RMS windows, with a silence gate and quick attack/slower release. The reference’s FFT-band-to-vowel mapping is a visual heuristic, not phoneme recognition; its band comments also disagree with its bin fractions. Later prefer provider timestamps: [Azure exposes viseme IDs and audio offsets](https://learn.microsoft.com/en-us/azure/cognitive-services/speech-service/how-to-speech-synthesis-viseme). Map them onto available model shapes, interpolating and closing on silence/consonants. [VRM 1.0 uses `aa/ih/ou/ee/oh`](https://vrm.dev/vrm1/expression/); older presets are A/I/U/E/O. “A2” is custom, not a portable preset.

Schedule audio and animation against a shared sample-based timeline, not WebSocket arrival or independent timers. In-browser audio can feed a `MediaStreamAudioDestinationNode`; native audio needs an explicit playout-clock mapping. Cancel queued audio and mouth events together on interruption.

**Preferred when the bridge owns a browser peer connection:** render in that browser context; [`canvas.captureStream(30)`](https://developer.mozilla.org/en-US/docs/Web/API/HTMLCanvasElement/captureStream) supplies video, not audio. Publish through the bridge SDK; use [`replaceTrack`](https://developer.mozilla.org/en-US/docs/Web/API/RTCRtpSender/replaceTrack) only for an existing compatible video sender. A voice-only call may require adding a transceiver, negotiation, and video signaling. Composite the background inside the canvas; DOM overlays are not captured.

**Native bridge:** a browser track cannot simply cross process boundaries. Prefer its native frame-source API if available, with bounded timestamped frame IPC; browser readback is additional work. Otherwise use renderer/OBS capture → raw frames → v4l2loopback → bridge camera input. Enable `exclusive_caps=1` when needed and start the producer before camera discovery. Loopback carries no audio; retain synchronized TTS separately.

**Headless Chromium is a rendering host, not an OS virtual camera.** GPU support is configuration-dependent: [Chromium documents X11 autodetection and possible Vulkan alternatives](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/gpu/using-gpu-hardware-in-headless-chrome.md). Verify hardware rendering; Xvfb alone proves nothing. Avoid screenshot polling. Keep media injection before the existing E2EE encoder transform; verify new video publication and key rotation. WebRTC transport encryption alone does not establish Matrix E2EE.

## 4. Latency and resource budget

Estimates for one warmed-up 720p30 stream on the specified Ryzen/RTX, simple assets, no neural inference. CPU quantities are fractions of one logical core; GPU times are work per frame, not utilization guarantees.

| Component | Delay budget | CPU / GPU budget |
|---|---|---|
| TTS request → first PCM | Provider-dependent; planning allowance 150–800+ ms | Existing TTS cost; unspecified |
| Queue, audio scheduling | 20–60 ms | <0.05 core; negligible GPU |
| RMS / timestamp mapping | 5–20 ms analysis window; <1 ms compute | <0.05 core; no GPU inference |
| A: PNG composition | 0–33 ms frame wait | 0.02–0.1 core; <1 ms GPU/frame |
| A: rigged Cubism | Same frame wait | 0.05–0.3 core; 1–4 ms GPU/frame, model-dependent |
| B: VRM update/render | Same frame wait | 0.1–0.4 core; 1–5 ms GPU/frame |
| Canvas capture/conversion | 2–10 ms | 0.05–0.3 core; copies possible |
| WebRTC encoding | 3–20 ms | Software: 0.2–1 core; hardware: lower CPU plus encoder engine |
| Optional loopback/compositor | Additional 10–50 ms | Additional 0.1–0.5 core; extra copies/composition |
| Network, jitter, decode/display | 40–150+ ms | Receiver/network-dependent |

Target first PCM arrival → local visible mouth **30–120 ms**; remote **80–300 ms**, excluding TTS generation. Target steady-state remote audio/video skew ≤50 ms. These overlap and are not simply additive; 30 fps already quantizes motion to 33 ms. Reserve 1–2 GB GPU memory for browser/rendering, then measure. Do not assume Linux Chromium uses NVENC because the GPU supports encoding. [NVIDIA’s 2025-era Audio2Face benchmarks](https://docs.nvidia.com/ace/audio2face-3d-microservice/latest/text/interacting/performance.html) demonstrate real-time throughput on other GPUs, not this deployment’s latency.

## 5. MVP, smallest-first

1. **2–6 hours:** load existing renderer and a local speech clip in a browser; RMS drives `aa`. Pass: visible mouth, silence closes, click resumes audio.
2. **Half-day:** capture canvas into a second video element. Pass: background included, no UI, stable 30 fps at 360p and 720p.
3. **Half-day:** stream timestamped PCM and interruptions. Pass: repeated speech/cancel tests show no stale mouth movement or accumulating drift.
4. **1–2 days:** publish into a test Matrix call using the appropriate bridge boundary. Pass: remote A/V, video negotiation, reconnect, and E2EE/key-rotation verification.
5. **1–2 days:** supervise headless startup and renderer failure. Pass: hour-long run, resource measurements, automatic restart, audio survives video failure.
6. **Half-day:** compare PNG and restrained VRM at 240p/720p with listeners. Keep video only if presence improves without distraction; add timed visemes only if needed.

## 6. Remaining risks

Bridge implementation, TTS timestamp support, avatar complexity/rights, GPU drivers, codec path, and simultaneous inference load remain unknown. Validate CORS/origin-clean capture, autoplay, background throttling, expression mouth overrides, and bounded queues. The staged reference’s corrective pose document is absent: honor its warning to use pose-based idle, version-aware quaternion conversion, and avoid assuming clips loop cleanly. Actual renderer code/assets were not staged or benchmarked.
