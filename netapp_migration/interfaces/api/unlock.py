"""Local unlock channel for a service-managed API.

The API can start **locked**: uvicorn binds its port immediately, but every
endpoint answers 503 until a super admin supplies the global token. That
token then arrives through a unix domain socket readable only by the service
account and root — never on disk, never in argv, never in the environment.

Why a socket rather than a console prompt: a service started by systemd has
no terminal an administrator connected over SSH can type into. Reading the
token from /dev/console blocks forever, invisibly, and the port is never
bound. A socket lets the service come up straight away and be unlocked from
any admin session, while keeping the property that matters — a human must
present the global token at every start, and nothing persists it.

Wire protocol, one exchange per connection:

    -> "<global token>\\n"
    <- "OK <n> delegated token(s)\\n"   or   "ERR <message>\\n"
"""

import logging
import os
import socket
import threading
from typing import Optional

from ...models import AuthError
from ...security.tokens import TokenStore

# A token far beyond any sane length: refuse rather than read unbounded.
MAX_TOKEN_BYTES = 4096
CONNECTION_TIMEOUT = 30.0


def default_socket_path(store_path: Optional[str] = None) -> str:
    """Where the unlock socket lives: beside the token store by default.

    That directory is already the private one (mode 700, owned by the
    service account), so the socket inherits a sensible location on both a
    system install and an unprivileged one.
    """
    explicit = os.environ.get("NETAPP_UNLOCK_SOCKET")
    if explicit:
        return explicit
    store = store_path or TokenStore().path
    return os.path.join(os.path.dirname(os.path.abspath(store)),
                        "unlock.sock")


class UnlockListener(threading.Thread):
    """Serves unlock requests for one TokenStore until stopped."""

    def __init__(self, store: TokenStore, path: str,
                 logger: Optional[logging.Logger] = None):
        super().__init__(name="unlock-listener", daemon=True)
        self.store = store
        self.path = path
        self.log = logger or logging.getLogger(__name__)
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()

    # -- life cycle ---------------------------------------------------------
    def bind(self) -> None:
        """Create the socket before the server starts, so a failure here is
        reported at startup rather than at the first unlock attempt."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, mode=0o700, exist_ok=True)

        # A socket left behind by a killed process would block bind().
        if os.path.exists(self.path):
            os.unlink(self.path)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # umask, not a later chmod: the socket must never exist world-writable,
        # not even for the instant between bind() and chmod().
        previous = os.umask(0o177)
        try:
            sock.bind(self.path)
        finally:
            os.umask(previous)
        os.chmod(self.path, 0o600)
        sock.listen(1)
        sock.settimeout(1.0)          # so stop() is honoured promptly
        self._sock = sock

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass

    # -- serving ------------------------------------------------------------
    def run(self) -> None:
        if self._sock is None:
            self.bind()
        while not self._stop.is_set():
            try:
                connection, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break                  # socket closed by stop()
            with connection:
                try:
                    connection.settimeout(CONNECTION_TIMEOUT)
                    self._serve_one(connection)
                except (OSError, socket.timeout):
                    continue

    def _serve_one(self, connection: socket.socket) -> None:
        buffer = b""
        while b"\n" not in buffer and len(buffer) < MAX_TOKEN_BYTES:
            chunk = connection.recv(1024)
            if not chunk:
                break
            buffer += chunk

        if b"\n" not in buffer and len(buffer) >= MAX_TOKEN_BYTES:
            connection.sendall(b"ERR token too long\n")
            return
        token = buffer.split(b"\n", 1)[0].decode("utf-8", "replace")
        if not token.strip():
            connection.sendall(b"ERR empty token\n")
            return

        if self.store.unlocked:
            connection.sendall(b"OK already unlocked\n")
            return

        try:
            self.store.unlock(token.strip())
        except AuthError as exc:
            self.log.warning("unlock refused: %s", exc.message)
            connection.sendall(f"ERR {exc.message}\n".encode())
            return

        count = len(self.store.list_scopes())
        self.log.info("token store unlocked through %s "
                      "(%d delegated token(s))", self.path, count)
        connection.sendall(f"OK unlocked, {count} delegated token(s)\n"
                           .encode())


def request_unlock(path: str, token: str, timeout: float = 10.0) -> str:
    """Client side: hand the token over and return the server's answer.

    Raises OSError when the socket cannot be reached — which is the normal
    outcome when the API is not running, or when the caller lacks the
    permissions to open it.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(token.encode("utf-8") + b"\n")
        answer = b""
        while b"\n" not in answer and len(answer) < 1024:
            chunk = sock.recv(256)
            if not chunk:
                break
            answer += chunk
    return answer.decode("utf-8", "replace").strip()
