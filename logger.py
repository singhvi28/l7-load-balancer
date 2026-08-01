"""
Structured access-log system modelled after standard reverse-proxy logs.

Example output::

    2026-07-07 12:34:56 | INFO | 10.0.0.5:49312 -> 127.0.0.1:8001 "GET /api HTTP/1.1" 200 3.2ms
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

import config


def setup_logging() -> logging.Logger:
    """Configure and return the application-wide logger."""
    logger = logging.getLogger("lb")
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s | pid=%(process)d | %(levelname)-5s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


_logger: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
    """Return the cached application logger, creating it on first call."""
    global _logger
    if _logger is None:
        _logger = setup_logging()
    return _logger


def access_log(
    *,
    client_ip: str,
    client_port: int,
    backend_ip: str,
    backend_port: int,
    method: str,
    path: str,
    version: str,
    status: int,
    latency_ms: float,
) -> None:
    """Emit a single access-log line in the configured format."""
    msg = config.ACCESS_LOG_FORMAT.format(
        client_ip=client_ip,
        client_port=client_port,
        backend_ip=backend_ip,
        backend_port=backend_port,
        method=method,
        path=path,
        version=version,
        status=status,
        latency_ms=latency_ms,
    )
    get_logger().info(msg)
