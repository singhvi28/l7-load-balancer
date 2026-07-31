"""
Per-connection state machine and proxy logic.

Every active proxy session (client ↔ LB ↔ backend) is represented by a
``ProxyConnection`` instance.  The connection progresses through a series
of states::

    READING_REQUEST → CONNECTING_TO_BACKEND → FORWARDING_REQUEST
                    → READING_RESPONSE → FORWARDING_RESPONSE → DONE

All socket I/O is non-blocking.  Partial reads/writes are handled by
internal byte buffers (``collections.deque`` of ``bytes`` objects).
"""

from __future__ import annotations

import errno
import socket
import time
from collections import deque
from enum import Enum, auto
from typing import Optional, Tuple, TYPE_CHECKING

import config
from http_parser import (
    HttpRequest,
    inject_forwarded_for,
    parse_response_status,
    serialise_request,
    try_parse_request,
    get_response_content_length,
    HEADER_DELIM,
)
from logger import access_log, get_logger

if TYPE_CHECKING:
    from router import RoundRobinRouter, LeastConnectionsRouter

log = get_logger()

Backend = Tuple[str, int]


class State(Enum):
    READING_REQUEST = auto()
    CONNECTING_TO_BACKEND = auto()
    FORWARDING_REQUEST = auto()
    READING_RESPONSE = auto()
    FORWARDING_RESPONSE = auto()
    DONE = auto()


