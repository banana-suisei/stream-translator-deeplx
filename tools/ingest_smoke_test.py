#!/usr/bin/env python
"""Standalone verification for the WebSocket PCM audio ingest server.

This repo has no test framework (see task constraint SC5), so this is a plain
script with PASS/FAIL prints and a non-zero exit code on failure, not a test
suite.

Modes
-----
--unit
    Exercises `PcmStreamDecoder` directly, no socket involved. Covers the
    conversion chain: odd-byte carry across payloads, stereo downmix,
    48 kHz -> 16 kHz resampling, and the hard 512-samples-per-frame contract
    that Silero VAD requires.

--wav <path> [--host HOST] [--port PORT]
    Connects to a *running* ingest server (`python -m stream_translator_gpt.main
    ingest --ingest_port <PORT> ...`) and streams a 16-bit PCM WAV file in
    ~100 ms chunks, using the WAV's own sample rate/channel count in the
    `hello` handshake. Useful for AC-S3 (plain 16 kHz mono) and AC-S8
    (48 kHz stereo, exercising downmix + resample server-side).

--misbehave <case> [--host HOST] [--port PORT]
    Drives the negative paths against a running server:
      bad-format     hello declares an unsupported format -> expect close 1003
      binary-first   a binary frame is sent before any hello -> expect close 1003
      double-client  a second client connects while one is active -> expect
                     the second to be closed 1008, and the first to be undisturbed
      abrupt-drop    a client is killed without a clean close handshake, then a
                     fresh client connects and must be accepted normally -- this
                     is the client-visible half of AC-S7 (the getter must keep
                     listening; whether it fails to also emit `None` on the
                     output queue cannot be observed from a WebSocket client and
                     must be confirmed by watching the server process not exit).

Example
-------
    python tools/ingest_smoke_test.py --unit
    python tools/ingest_smoke_test.py --wav sample.wav --port 8766
    python tools/ingest_smoke_test.py --misbehave abrupt-drop --port 8766
"""

import argparse
import json
import os
import struct
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_translator_gpt.audio_ingest_server import PcmStreamDecoder
from stream_translator_gpt.common import SAMPLE_RATE, SAMPLES_PER_FRAME

_failures = 0


def check(name: str, condition: bool, detail: str = '') -> None:
    global _failures
    if condition:
        print(f'[PASS] {name}')
    else:
        _failures += 1
        suffix = f'  ({detail})' if detail else ''
        print(f'[FAIL] {name}{suffix}')


def _sine_pcm16(num_samples: int, rate: int, freq: float = 440.0, amplitude: float = 0.5) -> bytes:
    t = np.arange(num_samples)
    samples = (amplitude * np.sin(2 * np.pi * freq * t / rate) * 32767).astype(np.int16)
    return samples.tobytes()


