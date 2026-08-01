"""Tests for SO_REUSEPORT multi-worker support."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import socket
import threading
import time

import pytest

from reactor import Reactor
from router import RoundRobinRouter


@pytest.mark.skipif(
    not hasattr(socket, "SO_REUSEPORT"),
    reason="SO_REUSEPORT not available",
)
def test_two_reactors_bind_same_port(free_port, backend_ports):
    """Two reactors with SO_REUSEPORT can share one listen port."""
    backends = [("127.0.0.1", backend_ports[0])]
    r1 = Reactor(
        RoundRobinRouter(backends),
        host="127.0.0.1",
        port=free_port,
        require_reuseport=True,
    )
    r2 = Reactor(
        RoundRobinRouter(backends),
        host="127.0.0.1",
        port=free_port,
        require_reuseport=True,
    )
    t1 = threading.Thread(target=r1.start, daemon=True)
    t2 = threading.Thread(target=r2.start, daemon=True)
    t1.start()
    time.sleep(0.15)
    t2.start()
    time.sleep(0.3)

    try:
        with socket.create_connection(("127.0.0.1", free_port), timeout=5) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = sock.recv(4096)
            assert data.startswith(b"HTTP/")
    finally:
        r1.stop()
        r2.stop()
        time.sleep(0.2)
