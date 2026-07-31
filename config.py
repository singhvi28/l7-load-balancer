"""
Configuration for the L7 Load Balancer.

All tunables live here so the rest of the codebase stays clean.
"""

# ─── Listener ────────────────────────────────────────────────────────────────
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
LISTEN_BACKLOG = 1024          # kernel accept-queue depth

# ─── Backend Pool ────────────────────────────────────────────────────────────
# Each entry is (host, port).  The health-checker will promote / demote these.
BACKENDS = [
    ("127.0.0.1", 8001),
    ("127.0.0.1", 8002),
    ("127.0.0.1", 8003),
]

# ─── Routing ─────────────────────────────────────────────────────────────────
# "round_robin" | "least_connections"
ROUTING_ALGORITHM = "round_robin"

# ─── Health Checking ─────────────────────────────────────────────────────────
HEALTH_CHECK_INTERVAL = 5      # seconds between sweeps
HEALTH_CHECK_TIMEOUT = 2       # per-backend TCP connect timeout
HEALTH_CHECK_PATH = "/health"  # HTTP path to probe (GET)

# ─── Connection Handling ─────────────────────────────────────────────────────
CLIENT_RECV_SIZE = 65536       # bytes per recv() call
BACKEND_RECV_SIZE = 65536
CONNECT_TIMEOUT = 5            # seconds to wait for backend connect
MAX_REQUEST_SIZE = 10 * 1024 * 1024   # 10 MiB safety cap

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
ACCESS_LOG_FORMAT = (
    '{client_ip}:{client_port} -> {backend_ip}:{backend_port} '
    '"{method} {path} {version}" {status} {latency_ms:.1f}ms'
)
