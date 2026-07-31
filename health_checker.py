"""
Background health-checker daemon thread.

Periodically probes every registered backend with a lightweight HTTP
``GET /health`` request.  Backends that fail to respond within the
configured timeout are removed from the router's healthy pool.  When
they recover, they are re-added.

Runs on a standard daemon thread so it never blocks the main event loop.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING

import config
from logger import get_logger

if TYPE_CHECKING:
    from router import RoundRobinRouter, LeastConnectionsRouter


def _probe(host: str, port: int, timeout: float, path: str) -> bool:
    """Attempt a quick HTTP GET to ``host:port/path``.

    Returns ``True`` if a valid HTTP response is received within *timeout*.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            sock.sendall(request.encode("latin-1"))
            # Read just enough to confirm a valid status line
            data = sock.recv(1024)
            if data and data.startswith(b"HTTP/"):
                return True
            return False
    except (OSError, socket.timeout, ConnectionRefusedError):
        return False


class HealthChecker:
    """Daemon thread that continuously probes backends."""

    def __init__(
        self,
        router: RoundRobinRouter | LeastConnectionsRouter,
        *,
        interval: float = config.HEALTH_CHECK_INTERVAL,
        timeout: float = config.HEALTH_CHECK_TIMEOUT,
        path: str = config.HEALTH_CHECK_PATH,
    ) -> None:
        self._router = router
        self._interval = interval
        self._timeout = timeout
        self._path = path
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="health-checker", daemon=True
        )
        self._log = get_logger()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self._log.info(
            "Health-checker starting  (interval=%ss, timeout=%ss, path=%s)",
            self._interval, self._timeout, self._path,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self._interval + 2)

    # ── Main loop ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.is_set():
            all_backends = self._router.get_all_backends()
            for backend in all_backends:
                host, port = backend
                healthy = _probe(host, port, self._timeout, self._path)
                if healthy:
                    self._router.mark_healthy(backend)
                else:
                    self._log.warning("Backend %s:%d is DOWN", host, port)
                    self._router.mark_unhealthy(backend)

            self._stop_event.wait(self._interval)
