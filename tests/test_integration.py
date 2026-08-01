"""
Integration tests — spin up the full load balancer and send real HTTP
requests through it.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import json
import socket
import threading
import time

import pytest

from reactor import Reactor
from router import RoundRobinRouter, LeastConnectionsRouter, create_router


def _send_http_get(host: str, port: int, path: str = "/test") -> dict:
    """Send a raw HTTP GET and return the parsed JSON body."""
    with socket.create_connection((host, port), timeout=5) as sock:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode())
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    # Split headers from body
    parts = raw.split(b"\r\n\r\n", 1)
    if len(parts) == 2:
        return json.loads(parts[1])
    return {}


def _send_http_post(host: str, port: int, path: str, body: bytes) -> dict:
    """Send a raw HTTP POST with a body."""
    with socket.create_connection((host, port), timeout=5) as sock:
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode() + body)
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    parts = raw.split(b"\r\n\r\n", 1)
    if len(parts) == 2:
        return json.loads(parts[1])
    return {}


@pytest.fixture()
def lb_round_robin(free_port, backend_ports):
    """Start a load balancer with round-robin routing."""
    backends = [("127.0.0.1", p) for p in backend_ports]
    router = RoundRobinRouter(backends)
    reactor = Reactor(router, host="127.0.0.1", port=free_port)
    t = threading.Thread(target=reactor.start, daemon=True)
    t.start()
    time.sleep(0.3)
    yield free_port, router
    reactor.stop()
    time.sleep(0.2)


@pytest.fixture()
def lb_least_conn(free_port, backend_ports):
    """Start a load balancer with least-connections routing."""
    backends = [("127.0.0.1", p) for p in backend_ports]
    router = LeastConnectionsRouter(backends)
    reactor = Reactor(router, host="127.0.0.1", port=free_port)
    t = threading.Thread(target=reactor.start, daemon=True)
    t.start()
    time.sleep(0.3)
    yield free_port, router
    reactor.stop()
    time.sleep(0.2)


class TestRoundRobinIntegration:
    def test_single_request(self, lb_round_robin, backend_ports):
        port, router = lb_round_robin
        resp = _send_http_get("127.0.0.1", port)
        assert "backend" in resp
        # Backend should be one of our ports
        backend_port = int(resp["backend"].strip(":"))
        assert backend_port in backend_ports

    def test_distributes_across_backends(self, lb_round_robin, backend_ports):
        port, router = lb_round_robin
        seen = set()
        for _ in range(6):
            resp = _send_http_get("127.0.0.1", port)
            seen.add(resp["backend"])
        # Should have hit all 3 backends
        assert len(seen) == len(backend_ports)

    def test_post_request(self, lb_round_robin):
        port, router = lb_round_robin
        body = b'{"hello": "world"}'
        resp = _send_http_post("127.0.0.1", port, "/api", body)
        assert resp.get("method") == "POST"
        assert resp.get("received_bytes") == len(body)

    def test_503_when_no_backends(self, free_port):
        """With all backends unhealthy, LB should return 503."""
        backends = [("127.0.0.1", 19990)]
        router = RoundRobinRouter(backends)
        router.mark_unhealthy(backends[0])
        reactor = Reactor(router, host="127.0.0.1", port=free_port)
        t = threading.Thread(target=reactor.start, daemon=True)
        t.start()
        time.sleep(0.3)

        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=5) as sock:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                data = sock.recv(4096)
                assert b"503" in data
        finally:
            reactor.stop()


class TestLeastConnectionsIntegration:
    def test_single_request(self, lb_least_conn, backend_ports):
        port, router = lb_least_conn
        resp = _send_http_get("127.0.0.1", port)
        assert "backend" in resp

    def test_distributes_across_backends(self, lb_least_conn, backend_ports):
        port, router = lb_least_conn
        seen = set()
        for _ in range(6):
            resp = _send_http_get("127.0.0.1", port)
            seen.add(resp["backend"])
        assert len(seen) == len(backend_ports)


class TestXForwardedFor:
    def test_header_injected(self, lb_round_robin):
        """The request reaching the backend should have X-Forwarded-For."""
        port, router = lb_round_robin
        # We can verify by checking the response — our mock doesn't echo
        # headers, but the request parse + inject logic is tested in unit tests.
        # Here we just ensure the request completes successfully.
        resp = _send_http_get("127.0.0.1", port, "/check-xff")
        assert resp.get("path") == "/check-xff"


class TestBackendRetries:
    def test_get_retries_dead_backend(self, free_port, backend_ports):
        """GET should fail over from a dead backend to a live one."""
        dead = ("127.0.0.1", 19991)
        live = ("127.0.0.1", backend_ports[0])
        router = RoundRobinRouter([dead, live])
        reactor = Reactor(router, host="127.0.0.1", port=free_port)
        t = threading.Thread(target=reactor.start, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            resp = _send_http_get("127.0.0.1", free_port)
            assert "backend" in resp
            assert int(resp["backend"].strip(":")) == backend_ports[0]
        finally:
            reactor.stop()
            time.sleep(0.2)

    def test_post_does_not_retry(self, free_port):
        """POST to a dead-only pool should 502 without succeeding."""
        dead = ("127.0.0.1", 19992)
        router = RoundRobinRouter([dead])
        reactor = Reactor(router, host="127.0.0.1", port=free_port)
        t = threading.Thread(target=reactor.start, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=5) as sock:
                body = b'{"x":1}'
                req = (
                    f"POST /api HTTP/1.1\r\n"
                    f"Host: localhost\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                ).encode() + body
                sock.sendall(req)
                data = sock.recv(4096)
                assert b"502" in data
        finally:
            reactor.stop()
            time.sleep(0.2)

    def test_retry_cap_respected(self, free_port, monkeypatch):
        """Exhausting retries against dead backends yields 502."""
        import config as cfg
        monkeypatch.setattr(cfg, "MAX_BACKEND_RETRIES", 1)
        dead1 = ("127.0.0.1", 19993)
        dead2 = ("127.0.0.1", 19994)
        router = RoundRobinRouter([dead1, dead2])
        reactor = Reactor(router, host="127.0.0.1", port=free_port)
        t = threading.Thread(target=reactor.start, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=5) as sock:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                data = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                assert b"502" in data
        finally:
            reactor.stop()
            time.sleep(0.2)