def run_unit() -> int:
    print('--- PcmStreamDecoder unit checks ---')

    # 1. Exact framing: a payload worth exactly N frames must yield exactly N
    #    frames, each exactly SAMPLES_PER_FRAME samples long.
    decoder = PcmStreamDecoder(source_rate=SAMPLE_RATE, channels=1)
    payload = _sine_pcm16(SAMPLES_PER_FRAME * 3, SAMPLE_RATE)
    frames = decoder.feed(payload)
    check('16kHz mono: exact frame count for exact input', len(frames) == 3, f'got {len(frames)}')
    check('16kHz mono: every frame is exactly SAMPLES_PER_FRAME', all(len(f) == SAMPLES_PER_FRAME for f in frames),
          f'sizes={[len(f) for f in frames]}')
    check('16kHz mono: frames are float32', all(f.dtype == np.float32 for f in frames))

    # 2. Odd-byte carry: a single leftover byte from one payload must combine
    #    correctly with the next payload's first byte to reconstruct one
    #    int16 sample, without crashing and without emitting early frames.
    decoder = PcmStreamDecoder(source_rate=SAMPLE_RATE, channels=1)
    sample_value = 12345
    raw = struct.pack('<h', sample_value)
    frames_a = decoder.feed(raw[:1])
    check('odd-byte split: single leftover byte does not crash and emits no frame', frames_a == [])
    check('odd-byte split: leftover byte is buffered', decoder._odd_byte == raw[:1])
    frames_b = decoder.feed(raw[1:])
    check('odd-byte split: second call emits no frame yet (buffer < 512)', frames_b == [])
    reconstructed = decoder._buffer[0] if len(decoder._buffer) else None
    expected = sample_value / 32768.0
    check('odd-byte split: reconstructed sample matches original within tolerance', reconstructed is not None and
          abs(reconstructed - expected) < 1e-4, f'reconstructed={reconstructed}, expected={expected}')

    # 3. Stereo downmix: interleaved L/R samples must average to (L+R)/2.
    decoder = PcmStreamDecoder(source_rate=SAMPLE_RATE, channels=2)
    left, right = 8000, -8000
    interleaved = np.array([left, right] * 4, dtype=np.int16)
    decoder.feed(interleaved.tobytes())
    downmixed = decoder._buffer
    check('stereo downmix: opposite-sign channels average to ~0',
          len(downmixed) == 4 and np.allclose(downmixed, 0.0, atol=1e-6), f'buffer={downmixed}')

    # 4. Resample 48kHz -> 16kHz: still produces exact 512-sample frames, and
    #    roughly a third as many output samples as input samples.
    decoder = PcmStreamDecoder(source_rate=48000, channels=1)
    input_samples = SAMPLES_PER_FRAME * 3 * 3  # -> ~3 output frames at 1/3 rate
    payload = _sine_pcm16(input_samples, 48000)
    frames = decoder.feed(payload)
    check('48k->16k resample: produces at least 2 full frames', len(frames) >= 2, f'got {len(frames)}')
    check('48k->16k resample: every frame is exactly SAMPLES_PER_FRAME',
          all(len(f) == SAMPLES_PER_FRAME for f in frames), f'sizes={[len(f) for f in frames]}')

    # 5. No frame is ever a size other than SAMPLES_PER_FRAME, across a mixed
    #    sequence of small/irregular payload sizes (simulating real network
    #    chunking, which does not align to sample or frame boundaries).
    decoder = PcmStreamDecoder(source_rate=SAMPLE_RATE, channels=1)
    rng = np.random.default_rng(0)
    all_frames = []
    total_samples_in = 0
    remaining = _sine_pcm16(SAMPLES_PER_FRAME * 10 + 7, SAMPLE_RATE)
    while remaining:
        chunk_len = int(rng.integers(1, 37))
        chunk, remaining = remaining[:chunk_len], remaining[chunk_len:]
        all_frames.extend(decoder.feed(chunk))
        total_samples_in += len(chunk)
    check('irregular chunking: every emitted frame is exactly SAMPLES_PER_FRAME',
          all(len(f) == SAMPLES_PER_FRAME for f in all_frames), f'sizes={set(len(f) for f in all_frames)}')
    check('irregular chunking: emits the expected number of full frames',
          len(all_frames) == 10, f'got {len(all_frames)}')

    # 6. Amplitude round-trip sanity: a near-full-scale tone should decode to
    #    a near-1.0 peak amplitude, not something wildly off (wrong divisor,
    #    wrong endianness, etc.).
    decoder = PcmStreamDecoder(source_rate=SAMPLE_RATE, channels=1)
    payload = _sine_pcm16(SAMPLES_PER_FRAME * 2, SAMPLE_RATE, amplitude=0.99)
    frames = decoder.feed(payload)
    peak = np.max(np.abs(np.concatenate(frames))) if frames else -1.0
    check('amplitude round-trip: ~0.99 full-scale tone decodes to a comparable peak', 0.9 < peak < 1.0, f'peak={peak}')

    print(f'--- {("ALL PASSED" if _failures == 0 else f"{_failures} FAILURE(S)")} ---')
    return 1 if _failures else 0


def _hello(sample_rate: int, channels: int, fmt: str = 'pcm_s16le') -> str:
    return json.dumps({
        'type': 'hello',
        'format': fmt,
        'sample_rate': sample_rate,
        'channels': channels,
        'client': 'ingest_smoke_test/1.0',
    })


def run_wav(host: str, port: int, path: str) -> int:
    from websockets.sync.client import connect

    with wave.open(path, 'rb') as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        rate = wf.getframerate()
        n_frames = wf.getnframes()
        pcm = wf.readframes(n_frames)

    if sample_width != 2:
        print(f'[FAIL] WAV file must be 16-bit PCM (sampwidth=2), got sampwidth={sample_width}')
        return 1

    uri = f'ws://{host}:{port}/ingest'
    print(f'Connecting to {uri} ({path}: {rate} Hz, {channels} ch, {len(pcm)} bytes)')
    chunk_bytes = int(rate * channels * sample_width * 0.1)  # ~100ms per frame
    chunk_bytes -= chunk_bytes % (channels * sample_width)  # keep sample-aligned, not required but tidy

    with connect(uri, open_timeout=10) as ws:
        ws.send(_hello(rate, channels))
        ready_raw = ws.recv(timeout=10)
        ready = json.loads(ready_raw)
        check('server replied with ready', ready.get('type') == 'ready', f'got {ready}')
        print(f'ready: {ready}')

        offset = 0
        sent_bytes = 0
        while offset < len(pcm):
            piece = pcm[offset:offset + chunk_bytes]
            ws.send(piece)
            sent_bytes += len(piece)
            offset += chunk_bytes
            time.sleep(0.1)
        ws.close(1000, 'end of file')
    check('entire WAV payload was sent', sent_bytes == len(pcm), f'{sent_bytes} != {len(pcm)}')
    print('Done. Check the running server / SSE output for transcription results (AC-S3 is verified there).')
    return 1 if _failures else 0


