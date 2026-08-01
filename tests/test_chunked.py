"""Integration test for chunked Transfer-Encoding backend responses."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import socket
import threading
import time

from reactor import Reactor
from router import RoundRobinRouter


def _raw_chunked_backend(port: int, ready: threading.Event) -> None:
    """Serve one HTTP response with Transfer-Encoding: chunked, then exit."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    ready.set()
    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                # Drain request
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                ) + body
                conn.sendall(resp)
    except OSError:
        pass
    finally:
        srv.close()


def test_chunked_response_relayed(free_port):
    backend_port_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backend_port_sock.bind(("127.0.0.1", 0))
    backend_port = backend_port_sock.getsockname()[1]
    backend_port_sock.close()

    ready = threading.Event()
    t_be = threading.Thread(
        target=_raw_chunked_backend, args=(backend_port, ready), daemon=True
    )
    t_be.start()
    assert ready.wait(timeout=2)

    router = RoundRobinRouter([("127.0.0.1", backend_port)])
    reactor = Reactor(router, host="127.0.0.1", port=free_port)
    t = threading.Thread(target=reactor.start, daemon=True)
    t.start()
    time.sleep(0.3)

    try:
        with socket.create_connection(("127.0.0.1", free_port), timeout=5) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks)
        assert b"Transfer-Encoding: chunked" in raw
        assert b"5\r\nhello\r\n" in raw
        assert b"0\r\n\r\n" in raw
    finally:
        reactor.stop()
        time.sleep(0.2)
