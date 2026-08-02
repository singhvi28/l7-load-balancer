# L7 Load Balancer

A high-performance **Layer 7 (HTTP) load balancer** built entirely with Python's standard library — no frameworks, no external dependencies. Uses raw sockets, non-blocking I/O multiplexing via `epoll`, and a custom HTTP parser.

## Architecture

```
                     ┌────────────────────────────┐
                     │    Clients (HTTP/1.1)      │
                     └─────────────┬──────────────┘
                                   │
                     ┌─────────────▼──────────────┐
                     │  SO_REUSEPORT acceptor(s)  │
                     │  port 8080  (1..N workers) │
                     └─────────────┬──────────────┘
                                   │
                     ┌─────────────▼──────────────┐
                     │  Reactor (Event Loop)      │
                     │  selectors / epoll         │
                     │  + deadline heap           │
                     │                            │
                     │  ┌──────────────────────┐  │
                     │  │ Connection State     │  │
                     │  │ Machine (per-conn)   │  │
                     │  │ + idempotent retries │  │
                     │  └──────────────────────┘  │
                     │  ┌──────────────────────┐  │
                     │  │ HTTP Parser          │  │
                     │  │ Content-Length /     │  │
                     │  │ chunked TE           │  │
                     │  └──────────────────────┘  │
                     │  ┌──────────────────────┐  │
                     │  │ Router               │  │
                     │  │ (RR / Least-Conn)    │  │
                     │  └──────────────────────┘  │
                     └─────────────┬──────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     ┌────────▼──┐         ┌───────▼───┐        ┌───────▼───┐
     │ Backend 1 │         │ Backend 2 │        │ Backend 3 │
     │ :8001     │         │ :8002     │        │ :8003     │
     └───────────┘         └───────────┘        └───────────┘

     ┌──────────────────────────────────┐
     │ Health Checker (daemon thread)   │
     │ Periodic HTTP probes (GET /health)│
     └──────────────────────────────────┘
```

With `--workers N` (N > 1), a supervisor forks N processes that each bind the same port via `SO_REUSEPORT`. The kernel distributes accepts across workers; each worker has its own reactor, router, and health checker.

## Features

- **Non-blocking I/O** — `selectors.DefaultSelector` (epoll on Linux) multiplexes thousands of connections on a single thread per worker
- **Connection state machine** — handles partial TCP reads/writes across `READING_REQUEST → CONNECTING_TO_BACKEND → FORWARDING_REQUEST → READING_RESPONSE → FORWARDING_RESPONSE`
- **Custom HTTP parser** — request/response headers, `Content-Length`, close-delimited bodies, and `Transfer-Encoding: chunked` responses
- **`X-Forwarded-For` injection** — appends the client's original IP before forwarding
- **Routing algorithms:**
  - **Round-Robin** — cyclic selection over the healthy pool
  - **Least Connections** — O(log N) min-heap with lazy deletion
- **Idempotent retries** — on backend connect/IO failure, retries `GET`/`HEAD`/`OPTIONS`/`PUT`/`DELETE` against a different backend (up to `MAX_BACKEND_RETRIES`), only before any client bytes are sent
- **Per-connection timeouts** — idle / connect / backend / client-send deadlines via a min-heap driving the selector timeout (`408` / `504`)
- **Multi-process scaling** — `--workers N` + `SO_REUSEPORT`
- **Active health checking** — daemon thread probes `GET /health` and promotes/demotes backends under a lock
- **Structured access logs** — pid, latency, client, backend, method, path, status
- **Zero external dependencies** — standard library only

## Quick Start

### 1. Start mock backends

```bash
python3 mock_backends.py
```

Starts 3 HTTP servers on ports 8001, 8002, and 8003.

### 2. Start the load balancer

```bash
# Round-robin (default), single worker
python3 main.py

# Least connections
python3 main.py --algorithm least_connections

# Scale across cores
python3 main.py --workers 4

# Custom port and backends
python3 main.py --port 9090 --backends 10.0.0.1:80,10.0.0.2:80
```

### 3. Send traffic

```bash
curl http://localhost:8080/hello
curl -X POST -d '{"key":"value"}' http://localhost:8080/api
```

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8080` | Bind port |
| `--algorithm` | `round_robin` | `round_robin` or `least_connections` |
| `--backends` | `127.0.0.1:8001,...` | Comma-separated `host:port` list |
| `--workers` | `1` | Worker processes sharing the port via `SO_REUSEPORT` |
| `--no-health-check` | off | Disable background health checking |

## Project Structure

```
├── config.py           # Tunables (ports, timeouts, retries, workers)
├── main.py             # Entry point — CLI, supervisor / workers
├── reactor.py          # Selector event loop + deadline heap
├── connection.py       # Per-connection state machine, retries, timeouts
├── http_parser.py      # HTTP request/response + chunked framing
├── router.py           # Round-Robin & Least-Connections (+ exclude)
├── health_checker.py   # Background health-check daemon thread
├── logger.py           # Access-log system (includes pid)
├── mock_backends.py    # Test backend servers
├── NOTES.md            # Interview study guide
├── CONTEXT.md          # Design notes / remaining gaps
└── tests/
    ├── TESTING.md
    ├── conftest.py
    ├── test_http_parser.py
    ├── test_router.py
    ├── test_health_checker.py
    ├── test_integration.py
    ├── test_timeouts.py
    ├── test_workers.py
    └── test_chunked.py
```

## Running Tests

```bash
python3 -m pytest tests/ -v
```

See [`tests/TESTING.md`](tests/TESTING.md) for the full test matrix.

## Stress Testing

```bash
# Apache Bench — use -l because mock backends return variable-length bodies
ab -n 10000 -c 100 -l http://127.0.0.1:8080/

# Multi-worker balancer + ab
python3 main.py --workers 4
ab -n 10000 -c 100 -l http://127.0.0.1:8080/

# wrk
wrk -t4 -c400 -d30s http://127.0.0.1:8080/
```

Without `-l`, `ab` treats differing body lengths as failures. With `-l`, the same workload reports 0 failed requests (~3.2k–3.3k req/s at `-c 100` against the mock backends on a laptop).

## Known Limitations

Still open (each needs architectural change, not a small addition):

- Full response buffered before relay (no streaming / backpressure)
- No HTTP keep-alive / backend connection pooling
- No TLS
- No HTTP/2

See [`NOTES.md`](NOTES.md) for the interview-oriented deep dive, and [`CONTEXT.md`](CONTEXT.md) for the ranked roadmap.
