"""pytest configuration and shared fixtures."""

from __future__ import annotations

import socket
import threading
import time
from typing import Generator, Tuple

import pytest

# Ensure project root is importable
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from mock_backends import run_backend


@pytest.fixture(scope="session")
def backend_ports() -> Tuple[int, int, int]:
    """Return three fixed ports for mock backends."""
    return (18001, 18002, 18003)


@pytest.fixture(scope="session", autouse=True)
def mock_backends(backend_ports) -> Generator:
    """Start mock backend servers for the test session."""
    events = []
    for port in backend_ports:
        ev = threading.Event()
        t = threading.Thread(target=run_backend, args=(port, ev), daemon=True)
        t.start()
        events.append(ev)

    # Wait for all backends to bind
    for ev in events:
        ev.wait(timeout=5)
    time.sleep(0.3)

    yield backend_ports


@pytest.fixture()
def free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
