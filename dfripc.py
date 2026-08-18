"""Control socket between the editor and the daemon.

Line-delimited JSON over a Unix socket. The editor uses it to push a config
for live preview on the real strip while you are still editing -- which is the
whole point of the app: you change a colour and the hardware changes under
your fingers, with nothing saved yet.

Requests arrive on a listener thread and are handed to the render loop through
a queue. Nothing outside that loop ever touches cairo or the DRM buffer, so a
misbehaving client cannot corrupt what is on screen.
"""

import json
import os
import queue
import socket
import threading

SOCKET_PATH = '/run/dfrd.sock'
FALLBACK_SOCKET = '/tmp/dfrd.sock'
ENCODING = 'utf-8'
MAX_MESSAGE = 4 * 1024 * 1024        # a whole config, comfortably


def socket_path():
    """Prefer /run; fall back to /tmp when /run is not writable."""
    if os.path.isdir('/run') and os.access('/run', os.W_OK):
        return SOCKET_PATH
    return FALLBACK_SOCKET


def _send_json(sock, payload):
    data = json.dumps(payload).encode(ENCODING) + b'\n'
    sock.sendall(data)


def _recv_json(sock):
    chunks = bytearray()
    while b'\n' not in chunks:
        block = sock.recv(65536)
        if not block:
            break
        chunks.extend(block)
        if len(chunks) > MAX_MESSAGE:
            raise ValueError('message too large')
    if not chunks:
        return None
    line, _, _ = bytes(chunks).partition(b'\n')
    return json.loads(line.decode(ENCODING))


class Server:
    """Accepts control connections and queues requests for the render loop."""

    def __init__(self, path=None, owner_uid=None, owner_gid=None):
        self.path = path or socket_path()
        self.owner = (owner_uid, owner_gid)
        self.requests = queue.Queue()
        self._sock = None
        self._thread = None
        self._running = False

    def start(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(8)
        os.chmod(self.path, 0o660)
        if self.owner[0] is not None and os.getuid() == 0:
            try:
                os.chown(self.path, self.owner[0], self.owner[1])
            except OSError:
                pass
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name='dfrd-ipc')
        self._thread.start()
        return self.path

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        reply_box = queue.Queue(maxsize=1)
        try:
            conn.settimeout(20)
            request = _recv_json(conn)
            if request is None:
                return
            self.requests.put((request, reply_box))
            try:
                reply = reply_box.get(timeout=15)
            except queue.Empty:
                reply = {'ok': False, 'error': 'daemon busy'}
            _send_json(conn, reply)
        except Exception as exc:                       # noqa: BLE001
            try:
                _send_json(conn, {'ok': False, 'error': str(exc)})
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def poll(self):
        """Yield pending (request, reply_box) pairs. Call from the loop."""
        while True:
            try:
                yield self.requests.get_nowait()
            except queue.Empty:
                return

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Client:
    """Thin synchronous client. Every call returns a dict with `ok`."""

    def __init__(self, path=None, timeout=6.0):
        self.path = path or socket_path()
        self.timeout = timeout

    def available(self):
        return os.path.exists(self.path)

    def call(self, command, **payload):
        if not self.available():
            return {'ok': False, 'error': 'daemon not running'}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.path)
                _send_json(sock, dict(payload, cmd=command))
                reply = _recv_json(sock)
                return reply if reply is not None else {'ok': False,
                                                        'error': 'no reply'}
        except (OSError, ValueError) as exc:
            return {'ok': False, 'error': str(exc)}

    # convenience wrappers -------------------------------------------------

    def ping(self):
        return self.call('ping')

    def status(self):
        return self.call('status')

    def reload(self):
        return self.call('reload')

    def preview(self, config, page=None):
        return self.call('preview', config=config, page=page)

    def clear_preview(self):
        return self.call('clear_preview')

    def set_page(self, page):
        return self.call('page', page=page)

    def screenshot(self):
        return self.call('screenshot')
