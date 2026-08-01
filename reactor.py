"""
Selector-based event loop (Reactor pattern).

Uses ``selectors.DefaultSelector`` which automatically picks the most
efficient implementation for the platform (``epoll`` on Linux, ``kqueue``
on macOS).

The reactor's job:
  1. Accept new client connections from the listening socket.
  2. Register client and backend sockets with the appropriate events.
  3. Dispatch I/O readiness callbacks to ``ProxyConnection`` instances.
  4. Reap completed connections.
"""

from __future__ import annotations

import selectors
import socket
from typing import TYPE_CHECKING

import config
from connection import ProxyConnection, State
from logger import get_logger

if TYPE_CHECKING:
    from router import RoundRobinRouter, LeastConnectionsRouter

log = get_logger()


class Reactor:
    """Single-threaded, non-blocking event loop driving the load balancer."""

    def __init__(
        self,
        router: RoundRobinRouter | LeastConnectionsRouter,
        host: str = config.LISTEN_HOST,
        port: int = config.LISTEN_PORT,
        *,
        require_reuseport: bool = False,
    ) -> None:
        self._router = router
        self._host = host
        self._port = port
        self._require_reuseport = require_reuseport
        self._sel = selectors.DefaultSelector()
        self._server_sock: socket.socket | None = None
        self._connections: dict[int, ProxyConnection] = {}  # fd → conn
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Bind, listen, and enter the event loop."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
        )
        if hasattr(socket, "SO_REUSEPORT"):
            self._server_sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEPORT, 1
            )
        elif self._require_reuseport:
            raise RuntimeError(
                "SO_REUSEPORT is required for --workers > 1 but is not "
                "available on this platform"
            )
        self._server_sock.setblocking(False)
        self._server_sock.bind((self._host, self._port))
        self._server_sock.listen(config.LISTEN_BACKLOG)

        self._sel.register(
            self._server_sock, selectors.EVENT_READ, data="accept"
        )
        log.info(
            "Reactor listening on %s:%d  (selector=%s)",
            self._host, self._port,
            type(self._sel).__name__,
        )

        self._running = True
        try:
            self._loop()
        except KeyboardInterrupt:
            log.info("Shutting down…")
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._running = False

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            events = self._sel.select(timeout=1.0)
            for key, mask in events:
                if key.data == "accept":
                    self._accept()
                else:
                    conn: ProxyConnection = key.data
                    fd = key.fileobj.fileno() if hasattr(key.fileobj, 'fileno') else -1
                    try:
                        self._dispatch(conn, key.fileobj, mask)
                    except Exception:
                        log.exception("Unhandled error in connection %s", conn.client_addr)
                        conn.state = State.DONE

            # Reap finished connections
            self._reap()

    # ── Accept ────────────────────────────────────────────────────────────

    def _accept(self) -> None:
        """Accept all pending client connections in a burst."""
        while True:
            try:
                client_sock, client_addr = self._server_sock.accept()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break

            client_sock.setblocking(False)
            conn = ProxyConnection(client_sock, client_addr, self._router)

            # Register client socket for reading (initial request)
            self._sel.register(
                client_sock, selectors.EVENT_READ, data=conn
            )
            self._connections[client_sock.fileno()] = conn

    # ── Dispatch ──────────────────────────────────────────────────────────

    def _dispatch(
        self, conn: ProxyConnection, fileobj, mask: int
    ) -> None:
        """Route I/O events to the correct handler based on state."""
        sock = fileobj

        # Determine if this event is for the client or backend socket
        is_client = sock is conn.client_sock
        is_backend = sock is conn.backend_sock

        if is_client and (mask & selectors.EVENT_READ):
            prev_state = conn.state
            conn.handle_client_readable()
            self._transition(conn, prev_state)

        if is_client and (mask & selectors.EVENT_WRITE):
            prev_state = conn.state
            conn.handle_client_writable()
            self._transition(conn, prev_state)

        if is_backend and (mask & selectors.EVENT_READ):
            prev_state = conn.state
            conn.handle_backend_readable()
            self._transition(conn, prev_state)

        if is_backend and (mask & selectors.EVENT_WRITE):
            prev_state = conn.state
            conn.handle_backend_writable()
            self._transition(conn, prev_state)

        # Retry path: unregister the failed backend FD even if state
        # stayed CONNECTING_TO_BACKEND (same-state transition skip).
        self._flush_pending_unregister(conn)

    def _flush_pending_unregister(self, conn: ProxyConnection) -> None:
        """Unregister a closed backend socket left behind by a retry."""
        old = conn.pending_backend_unregister
        if old is None:
            return
        try:
            self._sel.unregister(old)
        except (KeyError, ValueError):
            pass
        conn.pending_backend_unregister = None

        # New backend may already be in CONNECTING/FORWARDING without a
        # state change that would have registered it.
        if conn.backend_sock is not None and conn.state in (
            State.CONNECTING_TO_BACKEND,
            State.FORWARDING_REQUEST,
        ):
            try:
                self._sel.register(
                    conn.backend_sock,
                    selectors.EVENT_WRITE,
                    data=conn,
                )
            except (KeyError, ValueError):
                try:
                    self._sel.modify(
                        conn.backend_sock,
                        selectors.EVENT_WRITE,
                        data=conn,
                    )
                except (KeyError, ValueError):
                    pass

    # ── State transition → selector updates ───────────────────────────────

    def _transition(self, conn: ProxyConnection, prev_state: State) -> None:
        """Update selector registrations when a connection changes state."""
        # Always clear a pending unregister before re-registering
        if conn.pending_backend_unregister is not None:
            self._flush_pending_unregister(conn)

        if conn.state == prev_state:
            return

        if conn.state == State.CONNECTING_TO_BACKEND:
            # Register backend socket for writability (connect completion)
            if conn.backend_sock is not None:
                try:
                    self._sel.register(
                        conn.backend_sock,
                        selectors.EVENT_WRITE,
                        data=conn,
                    )
                except (KeyError, ValueError):
                    pass

        elif conn.state == State.FORWARDING_REQUEST:
            # Backend connected — keep monitoring for write readiness
            if conn.backend_sock is not None:
                try:
                    self._sel.modify(
                        conn.backend_sock,
                        selectors.EVENT_WRITE,
                        data=conn,
                    )
                except (KeyError, ValueError):
                    try:
                        self._sel.register(
                            conn.backend_sock,
                            selectors.EVENT_WRITE,
                            data=conn,
                        )
                    except (KeyError, ValueError):
                        pass

        elif conn.state == State.READING_RESPONSE:
            # Done writing to backend — switch to reading
            if conn.backend_sock is not None:
                try:
                    self._sel.modify(
                        conn.backend_sock,
                        selectors.EVENT_READ,
                        data=conn,
                    )
                except (KeyError, ValueError):
                    pass

        elif conn.state == State.FORWARDING_RESPONSE:
            # We have the full response — make client writable
            try:
                self._sel.modify(
                    conn.client_sock,
                    selectors.EVENT_WRITE,
                    data=conn,
                )
            except (KeyError, ValueError):
                pass
            # Unregister backend (done with it)
            if conn.backend_sock is not None:
                try:
                    self._sel.unregister(conn.backend_sock)
                except (KeyError, ValueError):
                    pass

    # ── Reap completed connections ────────────────────────────────────────

    def _reap(self) -> None:
        dead_fds = [
            fd for fd, conn in self._connections.items()
            if conn.state == State.DONE
        ]
        for fd in dead_fds:
            conn = self._connections.pop(fd)
            # Unregister from selector
            for sock in (conn.client_sock, conn.backend_sock):
                if sock is not None:
                    try:
                        self._sel.unregister(sock)
                    except (KeyError, ValueError):
                        pass
            conn.cleanup()

    # ── Shutdown ──────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        for conn in self._connections.values():
            conn.cleanup()
        self._connections.clear()
        if self._server_sock:
            try:
                self._sel.unregister(self._server_sock)
            except (KeyError, ValueError):
                pass
            self._server_sock.close()
        self._sel.close()
        log.info("Reactor shut down.")
