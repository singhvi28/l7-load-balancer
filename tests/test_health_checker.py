"""Unit tests for the health checker."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from health_checker import _probe, HealthChecker
from router import RoundRobinRouter


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


class TestProbe:
    def test_healthy_backend(self, backend_ports):
        """Probe a running mock backend."""
        assert _probe("127.0.0.1", backend_ports[0], timeout=2, path="/health") is True

    def test_dead_backend(self):
        """Probe a port with nothing listening."""
        assert _probe("127.0.0.1", 19999, timeout=1, path="/health") is False


class TestHealthChecker:
    def test_marks_dead_backend_unhealthy(self):
        backends = [("127.0.0.1", 19998), ("127.0.0.1", 19999)]
        router = RoundRobinRouter(backends)
        hc = HealthChecker(router, interval=0.5, timeout=0.5)
        hc.start()
        time.sleep(1.5)
        hc.stop()
        # Both should be marked unhealthy (nothing listens on 19998/19999)
        assert len(router.get_healthy_backends()) == 0

    def test_keeps_live_backend_healthy(self, backend_ports):
        backends = [("127.0.0.1", backend_ports[0]), ("127.0.0.1", 19999)]
        router = RoundRobinRouter(backends)
        hc = HealthChecker(router, interval=0.5, timeout=0.5)
        hc.start()
        time.sleep(1.5)
        hc.stop()
        healthy = router.get_healthy_backends()
        assert ("127.0.0.1", backend_ports[0]) in healthy
        assert ("127.0.0.1", 19999) not in healthy
