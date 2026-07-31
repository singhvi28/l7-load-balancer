# L7 Load Balancer

A high-performance **Layer 7 (HTTP) load balancer** built entirely with Python's standard library — no frameworks, no external dependencies. Uses raw socket streams, non-blocking I/O multiplexing via `epoll`, and a custom HTTP parser.

## Architecture

```
                     ┌────────────────────────-───┐
                     │    Clients (HTTP/1.1)      │
                     └─────────┬───────────────-──┘
                               │
                     ┌─────────▼───────────────-──┐
                     │   Acceptor (non-blocking)  │
                     │   port 8080                │
                     └─────────┬───────────────-──┘
                               │
                     ┌─────────▼───────────-─────-─┐
                     │   Reactor (Event Loop)      │
                     │   selectors / epoll         │
                     │                             │
                     │   ┌──────────────────-───┐  │
                     │   │ Connection State     │  │
                     │   │ Machine (per-conn)   │  │
                     │   └───────────────────-──┘  │
                     │   ┌────────────────────-─┐  │
                     │   │ HTTP Parser          │  │
                     │   │ (raw byte streams)   │  │
                     │   └────────────────────-─┘  │
                     │   ┌────────────────────-─┐  │
                     │   │ Router               │  │
                     │   │ (RR / Least-Conn)    │  │
                     │   └────────────────────-─┘  │
                     └─────────┬───────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼──┐     ┌───────▼───┐    ┌───────▼───┐
     │ Backend 1 │     │ Backend 2 │    │ Backend 3 │
     │ :8001     │     │ :8002     │    │ :8003     │
     └───────────┘     └───────────┘    └───────────┘

     ┌──────────────────────────────────┐
     │ Health Checker (daemon thread)   │
     │ Periodic TCP / HTTP probes       │
     └──────────────────────────────────┘
```

## Features

- **Non-blocking I/O** — `selectors.DefaultSelector` (epoll on Linux) handles thousands of concurrent connections on a single thread
- **Connection state machine** — properly handles partial TCP reads/writes across states: `READING_REQUEST → CONNECTING_TO_BACKEND → FORWARDING_REQUEST → READING_RESPONSE → FORWARDING_RESPONSE`
- **Custom HTTP parser** — parses request lines, headers, `Content-Length` bodies from raw byte streams
- **`X-Forwarded-For` injection** — automatically appends the client's original IP
- **Routing algorithms:**
  - **Round-Robin** — cyclic iteration with modulo arithmetic
  - **Least Connections** — O(log N) min-heap with lazy deletion for fairness
- **Active health checking** — daemon thread probes backends with `GET /health` and safely promotes/demotes servers using `threading.Lock`
- **Structured access logs** — latency, client IP, backend, method, path, status code
- **Zero external dependencies** — standard library only (`socket`, `selectors`, `threading`, `heapq`, `collections`, `logging`)

## Quick Start

### 1. Start mock backends

```bash
python3 mock_backends.py
```

This starts 3 HTTP servers on ports 8001, 8002, 8003.

### 2. Start the load balancer

```bash
# Round-robin (default)
python3 main.py

# Least connections
python3 main.py --algorithm least_connections

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
| `--no-health-check` | off | Disable background health checking |

## Project Structure

```
├── config.py           # All tunables (ports, timeouts, buffer sizes)
├── main.py             # Entry point — CLI + wiring
├── reactor.py          # Selector-based event loop
├── connection.py       # Per-connection state machine & proxy logic
├── http_parser.py      # Raw HTTP request/response parsing
├── router.py           # Round-Robin & Least-Connections algorithms
├── health_checker.py   # Background health-check daemon thread
├── logger.py           # Access-log system
├── mock_backends.py    # Test backend servers
└── tests/
    ├── conftest.py
    ├── test_http_parser.py
    ├── test_router.py
    ├── test_health_checker.py
    └── test_integration.py
```

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Stress Testing

```bash
# Apache Bench — 10,000 requests, 100 concurrent
ab -n 10000 -c 100 http://localhost:8080/

# wrk — 30 seconds, 4 threads, 400 connections
wrk -t4 -c400 -d30s http://localhost:8080/
```
