#!/usr/bin/env python3
"""
L7 Load Balancer — entry point.

Wires together the router, health-checker, and reactor, then starts the
event loop.  With ``--workers N`` (N > 1), the parent forks N children that
share the listen port via ``SO_REUSEPORT``.

Usage::

    python main.py
    python main.py --algorithm least_connections
    python main.py --port 9090
    python main.py --backends 127.0.0.1:8001,127.0.0.1:8002
    python main.py --workers 4
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
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


def _run_worker(args: argparse.Namespace, backends: List[Tuple[str, int]]) -> None:
    """Build router / health-checker / reactor and enter the event loop."""
    log.info(
        "Worker starting  listen=%s:%d algorithm=%s backends=%s",
        args.host, args.port, args.algorithm, backends,
    )

    router = create_router(args.algorithm, backends)
    reactor = Reactor(
        router,
        host=args.host,
        port=args.port,
        require_reuseport=args.workers > 1,
    )

    def _signal_handler(signum, frame):
        log.info("Received signal %d — stopping…", signum)
        reactor.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    health_checker = None
    if not args.no_health_check:
        health_checker = HealthChecker(router)
        health_checker.start()

    try:
        reactor.start()
    finally:
        if health_checker:
            health_checker.stop()
        log.info("Worker exiting.")


def _run_supervisor(args: argparse.Namespace, backends: List[Tuple[str, int]]) -> None:
    """Fork *N* workers, forward signals, and shut down if any child exits."""
    if not hasattr(socket, "SO_REUSEPORT"):
        log.error("SO_REUSEPORT is not available; cannot use --workers > 1")
        sys.exit(1)

    children: List[int] = []

    def _forward_and_exit(signum, frame):
        log.info("Supervisor received signal %d — stopping workers…", signum)
        for pid in children:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, _forward_and_exit)
    signal.signal(signal.SIGTERM, _forward_and_exit)

    log.info("─" * 60)
    log.info("L7 Load Balancer  (supervisor)")
    log.info("─" * 60)
    log.info("  Listen   : %s:%d", args.host, args.port)
    log.info("  Algorithm: %s", args.algorithm)
    log.info("  Backends : %s", backends)
    log.info("  Workers  : %d", args.workers)
    log.info("─" * 60)

    for i in range(args.workers):
        pid = os.fork()
        if pid == 0:
            # Child — reset signal handlers to worker defaults
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            try:
                _run_worker(args, backends)
            except Exception:
                log.exception("Worker crashed")
                os._exit(1)
            os._exit(0)
        children.append(pid)
        log.info("Forked worker %d (pid=%d)", i + 1, pid)

    # Parent waits; if any child exits, tear down the rest
    exit_code = 0
    while children:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        except KeyboardInterrupt:
            _forward_and_exit(signal.SIGINT, None)
            continue

        if pid not in children:
            continue
        children.remove(pid)
        if os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
            log.warning("Worker pid=%d exited with code %d", pid, code)
            if code != 0:
                exit_code = code
        elif os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            log.warning("Worker pid=%d killed by signal %d", pid, sig)
            exit_code = 1
        else:
            log.warning("Worker pid=%d exited (status=%s)", pid, status)
            exit_code = 1

        # Shut down remaining workers to avoid a half-dead fleet
        for remaining in list(children):
            try:
                os.kill(remaining, signal.SIGTERM)
            except ProcessLookupError:
                pass

    log.info("Supervisor exiting.")
    sys.exit(exit_code)


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
    parser.add_argument(
        "--workers",
        type=int,
        default=config.WORKERS,
        help="Number of worker processes (SO_REUSEPORT; default 1)",
    )
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be >= 1")

    backends = (
        parse_backends(args.backends)
        if args.backends
        else config.BACKENDS
    )

    if args.workers == 1:
        log.info("─" * 60)
        log.info("L7 Load Balancer")
        log.info("─" * 60)
        log.info("  Listen   : %s:%d", args.host, args.port)
        log.info("  Algorithm: %s", args.algorithm)
        log.info("  Backends : %s", backends)
        log.info("  Workers  : 1")
        log.info("─" * 60)
        _run_worker(args, backends)
        log.info("Bye.")
    else:
        _run_supervisor(args, backends)


if __name__ == "__main__":
    main()
