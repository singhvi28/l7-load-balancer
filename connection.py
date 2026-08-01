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
from enum import Enum, auto
from typing import Optional, Set, Tuple, TYPE_CHECKING

import config
from http_parser import (
    HttpRequest,
    inject_forwarded_for,
    parse_response_status,
    serialise_request,
    try_parse_request,
    get_response_content_length,
    is_chunked_response,
    try_consume_chunked_body,
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
        "request_buf",
        "parsed_request",
        "forward_buf",
        "forward_offset",
        "response_buf",
        "relay_buf",
        "relay_offset",
        "start_time",
        "response_status",
        "_router",
        "_response_headers_done",
        "_response_content_length",
        "_response_body_received",
        "_response_is_chunked",
        "_tried_backends",
        "_backend_attempts",
        "_client_bytes_sent",
        "_disconnect_notified",
        "pending_backend_unregister",
        "deadline",
        "deadline_gen",
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
        self._response_is_chunked = False

        self._tried_backends: Set[Backend] = set()
        self._backend_attempts = 0
        self._client_bytes_sent = False
        self._disconnect_notified = False
        # Socket the reactor must unregister before we open a retry connect
        self.pending_backend_unregister: Optional[socket.socket] = None

        self.deadline = 0.0
        self.deadline_gen = 0
        self.refresh_deadline()

    # ── Deadlines ─────────────────────────────────────────────────────────

    def _timeout_for_state(self) -> Optional[float]:
        if self.state == State.READING_REQUEST:
            return config.CLIENT_IDLE_TIMEOUT
        if self.state == State.CONNECTING_TO_BACKEND:
            return config.CONNECT_TIMEOUT
        if self.state in (State.FORWARDING_REQUEST, State.READING_RESPONSE):
            return config.BACKEND_TIMEOUT
        if self.state == State.FORWARDING_RESPONSE:
            return config.CLIENT_SEND_TIMEOUT
        return None

    def refresh_deadline(self) -> None:
        """Bump generation and set a new absolute deadline for the current state."""
        timeout = self._timeout_for_state()
        if timeout is None:
            return
        self.deadline_gen += 1
        self.deadline = time.monotonic() + timeout

    def timed_out(self) -> bool:
        return self.state != State.DONE and time.monotonic() >= self.deadline

    def handle_timeout(self) -> None:
        """Expire this connection — 408 / 504, or silent close if mid-relay."""
        if self.state == State.DONE:
            return
        log.warning(
            "Connection timeout in %s for %s",
            self.state.name, self.client_addr,
        )
        if self.state == State.READING_REQUEST:
            self._send_error(408, "Request Timeout")
            return
        if self._client_bytes_sent:
            # Already writing to client — just tear down
            if self.backend_addr and not self._disconnect_notified:
                self._router.on_disconnect(self.backend_addr)
                self._disconnect_notified = True
            self.state = State.DONE
            return
        self._send_error(504, "Gateway Timeout")

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
        self.refresh_deadline()

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
        self._backend_attempts = 1
        self._disconnect_notified = False
        self._initiate_backend_connect(backend)

    def _initiate_backend_connect(self, backend: Backend) -> None:
        """Start a non-blocking connect to the selected backend."""
        self.state = State.CONNECTING_TO_BACKEND
        self.refresh_deadline()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        self.backend_sock = sock

        err = sock.connect_ex(backend)
        if err == 0:
            # Immediate connect (unlikely but possible on localhost)
            self.state = State.FORWARDING_REQUEST
            self.refresh_deadline()
        elif err in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN, 115):
            # Connection in progress — reactor will monitor for writability
            pass
        else:
            log.error("connect_ex to %s:%d failed: errno=%d", *backend, err)
            self._fail_backend("connect_ex failed")

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
                self._fail_backend("connect failed")
                return
            self.state = State.FORWARDING_REQUEST
            self.refresh_deadline()

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
            self._fail_backend("send failed")
            return

        if sent:
            self.refresh_deadline()
        self.forward_offset += sent
        if self.forward_offset >= len(self.forward_buf):
            # Entire request forwarded
            self.state = State.READING_RESPONSE
            self.refresh_deadline()

    def handle_backend_readable(self) -> None:
        """Called when the backend socket has response data available."""
        if self.state != State.READING_RESPONSE:
            return

        try:
            data = self.backend_sock.recv(config.BACKEND_RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._fail_backend("recv failed")
            return

        if not data:
            # Backend closed — close-delimited body, or truncated chunked
            if self._response_is_chunked and self._response_headers_done:
                hdr_end = self.response_buf.find(HEADER_DELIM)
                body = (
                    bytes(self.response_buf[hdr_end + len(HEADER_DELIM):])
                    if hdr_end != -1
                    else b""
                )
                if try_consume_chunked_body(body) is None:
                    self._fail_backend("truncated chunked response")
                    return
            self._prepare_relay()
            return

        self.response_buf.extend(data)
        self.refresh_deadline()

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
                chunked = is_chunked_response(bytes(self.response_buf))
                self._response_is_chunked = bool(chunked)
                if self._response_is_chunked:
                    # RFC 7230: ignore Content-Length when chunked
                    self._response_content_length = -1
                else:
                    self._response_content_length = get_response_content_length(
                        bytes(self.response_buf)
                    )
                self._response_body_received = len(self.response_buf) - header_len

        if not self._response_headers_done:
            return

        hdr_end = self.response_buf.find(HEADER_DELIM)
        header_len = hdr_end + len(HEADER_DELIM)
        body = bytes(self.response_buf[header_len:])

        if self._response_is_chunked:
            framed = try_consume_chunked_body(body)
            if framed is not None:
                # Truncate any bytes past the complete chunked message
                del self.response_buf[header_len + framed:]
                self._prepare_relay()
            return

        self._response_body_received = len(body)
        if self._response_content_length >= 0:
            if self._response_body_received >= self._response_content_length:
                self._prepare_relay()
                return

    def _prepare_relay(self) -> None:
        """Transition to relaying the response back to the client."""
        self.relay_buf = bytes(self.response_buf)
        self.relay_offset = 0
        self.state = State.FORWARDING_RESPONSE
        self.refresh_deadline()

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

        if sent:
            self._client_bytes_sent = True
            self.refresh_deadline()
        self.relay_offset += sent
        if self.relay_offset >= len(self.relay_buf):
            self._finish()

    # ── Backend failure / retry ───────────────────────────────────────────

    def _can_retry(self) -> bool:
        if self._client_bytes_sent:
            return False
        req = self.parsed_request
        if req is None:
            return False
        if req.method.upper() not in config.IDEMPOTENT_METHODS:
            return False
        # attempts so far; retries remaining = MAX - (attempts - 1)
        return self._backend_attempts <= config.MAX_BACKEND_RETRIES

    def _fail_backend(self, reason: str) -> None:
        """Handle a backend I/O failure — retry if allowed, else 502."""
        log.warning(
            "Backend failure (%s) for %s — attempt %d",
            reason,
            self.backend_addr,
            self._backend_attempts,
        )

        failed = self.backend_addr
        if failed is not None and not self._disconnect_notified:
            self._router.on_disconnect(failed)
            self._disconnect_notified = True

        old_sock = self.backend_sock
        if old_sock is not None:
            self.pending_backend_unregister = old_sock
            try:
                old_sock.close()
            except OSError:
                pass
            self.backend_sock = None

        if failed is not None:
            self._tried_backends.add(failed)

        if self._can_retry():
            backend = self._router.next_backend(exclude=self._tried_backends)
            if backend is not None:
                self.backend_addr = backend
                self._backend_attempts += 1
                self._disconnect_notified = False
                self.forward_offset = 0
                self.response_buf = bytearray()
                self.response_status = None
                self._response_headers_done = False
                self._response_content_length = -1
                self._response_body_received = 0
                self._response_is_chunked = False
                self._initiate_backend_connect(backend)
                return

        self._send_error(502, "Bad Gateway")

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
            self._client_bytes_sent = True
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
        if self.backend_addr and not self._disconnect_notified:
            self._router.on_disconnect(self.backend_addr)
            self._disconnect_notified = True

        self.state = State.DONE

    def cleanup(self) -> None:
        """Close both sockets.  Safe to call multiple times."""
        for sock in (self.client_sock, self.backend_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self.pending_backend_unregister is not None:
            try:
                self.pending_backend_unregister.close()
            except OSError:
                pass