def run_misbehave(host: str, port: int, case: str) -> int:
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed

    uri = f'ws://{host}:{port}/ingest'

    if case == 'bad-format':
        with connect(uri, open_timeout=10) as ws:
            ws.send(_hello(SAMPLE_RATE, 1, fmt='opus'))
            try:
                ws.recv(timeout=5)
                check('bad-format: connection closed by server', False, 'recv succeeded, expected close')
            except ConnectionClosed:
                check('bad-format: server closed with 1003', ws.close_code == 1003, f'close_code={ws.close_code}')

    elif case == 'binary-first':
        with connect(uri, open_timeout=10) as ws:
            ws.send(b'\x00\x01\x02\x03')
            try:
                ws.recv(timeout=5)
                check('binary-first: connection closed by server', False, 'recv succeeded, expected close')
            except ConnectionClosed:
                check('binary-first: server closed with 1003', ws.close_code == 1003, f'close_code={ws.close_code}')

    elif case == 'double-client':
        with connect(uri, open_timeout=10) as ws_a:
            ws_a.send(_hello(SAMPLE_RATE, 1))
            ready_a = json.loads(ws_a.recv(timeout=5))
            check('double-client: first client got ready', ready_a.get('type') == 'ready')

            with connect(uri, open_timeout=10) as ws_b:
                # The server may close as soon as it reads the request, i.e.
                # before or during our send() -- both send() and recv() can
                # observe the resulting ConnectionClosedError.
                try:
                    ws_b.send(_hello(SAMPLE_RATE, 1))
                    ws_b.recv(timeout=5)
                    check('double-client: second client closed by server', False, 'recv succeeded, expected close')
                except ConnectionClosed:
                    check('double-client: second client closed with 1008', ws_b.close_code == 1008,
                          f'close_code={ws_b.close_code}')

            # First client must be undisturbed: it can still stream audio.
            ws_a.send(_sine_pcm16(SAMPLES_PER_FRAME, SAMPLE_RATE))
            check('double-client: first client still open after second was rejected', ws_a.close_code is None,
                  f'close_code={ws_a.close_code}')

    elif case == 'abrupt-drop':
        with connect(uri, open_timeout=10) as ws_a:
            ws_a.send(_hello(SAMPLE_RATE, 1))
            ready_a = json.loads(ws_a.recv(timeout=5))
            check('abrupt-drop: first client got ready', ready_a.get('type') == 'ready')
            ws_a.send(_sine_pcm16(SAMPLES_PER_FRAME, SAMPLE_RATE))
            # Simulate a dead Wi-Fi link: kill the socket, skip the close handshake.
            ws_a.close_socket()

        time.sleep(0.5)  # give the server a moment to notice the drop and release the session slot

        with connect(uri, open_timeout=10) as ws_b:
            ws_b.send(_hello(SAMPLE_RATE, 1))
            ready_b = json.loads(ws_b.recv(timeout=5))
            check('abrupt-drop: server accepted a new session after the abrupt drop',
                  ready_b.get('type') == 'ready', f'got {ready_b}')
            print('NOTE: this only proves the server keeps listening. Whether the pipeline\'s')
            print('output_queue avoided the None sentinel must be confirmed by watching the')
            print('main.py process (Terminal A) stay alive and keep transcribing afterward.')

    else:
        print(f'Unknown --misbehave case: {case}')
        return 1

    print(f'--- {("ALL PASSED" if _failures == 0 else f"{_failures} FAILURE(S)")} ---')
    return 1 if _failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--unit', action='store_true', help='Run decoder-only unit checks, no socket.')
    parser.add_argument('--wav', type=str, default=None, help='Path to a 16-bit PCM WAV file to stream.')
    parser.add_argument('--misbehave',
                        type=str,
                        default=None,
                        choices=['bad-format', 'binary-first', 'double-client', 'abrupt-drop'],
                        help='Run one negative-path case against a running server.')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Ingest server host (for --wav/--misbehave).')
    parser.add_argument('--port', type=int, default=8766, help='Ingest server port (for --wav/--misbehave).')
    args = parser.parse_args()

    if args.unit:
        return run_unit()
    if args.wav:
        return run_wav(args.host, args.port, args.wav)
    if args.misbehave:
        return run_misbehave(args.host, args.port, args.misbehave)

    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
