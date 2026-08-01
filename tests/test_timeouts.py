"""Tests for per-connection idle / backend timeouts."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from reactor import Reactor
from router import RoundRobinRouter


def _recv_all(sock: socket.socket, timeout: float = 5.0) -> bytes:
    sock.settimeout(timeout)
    chunks = []
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    except socket.timeout:
        pass
    return b"".join(chunks)


class TestClientIdleTimeout:
    def test_idle_client_gets_408(self, free_port, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "CLIENT_IDLE_TIMEOUT", 0.3)

        router = RoundRobinRouter([("127.0.0.1", 19980)])
        reactor = Reactor(router, host="127.0.0.1", port=free_port)
        t = threading.Thread(target=reactor.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=5) as sock:
                # Open connection but send nothing (Slowloris-style)
                data = _recv_all(sock, timeout=2.0)
                assert b"408" in data
        finally:
            reactor.stop()
            time.sleep(0.2)


class TestBackendTimeout:
    def test_hung_backend_gets_504(self, free_port, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "BACKEND_TIMEOUT", 0.3)
        monkeypatch.setattr(cfg, "CONNECT_TIMEOUT", 0.3)
        monkeypatch.setattr(cfg, "MAX_BACKEND_RETRIES", 0)

        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(2.0)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), SlowHandler)
        slow_port = httpd.server_address[1]
        srv = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv.start()
        time.sleep(0.1)

        router = RoundRobinRouter([("127.0.0.1", slow_port)])
        reactor = Reactor(router, host="127.0.0.1", port=free_port)
        t = threading.Thread(target=reactor.start, daemon=True)
        t.start()
        time.sleep(0.2)

        try:
            with socket.create_connection(("127.0.0.1", free_port), timeout=5) as sock:
                sock.sendall(
                    b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                )
                data = _recv_all(sock, timeout=3.0)
                assert b"504" in data
        finally:
            reactor.stop()
            httpd.shutdown()
            time.sleep(0.2)
