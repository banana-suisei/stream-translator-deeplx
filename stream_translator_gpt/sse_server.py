import json
import queue
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .common import start_daemon_thread


class LocalSSEServer:

    def __init__(self, host: str, port: int, path: str = '/events') -> None:
        self.host = host or '127.0.0.1'
        self.port = int(port)
        self.path = path if path.startswith('/') else f'/{path}'
        self.health_path = '/health'
        self._client_queues = set()
        self._client_lock = threading.Lock()
        self._closed = threading.Event()
        self._httpd = self._create_httpd()
        self._thread = start_daemon_thread(self._httpd.serve_forever, poll_interval=0.5)

    def _create_httpd(self):
        server = self

        class SSEHTTPServer(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        class Handler(BaseHTTPRequestHandler):

            def do_OPTIONS(self):
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Cache-Control, Last-Event-ID')
                self.end_headers()

            def do_GET(self):
                if self.path == server.health_path:
                    payload = json.dumps(server.get_health(), ensure_ascii=False).encode('utf-8')
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if self.path != server.path:
                    payload = json.dumps(
                        {
                            'message': 'SSE endpoint is ready.',
                            'events_url': server.path,
                            'health_url': server.health_path,
                        },
                        ensure_ascii=False,
                    ).encode('utf-8')
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Cache-Control', 'no-cache')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                self.send_response(HTTPStatus.OK)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()

                client_queue = queue.Queue()
                server._register_client(client_queue)
                try:
                    ready_event = server.format_sse(
                        event='ready',
                        data={
                            'connected_at': datetime.now(timezone.utc).isoformat(),
                            'path': server.path,
                        },
                    )
                    self.wfile.write(ready_event.encode('utf-8'))
                    self.wfile.flush()
                    while not server._closed.is_set():
                        try:
                            message = client_queue.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b': keep-alive\n\n')
                            self.wfile.flush()
                            continue
                        if message is None:
                            break
                        self.wfile.write(message.encode('utf-8'))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    server._unregister_client(client_queue)

            def log_message(self, format, *args):
                return

        return SSEHTTPServer((self.host, self.port), Handler)

    def _register_client(self, client_queue: queue.Queue):
        with self._client_lock:
            self._client_queues.add(client_queue)

    def _unregister_client(self, client_queue: queue.Queue):
        with self._client_lock:
            self._client_queues.discard(client_queue)

    def format_sse(self, event: str, data: dict, event_id: int | None = None):
        lines = []
        if event_id is not None:
            lines.append(f'id: {event_id}')
        if event:
            lines.append(f'event: {event}')
        payload = json.dumps(data, ensure_ascii=False)
        for line in payload.splitlines() or ['']:
            lines.append(f'data: {line}')
        return '\n'.join(lines) + '\n\n'

    def broadcast(self, event: str, data: dict, event_id: int | None = None):
        message = self.format_sse(event=event, data=data, event_id=event_id)
        with self._client_lock:
            client_queues = list(self._client_queues)
        for client_queue in client_queues:
            client_queue.put(message)

    def get_health(self):
        with self._client_lock:
            client_count = len(self._client_queues)
        return {
            'ok': True,
            'host': self.host,
            'port': self.port,
            'path': self.path,
            'clients': client_count,
        }

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        close_message = self.format_sse(
            event='close',
            data={
                'closed_at': datetime.now(timezone.utc).isoformat(),
            },
        )
        with self._client_lock:
            client_queues = list(self._client_queues)
        for client_queue in client_queues:
            client_queue.put(close_message)
            client_queue.put(None)
        self._httpd.shutdown()
        self._httpd.server_close()
