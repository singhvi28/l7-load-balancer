#!/usr/bin/env python3
"""
L7 Load Balancer — entry point.

Wires together the router, health-checker, and reactor, then starts the
event loop.

Usage::

    python main.py
    python main.py --algorithm least_connections
    python main.py --port 9090
    python main.py --backends 127.0.0.1:8001,127.0.0.1:8002
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import List, Tuple

import config
from health_checker import HealthChecker
from logger import get_logger
from reactor import Reactor
from router import create_router

log = get_logger()


def parse_backends(raw: str) -> List[Tuple[str, int]]:
    """Parse a comma-separated list of ``host:port`` pairs."""
    backends = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, _, port_s = entry.rpartition(":")
        backends.append((host, int(port_s)))
    return backends


def main() -> None:
    parser = argparse.ArgumentParser(
        description="High-performance Layer 7 Load Balancer"
    )
    parser.add_argument(
        "--host", default=config.LISTEN_HOST, help="Bind address"
    )
    parser.add_argument(
        "--port", type=int, default=config.LISTEN_PORT, help="Bind port"
    )
    parser.add_argument(
        "--algorithm",
        choices=["round_robin", "least_connections"],
        default=config.ROUTING_ALGORITHM,
        help="Routing algorithm",
    )
    parser.add_argument(
        "--backends",
        type=str,
        default=None,
        help="Comma-separated backend list (host:port,...)",
    )
    parser.add_argument(
        "--no-health-check",
        action="store_true",
        help="Disable background health checking",
    )
    args = parser.parse_args()

    backends = (
        parse_backends(args.backends)
        if args.backends
        else config.BACKENDS
    )

    log.info("─" * 60)
    log.info("L7 Load Balancer")
    log.info("─" * 60)
    log.info("  Listen   : %s:%d", args.host, args.port)
    log.info("  Algorithm: %s", args.algorithm)
    log.info("  Backends : %s", backends)
    log.info("─" * 60)

    router = create_router(args.algorithm, backends)
    reactor = Reactor(router, host=args.host, port=args.port)

    # Graceful shutdown on SIGINT / SIGTERM
    def _signal_handler(signum, frame):
        log.info("Received signal %d — stopping…", signum)
        reactor.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Start health checker (in daemon thread)
    health_checker = None
    if not args.no_health_check:
        health_checker = HealthChecker(router)
        health_checker.start()

    try:
        reactor.start()
    finally:
        if health_checker:
            health_checker.stop()
        log.info("Bye.")


if __name__ == "__main__":
    main()