class ProxyConnection:
    """Manages the full lifecycle of a single proxied HTTP request."""

    __slots__ = (
        "client_sock", "client_addr",
        "backend_sock", "backend_addr",
        "state",
        "request_buf",          # raw bytes accumulator for the client request
        "parsed_request",
        "forward_buf",          # bytes to send to backend
        "forward_offset",
        "response_buf",         # raw bytes accumulator for the backend response
        "relay_buf",            # bytes to send back to client
        "relay_offset",
        "start_time",
        "response_status",
        "_router",
        "_response_headers_done",
        "_response_content_length",
        "_response_body_received",
    )

    def __init__(
        self,
        client_sock: socket.socket,
        client_addr: Tuple[str, int],
        router: RoundRobinRouter | LeastConnectionsRouter,
    ) -> None:
        self.client_sock = client_sock
        self.client_addr = client_addr
        self.backend_sock: Optional[socket.socket] = None
        self.backend_addr: Optional[Backend] = None
        self.state = State.READING_REQUEST
        self._router = router

        # Buffers
        self.request_buf = bytearray()
        self.parsed_request: Optional[HttpRequest] = None
        self.forward_buf = b""
        self.forward_offset = 0
        self.response_buf = bytearray()
        self.relay_buf = b""
        self.relay_offset = 0

        self.start_time = time.monotonic()
        self.response_status: Optional[int] = None

        self._response_headers_done = False
        self._response_content_length = -1
        self._response_body_received = 0

    # ── State handlers ────────────────────────────────────────────────────

    def handle_client_readable(self) -> None:
        """Called by the reactor when the client socket is readable."""
        if self.state != State.READING_REQUEST:
            return
        try:
            data = self.client_sock.recv(config.CLIENT_RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.state = State.DONE
            return

        if not data:
            # Client closed before sending a complete request
            self.state = State.DONE
            return

        self.request_buf.extend(data)

        if len(self.request_buf) > config.MAX_REQUEST_SIZE:
            self._send_error(413, "Request Entity Too Large")
            return

        # Try to parse the accumulated buffer
        req = try_parse_request(bytes(self.request_buf))
        if req is None:
            return  # need more data

        self.parsed_request = req
        inject_forwarded_for(req, self.client_addr[0])
        self.forward_buf = serialise_request(req)
        self.forward_offset = 0

        # Pick a backend
        backend = self._router.next_backend()
        if backend is None:
            self._send_error(503, "Service Unavailable")
            return

        self.backend_addr = backend
        self._initiate_backend_connect(backend)

    def _initiate_backend_connect(self, backend: Backend) -> None:
        """Start a non-blocking connect to the selected backend."""
        self.state = State.CONNECTING_TO_BACKEND
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        self.backend_sock = sock

        err = sock.connect_ex(backend)
        if err == 0:
            # Immediate connect (unlikely but possible on localhost)
            self.state = State.FORWARDING_REQUEST
        elif err in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN, 115):
            # Connection in progress — reactor will monitor for writability
            pass
        else:
            log.error("connect_ex to %s:%d failed: errno=%d", *backend, err)
            self._send_error(502, "Bad Gateway")

    def handle_backend_writable(self) -> None:
        """Called when the backend socket becomes writable."""
        if self.state == State.CONNECTING_TO_BACKEND:
            # Check if connect succeeded
            err = self.backend_sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_ERROR
            )
            if err != 0:
                log.error(
                    "Backend connect to %s:%d failed: errno=%d",
                    *self.backend_addr, err,
                )
                self._send_error(502, "Bad Gateway")
                return
            self.state = State.FORWARDING_REQUEST

        if self.state != State.FORWARDING_REQUEST:
            return

        # Send as much of the request as we can
        try:
            sent = self.backend_sock.send(
                self.forward_buf[self.forward_offset:]
            )
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._send_error(502, "Bad Gateway")
            return

        self.forward_offset += sent
        if self.forward_offset >= len(self.forward_buf):
            # Entire request forwarded
            self.state = State.READING_RESPONSE

    def handle_backend_readable(self) -> None:
        """Called when the backend socket has response data available."""
        if self.state != State.READING_RESPONSE:
            return

        try:
            data = self.backend_sock.recv(config.BACKEND_RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._send_error(502, "Bad Gateway")
            return

        if not data:
            # Backend closed — whatever we have is the full response
            self._prepare_relay()
            return

        self.response_buf.extend(data)

        # Parse status from first chunk if we haven't yet
        if self.response_status is None:
            code, _ = parse_response_status(bytes(self.response_buf))
            if code is not None:
                self.response_status = code

        # Check if full response has been received
        if not self._response_headers_done:
            hdr_end = self.response_buf.find(HEADER_DELIM)
            if hdr_end != -1:
                self._response_headers_done = True
                header_len = hdr_end + len(HEADER_DELIM)
                self._response_content_length = get_response_content_length(
                    bytes(self.response_buf)
                )
                self._response_body_received = len(self.response_buf) - header_len

        if self._response_headers_done and self._response_content_length >= 0:
            if self._response_body_received >= self._response_content_length:
                self._prepare_relay()
                return

        # Track body bytes for subsequent chunks
        if self._response_headers_done:
            self._response_body_received = (
                len(self.response_buf)
                - self.response_buf.find(HEADER_DELIM)
                - len(HEADER_DELIM)
            )

    def _prepare_relay(self) -> None:
        """Transition to relaying the response back to the client."""
        self.relay_buf = bytes(self.response_buf)
        self.relay_offset = 0
        self.state = State.FORWARDING_RESPONSE

    def handle_client_writable(self) -> None:
        """Called when the client socket is writable (relay phase)."""
        if self.state != State.FORWARDING_RESPONSE:
            return

        try:
            sent = self.client_sock.send(self.relay_buf[self.relay_offset:])
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.state = State.DONE
            return

        self.relay_offset += sent
        if self.relay_offset >= len(self.relay_buf):
            self._finish()

    # ── Error responses ───────────────────────────────────────────────────

    def _send_error(self, code: int, reason: str) -> None:
        """Send a simple HTTP error response to the client and close."""
        body = f"<h1>{code} {reason}</h1>"
        response = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Content-Type: text/html\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        try:
            self.client_sock.sendall(response.encode("latin-1"))
        except OSError:
            pass
        self.response_status = code
        self._finish()

    # ── Cleanup ───────────────────────────────────────────────────────────

    def _finish(self) -> None:
        """Log the request and mark the connection for cleanup."""
        latency = (time.monotonic() - self.start_time) * 1000
        req = self.parsed_request

        if req and self.backend_addr:
            access_log(
                client_ip=self.client_addr[0],
                client_port=self.client_addr[1],
                backend_ip=self.backend_addr[0],
                backend_port=self.backend_addr[1],
                method=req.method,
                path=req.path,
                version=req.version,
                status=self.response_status or 0,
                latency_ms=latency,
            )

        # Notify router of disconnect (meaningful for Least-Connections)
        if self.backend_addr:
            self._router.on_disconnect(self.backend_addr)

        self.state = State.DONE

    def cleanup(self) -> None:
        """Close both sockets.  Safe to call multiple times."""
        for sock in (self.client_sock, self.backend_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
