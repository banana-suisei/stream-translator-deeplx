"""WebSocket PCM audio ingest server.

Implements the wire protocol owned by the parent task's design.md:
  - endpoint: ws://<host>:<port>/ingest
  - client sends one JSON "hello" text message, server replies "ready"
  - client then streams raw PCM s16le binary frames, any payload size
  - close codes: 1000 normal, 1003 unsupported data, 1008 policy (second
    session), 1011 internal error

This module owns the WebSocket/protocol concern. `audio_getter.NetworkAudioGetter`
is a thin adapter that only knows about `output_queue`.
"""

import json
import queue
import threading

import numpy as np
from scipy.signal import resample
from websockets.sync.server import serve

from .common import SAMPLE_RATE, SAMPLES_PER_FRAME, INFO, WARNING

SUPPORTED_FORMAT = 'pcm_s16le'
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000
SUPPORTED_CHANNELS = (1, 2)

# Close codes, per parent design.md section 3.6.
CLOSE_NORMAL = 1000
CLOSE_UNSUPPORTED_DATA = 1003
CLOSE_POLICY_VIOLATION = 1008
CLOSE_INTERNAL_ERROR = 1011


class PcmStreamDecoder:
    """Pure, I/O-free conversion of raw PCM bytes into pipeline-ready frames.

    Converts s16le bytes into exact `SAMPLES_PER_FRAME`-sized float32 mono
    chunks at `SAMPLE_RATE`, matching the contract every audio getter must
    satisfy (Silero VAD requires exactly 512 samples per call).
    """

    def __init__(self, source_rate: int, channels: int) -> None:
        self.source_rate = source_rate
        self.channels = channels
        self._odd_byte = b''  # a single leftover byte when a payload splits a sample
        self._buffer = np.array([], dtype=np.float32)

    def feed(self, payload: bytes) -> list[np.ndarray]:
        """Consume one incoming payload, return zero or more 512-sample frames."""
        if not payload:
            return []

        data = self._odd_byte + payload
        if len(data) % 2 != 0:
            self._odd_byte = data[-1:]
            data = data[:-1]
        else:
            self._odd_byte = b''

        if not data:
            return []

        audio = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0

        if self.channels == 2:
            # Drop a trailing unpaired sample, if any, rather than crash on reshape.
            usable_len = (len(audio) // 2) * 2
            audio = audio[:usable_len].reshape(-1, 2).mean(axis=1)

        if self.source_rate != SAMPLE_RATE and len(audio) > 0:
            target_len = int(len(audio) * SAMPLE_RATE / self.source_rate)
            audio = resample(audio, target_len).astype(np.float32)

        self._buffer = np.concatenate((self._buffer, audio))

        frames = []
        while len(self._buffer) >= SAMPLES_PER_FRAME:
            frames.append(self._buffer[:SAMPLES_PER_FRAME].astype(np.float32))
            self._buffer = self._buffer[SAMPLES_PER_FRAME:]
        return frames


def _is_loopback(host: str) -> bool:
    return host in ('127.0.0.1', 'localhost', '::1')


def _validate_hello(message) -> tuple[dict | None, str | None]:
    """Return (hello_dict, None) if valid, else (None, error_reason)."""
    if not isinstance(message, str):
        return None, 'binary frame received before hello'
    try:
        hello = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None, 'hello is not valid JSON'
    if not isinstance(hello, dict):
        return None, 'hello is not a JSON object'
    if hello.get('type') != 'hello':
        return None, 'first message type is not "hello"'
    if hello.get('format') != SUPPORTED_FORMAT:
        return None, f'unsupported format: {hello.get("format")!r}'
    sample_rate = hello.get('sample_rate')
    if not isinstance(sample_rate, int) or isinstance(sample_rate,
                                                      bool) or not (MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE):
        return None, f'invalid sample_rate: {hello.get("sample_rate")!r}'
    channels = hello.get('channels')
    if channels not in SUPPORTED_CHANNELS:
        return None, f'invalid channels: {hello.get("channels")!r}'
    return hello, None


class AudioIngestServer:
    """Owns the WebSocket listener and enforces the single-active-session rule.

    A client disconnect must never end the pipeline (see NetworkAudioGetter):
    this class only ever puts decoded audio frames on `output_queue`. It never
    puts the `None` end-of-stream sentinel; that is exclusively the getter's
    responsibility, and only on process shutdown.
    """

    def __init__(self, host: str, port: int, output_queue: 'queue.SimpleQueue[np.array]', path: str = '/ingest'):
        self.host = host
        self.port = port
        self.output_queue = output_queue
        self.path = path if path.startswith('/') else f'/{path}'
        self._server = None
        self._serve_thread = None
        self._session_lock = threading.Lock()
        self._session_active = False

    def start(self) -> None:
        # Binds synchronously, matching LocalSSEServer's _create_httpd pattern.
        self._server = serve(self._handler, self.host, self.port)
        if not _is_loopback(self.host):
            print(f'{WARNING}Audio ingest is bound to {self.host}, which is not loopback-only. '
                  'The ingest port has no authentication and accepts audio in cleartext; '
                  'do not expose it to an untrusted network.')
        print(f'{INFO}Audio ingest listening on ws://{self.host}:{self.port}{self.path}')
        # start_daemon_thread is intentionally not reused here: this thread must
        # be joinable via wait_closed() so NetworkAudioGetter.loop() can block on
        # it exactly like the other getters block on their own I/O loops.
        self._serve_thread = threading.Thread(target=self._server.serve_forever)
        self._serve_thread.daemon = True
        self._serve_thread.start()

    def wait_closed(self) -> None:
        if self._serve_thread is not None:
            self._serve_thread.join()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def _try_claim_session(self) -> bool:
        with self._session_lock:
            if self._session_active:
                return False
            self._session_active = True
            return True

    def _release_session(self) -> None:
        with self._session_lock:
            self._session_active = False

    def _handler(self, websocket) -> None:
        remote = websocket.remote_address
        path = getattr(getattr(websocket, 'request', None), 'path', self.path)
        if path.split('?', 1)[0] != self.path:
            print(f'{WARNING}Ingest: rejecting connection from {remote} for unknown path {path!r}')
            websocket.close(CLOSE_UNSUPPORTED_DATA, 'unknown path')
            return

        if not self._try_claim_session():
            print(f'{WARNING}Ingest: rejecting {remote}, a session is already active')
            websocket.close(CLOSE_POLICY_VIOLATION, 'another ingest session is already active')
            return

        try:
            self._run_session(websocket, remote)
        finally:
            # Only the session slot is released here. The pipeline's `None`
            # end-of-stream sentinel is NEVER put on output_queue for a client
            # disconnect: AudioSlicer forwards None downstream and tears the
            # whole pipeline down permanently. A dropped phone connection must
            # only end this session, not the process. See NetworkAudioGetter.
            self._release_session()

    def _run_session(self, websocket, remote) -> None:
        try:
            first_message = websocket.recv()
        except Exception:
            return

        hello, error = _validate_hello(first_message)
        if error is not None:
            print(f'{WARNING}Ingest: closing {remote}, bad hello: {error}')
            websocket.close(CLOSE_UNSUPPORTED_DATA, error)
            return

        sample_rate = hello['sample_rate']
        channels = hello['channels']
        client_name = hello.get('client', 'unknown')
        resampling = sample_rate != SAMPLE_RATE

        ready = {
            'type': 'ready',
            'sample_rate': SAMPLE_RATE,
            'samples_per_frame': SAMPLES_PER_FRAME,
            'resampling': resampling,
        }
        try:
            websocket.send(json.dumps(ready))
        except Exception as e:
            print(f'{WARNING}Ingest: failed to send ready to {remote}: {e}')
            return

        print(f'{INFO}Ingest: session started from {remote} (client={client_name!r}, '
              f'sample_rate={sample_rate}, channels={channels}, resampling={resampling})')

        decoder = PcmStreamDecoder(source_rate=sample_rate, channels=channels)
        try:
            for message in websocket:
                if isinstance(message, str):
                    # No control frames are defined beyond the initial hello.
                    continue
                for frame in decoder.feed(message):
                    self.output_queue.put(frame)
        except Exception as e:
            print(f'{WARNING}Ingest: session from {remote} ended abnormally: {e}')
        finally:
            print(f'{INFO}Ingest: session from {remote} ended')
