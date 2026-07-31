#!/usr/bin/env python3
"""
Mock backend HTTP servers for local testing.

Spins up lightweight ``http.server`` instances on ports 8001, 8002, 8003.
Each server responds to any GET/POST with a JSON body identifying itself,
and exposes a ``/health`` endpoint for the health-checker.

Usage::

    python mock_backends.py              # start all 3 in foreground
    python mock_backends.py --ports 8001 8002
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler


class BackendHandler(BaseHTTPRequestHandler):
    """Simple handler that echoes request info back as JSON."""

    server_version = "MockBackend/1.0"
    # Suppress default stderr logging per-request
    def log_message(self, format, *args):
        pass

    def _respond(self, status: int = 200, body: dict | None = None) -> None:
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        body = {
            "backend": f":{self.server.server_port}",
            "path": self.path,
            "method": "GET",
            "timestamp": time.time(),
        }
        if self.path == "/health":
            body["status"] = "healthy"
        self._respond(200, body)

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        request_body = self.rfile.read(content_length) if content_length else b""
        body = {
            "backend": f":{self.server.server_port}",
            "path": self.path,
            "method": "POST",
            "received_bytes": len(request_body),
            "timestamp": time.time(),
        }
        self._respond(200, body)

    # Handle all other methods generically
    def do_PUT(self) -> None:
        self.do_POST()

    def do_DELETE(self) -> None:
        self.do_GET()

    def do_PATCH(self) -> None:
        self.do_POST()


def run_backend(port: int, ready_event: threading.Event | None = None) -> None:
    """Start a blocking HTTP server on *port*."""
    server = HTTPServer(("127.0.0.1", port), BackendHandler)
    print(f"  ✓ Mock backend listening on 127.0.0.1:{port}")
    if ready_event:
        ready_event.set()
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock backend servers")
    parser.add_argument(
        "--ports",
        nargs="+",
        type=int,
        default=[8001, 8002, 8003],
        help="Ports to listen on",
    )
    args = parser.parse_args()

    print(f"Starting {len(args.ports)} mock backend(s)…")
    threads = []
    for port in args.ports:
        t = threading.Thread(target=run_backend, args=(port,), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down mock backends.")


if __name__ == "__main__":
    main()
