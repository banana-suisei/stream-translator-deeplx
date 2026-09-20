# WebSocket audio ingest

This document describes the `ingest` input mode, which lets a remote client
(for example a phone capturing another app's playback audio) push audio into
`stream-translator-gpt` over the network instead of the tool pulling audio
from a stream URL, a local file, or a local PC device.

## 1. Usage

Pass `ingest` as the positional `URL` argument, and set `--ingest_port`:

```bash
stream-translator-gpt ingest \
  --ingest_port 8766 \
  --model tiny
```

Optional:

- `--ingest_host` — address to bind to. Defaults to `127.0.0.1` (loopback only).
- `--ingest_port` — **required** when `URL` is `ingest`. The tool exits
  immediately with an error if it is missing.

`ffmpeg` is **not** required for this mode (it is only used by the stream/local
file input modes).

On startup, the tool logs:

```text
[INFO] Audio ingest listening on ws://127.0.0.1:8766/ingest
```

Everything downstream — VAD slicing, transcription, translation, and output
(SSE, file, Discord, Telegram, cqhttp) — works exactly as it does for any other
input mode and needs no changes.

## 2. Wire protocol

The endpoint is `ws://<host>:<port>/ingest`.

1. The client opens a WebSocket connection.
2. The client sends exactly one JSON **text** message before any binary data:

   ```json
   {
     "type": "hello",
     "format": "pcm_s16le",
     "sample_rate": 16000,
     "channels": 1,
     "client": "your-app/1.0"
   }
   ```

   - `format` must be `"pcm_s16le"` (signed 16-bit little-endian PCM). This is
     the only supported format; anything else is rejected.
   - `sample_rate` may be any integer in `[8000, 192000]`. A rate other than
     16000 is **resampled** server-side (`scipy.signal.resample`), the same
     technique already used for local PC device capture.
   - `channels` may be `1` or `2`. Stereo is **downmixed** to mono
     server-side (channel average).
   - **Recommended**: send 16 kHz mono directly so the server never needs to
     resample. Resampling is a supported fallback, not the intended common
     case, and per-chunk FFT resampling can introduce small edge artifacts at
     chunk boundaries.

3. The server replies with one JSON text message:

   ```json
   {
     "type": "ready",
     "sample_rate": 16000,
     "samples_per_frame": 512,
     "resampling": true
   }
   ```

   `resampling` tells the client whether the server is converting its
   declared rate.

4. The client then streams raw PCM as **binary** WebSocket frames, in any
   payload size. There is no header or length prefix — each WebSocket binary
   frame's payload is just PCM bytes. A payload of ~100 ms (3200 bytes at
   16 kHz mono) is a reasonable size: small enough for low subtitle latency,
   large enough to keep per-frame overhead low over a multi-hour session.

5. The client closes the connection (code `1000`) when it is done.

### Close codes

| Code   | Meaning                                                    | Sent by |
| ------ | ----------------------------------------------------------- | ------- |
| `1000` | Normal end of stream                                         | either  |
| `1003` | Unsupported data: bad `hello`, unsupported format, binary frame before `hello` | server  |
| `1008` | Policy violation: another ingest session is already active   | server  |
| `1011` | Server internal error                                        | server  |

A client that receives `1003` should treat it as a configuration error (its
own handshake is malformed) rather than retry unchanged. Any other close
should be treated as a dropped connection worth reconnecting to, with backoff.

### Reconnection behavior

Only **one** ingest session is active at a time. If a client disconnects —
cleanly or abruptly (dropped Wi-Fi, killed socket, no close frame at all) —
the server ends that session and keeps listening. It does **not** stop the
transcription pipeline, and a new connection (from the same or a different
client) is accepted immediately and resumes feeding audio into the same
running process. You do not need to restart `stream-translator-gpt` after a
network hiccup.

A second client connecting while a session is already active is closed
immediately with `1008`; it does not interrupt or interleave with the active
session.

## 3. Security

**There is no authentication, no authorization, and no encryption on this
port.** Anyone who can reach `--ingest_host:--ingest_port` can open a
WebSocket connection and inject arbitrary audio into the transcription
pipeline.

This is intentional: the ingest port is meant to sit behind a reverse proxy
that terminates TLS and applies access control if it needs to be reachable
from outside the machine it runs on. The default bind address is
`127.0.0.1` (loopback only) for exactly this reason — the insecure-by-default
case is "only this machine can connect at all." Binding `--ingest_host` to
`0.0.0.0` or any non-loopback address is an explicit choice, and the tool logs
a warning when you do it. Do not expose the ingest port directly to an
untrusted network.

## 4. Known limitation

Like every other input mode in this tool, the queue between the audio getter
and the VAD slicer (`queue.SimpleQueue`) is unbounded. If transcription falls
behind real time for a long stretch, memory usage grows without an upper
bound. This is a pre-existing property of the pipeline, not something specific
to network ingest, and is not addressed by this feature.

## 5. Verification

`tools/ingest_smoke_test.py` is a standalone script (this repo has no test
framework) that exercises the ingest path without needing a phone:

```bash
# Pure decoder logic, no socket involved.
python tools/ingest_smoke_test.py --unit

# Stream a 16-bit PCM WAV file into a running server.
python -m stream_translator_gpt.main ingest --ingest_port 8766 --model tiny
python tools/ingest_smoke_test.py --wav your_speech.wav --port 8766

# Negative-path / resilience cases against a running server.
python tools/ingest_smoke_test.py --misbehave bad-format --port 8766
python tools/ingest_smoke_test.py --misbehave binary-first --port 8766
python tools/ingest_smoke_test.py --misbehave double-client --port 8766
python tools/ingest_smoke_test.py --misbehave abrupt-drop --port 8766
```
