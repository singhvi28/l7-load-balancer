# Testing Documentation

Comprehensive test documentation for the L7 Load Balancer. The test suite contains **65 tests** spanning unit tests, component tests, and full integration tests.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Running the Tests](#running-the-tests)
- [Test Infrastructure](#test-infrastructure)
- [Test Modules](#test-modules)
  - [test_http_parser.py — HTTP Parser Unit Tests](#test_http_parserpy--http-parser-unit-tests)
  - [test_router.py — Routing Algorithm Unit Tests](#test_routerpy--routing-algorithm-unit-tests)
  - [test_health_checker.py — Health Checker Tests](#test_health_checkerpy--health-checker-tests)
  - [test_integration.py — End-to-End Integration Tests](#test_integrationpy--end-to-end-integration-tests)
  - [test_timeouts.py — Per-Connection Timeout Tests](#test_timeoutspy--per-connection-timeout-tests)
  - [test_workers.py — SO_REUSEPORT Worker Tests](#test_workerspy--soreuseport-worker-tests)
  - [test_chunked.py — Chunked Response Integration](#test_chunkedpy--chunked-response-integration)
- [Test Matrix Summary](#test-matrix-summary)
- [Stress Testing](#stress-testing)

---

## Prerequisites

- **Python 3.10+**
- **pytest** (`pip install pytest`)

No other external dependencies are needed — the load balancer itself is built on the Python standard library.

## Running the Tests

```bash
# Run all tests
python3 -m pytest tests/

# Verbose output (shows each test name)
python3 -m pytest tests/ -v

# Run a specific test module
python3 -m pytest tests/test_router.py -v

# Run a specific test class
python3 -m pytest tests/test_integration.py::TestRoundRobinIntegration -v

# Run a single test
python3 -m pytest tests/test_http_parser.py::TestTryParseRequest::test_simple_get -v

# With short traceback on failures
python3 -m pytest tests/ -v --tb=short
```

---

## Test Infrastructure

### Fixtures — [`conftest.py`](conftest.py)

All shared fixtures are defined in `conftest.py` and automatically available to every test module.

| Fixture | Scope | Description |
|---------|-------|-------------|
| `backend_ports` | `session` | Returns the fixed tuple `(18001, 18002, 18003)` — the ports used by test mock backends. Avoids collisions with real backend ports (8001–8003). |
| `mock_backends` | `session` (autouse) | Spins up 3 `mock_backends.run_backend` instances on daemon threads (ports 18001, 18002, 18003). Uses `threading.Event` gates to ensure all backends are bound and listening before any test runs. Automatically starts at session begin and tears down at session end. |
| `free_port` | `function` | Binds a TCP socket to port `0` (OS-assigned), returns the port number, and releases it. Each test that needs an ephemeral port for a load balancer instance gets a unique one, preventing port conflicts between test cases. |

### Test Helpers — [`test_integration.py`](test_integration.py)

Two raw-socket HTTP client helpers are defined in the integration module:

| Helper | Purpose |
|--------|---------|
| `_send_http_get(host, port, path="/test")` | Opens a raw TCP connection, sends a hand-crafted `GET` request with `Connection: close`, reads the full response, splits on `\r\n\r\n`, and returns the body parsed as JSON. |
| `_send_http_post(host, port, path, body)` | Same pattern but sends a `POST` with `Content-Length` and an arbitrary byte body. Returns the JSON-parsed response body. |

Both helpers use raw `socket.create_connection` — no `urllib` or `requests` — to truly test the load balancer's byte-level handling.

---

## Test Modules

### `test_http_parser.py` — HTTP Parser Unit Tests

**30 tests** covering the custom byte-stream HTTP parser (`http_parser.py`), including Content-Length sentinels and chunked Transfer-Encoding.

#### `TestTryParseRequest` (5 tests)

Tests the core `try_parse_request()` function which attempts to parse a complete HTTP request from a raw byte buffer.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_incomplete_headers` | Returns `None` when the buffer contains a partial header block (no `\r\n\r\n` terminator). Ensures the parser waits for more data instead of producing garbage. |
| 2 | `test_simple_get` | Parses a complete `GET /hello HTTP/1.1` request with a `Host` header. Asserts all parsed fields: `method`, `path`, `version`, `headers["Host"]`, and that `body` is empty. |
| 3 | `test_post_with_body` | Parses a `POST` request with a JSON body and matching `Content-Length`. Verifies the body bytes are extracted correctly and completely. |
| 4 | `test_incomplete_body` | Headers are complete but `Content-Length: 100` declares a body larger than what's in the buffer (`"partial"` = 7 bytes). Returns `None`, proving the parser respects `Content-Length` before declaring the request complete. |
| 5 | `test_multiple_headers` | Parses a request with 3 headers (`Host`, `Accept`, `User-Agent`). Verifies all headers are present and correctly parsed in the `headers` dict. |

#### `TestInjectForwardedFor` (2 tests)

Tests the `X-Forwarded-For` header injection logic.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_adds_header` | When no `X-Forwarded-For` exists, a new header is created with the client IP (`10.0.0.1`). |
| 2 | `test_appends_to_existing` | When `X-Forwarded-For: 192.168.1.1` already exists (upstream proxy chain), the client IP is appended with a comma separator → `192.168.1.1, 10.0.0.5`. |

#### `TestSerialiseRequest` (2 tests)

Tests the `serialise_request()` function which rebuilds raw HTTP bytes from a parsed `HttpRequest` object.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_roundtrip` | Parses a GET request, serialises it back to bytes, then re-parses the output. Asserts `method` and `path` survive the round-trip without corruption. |
| 2 | `test_with_body` | Same round-trip test but with a POST request carrying an 11-byte body. Verifies the body survives serialisation and is identical on re-parse. |

#### `TestParseResponseStatus` (3 tests)

Tests `parse_response_status()` which extracts the status code and reason phrase from an HTTP response buffer.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_basic_200` | Parses `HTTP/1.1 200 OK\r\n` → code `200`, reason `"OK"`. |
| 2 | `test_404` | Parses `HTTP/1.1 404 Not Found\r\n` → code `404`. |
| 3 | `test_incomplete` | Buffer `HTTP/1.1 20` has no `\r\n` terminator → returns `(None, None)`. Guards against premature parsing on partial data. |

#### `TestGetContentLength` (3 tests)

Tests the `get_content_length()` utility that extracts `Content-Length` from a headers dict.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_present` | `{"Content-Length": "42"}` → returns `42`. |
| 2 | `test_absent` | Headers with no `Content-Length` key → defaults to `0`. |
| 3 | `test_case_insensitive` | `{"content-length": "10"}` (lowercase) → returns `10`. Ensures case-insensitive matching. |

#### `TestGetResponseContentLength` (4 tests)

Tests `get_response_content_length()` which peeks at `Content-Length` in a raw response byte buffer. Distinguishes three outcomes: incomplete headers (`-1`), absent header (`CONTENT_LENGTH_ABSENT` / `-2`), and an explicit length (`>= 0`).

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_present` | Full response with `Content-Length: 5` → returns `5`. |
| 2 | `test_zero_length` | `Content-Length: 0` → returns `0` (not conflated with “absent”). |
| 3 | `test_absent` | Headers complete but no `Content-Length` → returns `CONTENT_LENGTH_ABSENT` (`-2`), so the connection layer waits for close-delimited framing. |
| 4 | `test_no_headers_complete` | Incomplete response buffer (no `\r\n\r\n`) → returns `-1` to signal headers aren't done. |

#### `TestChunkedResponse` (11 tests)

Tests `is_chunked_response()` and `try_consume_chunked_body()` for backend responses with `Transfer-Encoding: chunked`.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_is_chunked_true` | Response with `Transfer-Encoding: chunked` → `True`. |
| 2 | `test_is_chunked_with_content_length_still_chunked` | Chunked wins even when `Content-Length` is also present (RFC 7230). |
| 3 | `test_is_chunked_false` | Content-Length-only response → `False`. |
| 4 | `test_is_chunked_incomplete_headers` | Partial headers → `None`. |
| 5 | `test_single_chunk` | `5\r\nhello\r\n0\r\n\r\n` parses as a complete framed body. |
| 6 | `test_multiple_chunks` | Two data chunks plus last-chunk parse completely. |
| 7 | `test_incomplete_mid_size_line` | Truncated size line → `None`. |
| 8 | `test_incomplete_mid_data` | Truncated chunk data → `None`. |
| 9 | `test_with_trailers` | Last-chunk followed by trailer headers then final CRLF. |
| 10 | `test_chunk_ext` | Size line with chunk-ext (`;foo=bar`) still parses. |
| 11 | `test_extra_bytes_after_complete` | Returns the length of the complete message and ignores trailing garbage. |

---

### `test_router.py` — Routing Algorithm Unit Tests

**17 tests** covering both routing algorithms (including `exclude=` for retries) and the factory function.

#### `TestRoundRobin` (7 tests)

Tests the `RoundRobinRouter` which cycles through healthy backends using modulo arithmetic.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_cycles_through_backends` | Calls `next_backend()` 6 times with 3 backends. Asserts the result is exactly `[B1, B2, B3, B1, B2, B3]` — a perfect cyclic sequence. |
| 2 | `test_empty_pool_returns_none` | Marks all 3 backends unhealthy, then calls `next_backend()`. Asserts it returns `None` (no crash, graceful degradation). |
| 3 | `test_mark_unhealthy_removes` | Marks backend #2 unhealthy, then calls `next_backend()` 4 times. Asserts backend #2 is never selected — proving it's excluded from the rotation. |
| 4 | `test_mark_healthy_restores` | Marks backend #1 unhealthy then immediately marks it healthy again. Verifies it reappears in `get_healthy_backends()`. |
| 5 | `test_double_mark_healthy_no_duplicate` | Calls `mark_healthy()` on an already-healthy backend. Asserts it appears exactly once in the pool — no duplicate entries that would skew distribution. |
| 6 | `test_exclude_skips_backend` | `next_backend(exclude={B1})` never returns B1. |
| 7 | `test_exclude_all_returns_none` | Excluding every healthy backend → `None`. |

#### `TestLeastConnections` (7 tests)

Tests the `LeastConnectionsRouter` which uses a min-heap to select the backend with fewest active connections.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_initial_selection` | With all backends at 0 connections, `next_backend()` returns one of the valid backends (basic sanity check). |
| 2 | `test_prefers_least_loaded` | Picks a backend (count goes to 1), disconnects it (count returns to 0), picks again. Verifies the second pick is a valid backend — confirming the heap reflects the disconnect. |
| 3 | `test_load_distribution` | Calls `next_backend()` 3 times without any disconnects. Since all start at 0, the heap should distribute picks across all 3 backends. Asserts `set(picks) == set(BACKENDS)`. |
| 4 | `test_empty_pool` | Marks all backends unhealthy. Asserts `next_backend()` returns `None`. |
| 5 | `test_disconnect_reduces_count` | Picks a backend, disconnects it, picks again. Verifies the router doesn't crash and returns a valid backend — confirming count decrement works. |
| 6 | `test_exclude_skips_backend` | With `exclude={B1}`, repeated picks never select B1; excluded heap entries are deferred correctly. |
| 7 | `test_exclude_all_returns_none` | Excluding every healthy backend → `None`. |

#### `TestFactory` (3 tests)

Tests the `create_router()` factory function.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_round_robin` | `create_router("round_robin", ...)` returns a `RoundRobinRouter` instance. |
| 2 | `test_least_connections` | `create_router("least_connections", ...)` returns a `LeastConnectionsRouter` instance. |
| 3 | `test_unknown_raises` | `create_router("random", ...)` raises `ValueError` — invalid algorithm names are rejected. |

---

### `test_health_checker.py` — Health Checker Tests

**4 tests** covering the health probe logic and the background daemon thread.

#### `TestProbe` (2 tests)

Tests the low-level `_probe()` function which performs a single HTTP `GET /health` probe.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_healthy_backend` | Probes a running mock backend on port 18001. Asserts the probe returns `True` — a valid HTTP response was received within the timeout. |
| 2 | `test_dead_backend` | Probes port 19999 where nothing is listening. Asserts the probe returns `False` — the `ConnectionRefusedError` is caught gracefully. |

#### `TestHealthChecker` (2 tests)

Tests the `HealthChecker` class which runs periodic health sweeps on a daemon thread.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_marks_dead_backend_unhealthy` | Creates a router with 2 backends on unused ports (19998, 19999). Starts the health checker with a 0.5s interval, waits 1.5s (≥ 2 sweeps), stops it. Asserts both backends were removed from the healthy pool. |
| 2 | `test_keeps_live_backend_healthy` | Creates a router with 1 live backend (mock on 18001) and 1 dead backend (port 19999). After health checking runs, asserts the live backend stayed healthy while the dead one was removed. Tests the mixed-health scenario. |

---

### `test_integration.py` — End-to-End Integration Tests

**10 tests** that spin up actual load balancer instances (reactor + router on ephemeral ports) and send real HTTP traffic through them via raw sockets.

#### Test Fixtures

| Fixture | Description |
|---------|-------------|
| `lb_round_robin` | Starts a full `Reactor` with a `RoundRobinRouter` on a free port, backed by mock backends on 18001–18003. Yields `(port, router)`. Stops the reactor on teardown. |
| `lb_least_conn` | Same as above but with a `LeastConnectionsRouter`. |

#### `TestRoundRobinIntegration` (4 tests)

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_single_request` | Sends 1 GET through the LB. Asserts the response JSON contains a `"backend"` field whose port is one of the 3 mock backends. Proves end-to-end proxying works: client → LB → backend → LB → client. |
| 2 | `test_distributes_across_backends` | Sends 6 sequential GET requests. Collects the `"backend"` field from each response into a set. Asserts all 3 backends were hit — proving round-robin distribution works under real I/O conditions (not just the unit-tested modulo logic). |
| 3 | `test_post_request` | Sends a POST with an 18-byte JSON body (`{"hello": "world"}`). Asserts the backend echoes back `"method": "POST"` and `"received_bytes": 18`. Verifies `Content-Length`-based body forwarding works end-to-end. |
| 4 | `test_503_when_no_backends` | Creates a LB with a single backend marked unhealthy. Sends a raw GET request. Asserts the response contains `503` — the load balancer correctly returns "Service Unavailable" when no healthy backends exist. Uses a standalone reactor (not the shared fixture) with its own `free_port`. |

#### `TestLeastConnectionsIntegration` (2 tests)

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_single_request` | Sends 1 GET through a least-connections LB. Asserts the JSON response contains a `"backend"` field. Basic sanity that the LC router integrates correctly with the reactor. |
| 2 | `test_distributes_across_backends` | Sends 6 sequential GET requests. Asserts all 3 backends are hit. This is the test that originally caught the **heap tiebreak fairness bug** — where deterministic `set` iteration order in `_rebuild_heap()` caused the same backend to win every time. Fixed by switching to lazy deletion. |

#### `TestXForwardedFor` (1 test)

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_header_injected` | Sends a GET to `/check-xff` through the LB. Since the mock backend doesn't echo request headers, this test verifies the request completes successfully (no crash from header injection) and the response path matches. The actual `X-Forwarded-For` injection logic is fully covered by `TestInjectForwardedFor` in the parser unit tests. |

#### `TestBackendRetries` (3 tests)

Tests idempotent failover when a backend connect fails.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_get_retries_dead_backend` | Pool is `[dead, live]`. A GET fails over from the dead port to the live mock backend and returns a successful JSON body. |
| 2 | `test_post_does_not_retry` | POST against a dead-only pool returns `502` without attempting failover (non-idempotent). |
| 3 | `test_retry_cap_respected` | With `MAX_BACKEND_RETRIES=1` and two dead backends, GET exhausts attempts and returns `502`. |

---

### `test_timeouts.py` — Per-Connection Timeout Tests

**2 tests** covering the deadline heap and state-specific timeouts.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `TestClientIdleTimeout::test_idle_client_gets_408` | Monkeypatches `CLIENT_IDLE_TIMEOUT` to 0.3s. Opens a TCP connection and sends nothing (Slowloris-style). Asserts the LB responds with `408 Request Timeout`. |
| 2 | `TestBackendTimeout::test_hung_backend_gets_504` | Spins a backend that sleeps 2s before answering; monkeypatches `BACKEND_TIMEOUT` / `CONNECT_TIMEOUT` to 0.3s and disables retries. Asserts the LB returns `504 Gateway Timeout`. |

---

### `test_workers.py` — SO_REUSEPORT Worker Tests

**1 test** (skipped when `SO_REUSEPORT` is unavailable).

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_two_reactors_bind_same_port` | Two `Reactor` instances with `require_reuseport=True` bind the same listen port. A client GET succeeds against that shared port. |

---

### `test_chunked.py` — Chunked Response Integration

**1 test** covering end-to-end chunked Transfer-Encoding passthrough.

| # | Test | What It Verifies |
|---|------|------------------|
| 1 | `test_chunked_response_relayed` | Starts a raw TCP backend that replies with `Transfer-Encoding: chunked` and two data chunks. Proxies a GET through the LB and asserts the client receives the chunked framing (`5\r\nhello\r\n`, terminating `0\r\n\r\n`) intact. |

---

## Test Matrix Summary

| Module | Test Class / Case | Tests | Type | Layer Tested |
|--------|-------------------|-------|------|-------------|
| `test_http_parser.py` | `TestTryParseRequest` | 5 | Unit | HTTP Parser |
| `test_http_parser.py` | `TestInjectForwardedFor` | 2 | Unit | Header Injection |
| `test_http_parser.py` | `TestSerialiseRequest` | 2 | Unit | Request Serialisation |
| `test_http_parser.py` | `TestParseResponseStatus` | 3 | Unit | Response Parsing |
| `test_http_parser.py` | `TestGetContentLength` | 3 | Unit | Header Extraction |
| `test_http_parser.py` | `TestGetResponseContentLength` | 4 | Unit | Response CL sentinels |
| `test_http_parser.py` | `TestChunkedResponse` | 11 | Unit | Chunked TE framing |
| `test_router.py` | `TestRoundRobin` | 7 | Unit | Round-Robin + exclude |
| `test_router.py` | `TestLeastConnections` | 7 | Unit | Least-Connections + exclude |
| `test_router.py` | `TestFactory` | 3 | Unit | Router Factory |
| `test_health_checker.py` | `TestProbe` | 2 | Component | TCP/HTTP Probing |
| `test_health_checker.py` | `TestHealthChecker` | 2 | Component | Daemon Thread + Router |
| `test_integration.py` | `TestRoundRobinIntegration` | 4 | Integration | Full proxy pipeline (RR) |
| `test_integration.py` | `TestLeastConnectionsIntegration` | 2 | Integration | Full proxy pipeline (LC) |
| `test_integration.py` | `TestXForwardedFor` | 1 | Integration | Header injection through proxy |
| `test_integration.py` | `TestBackendRetries` | 3 | Integration | Idempotent failover |
| `test_timeouts.py` | Idle / backend timeouts | 2 | Integration | Deadline heap (408 / 504) |
| `test_workers.py` | SO_REUSEPORT dual bind | 1 | Integration | Multi-reactor listen share |
| `test_chunked.py` | Chunked relay | 1 | Integration | Chunked response passthrough |
| | | **65** | | |

### Coverage by Component

```
┌────────────────────────────────────────────────────────────────┐
│ Component             │ Unit │ Component │ Integration │ Total │
├───────────────────────┼──────┼───────────┼─────────────┤───────┤
│ HTTP Parser           │  30  │     -     │      1*     │   31  │
│ Router (Round-Robin)  │   7  │     -     │      4      │   11  │
│ Router (Least-Conn)   │   7  │     -     │      2      │    9  │
│ Router Factory        │   3  │     -     │      -      │    3  │
│ Health Checker        │   -  │     4     │      -      │    4  │
│ Retries / failover    │   -  │     -     │      3      │    3  │
│ Timeouts              │   -  │     -     │      2      │    2  │
│ Workers / REUSEPORT   │   -  │     -     │      1      │    1  │
│ X-Forwarded-For       │   2† │     -     │      1      │    3  │
│ Reactor / Connection  │   -  │     -     │     14‡     │   14  │
├───────────────────────┼──────┼───────────┼─────────────┤───────┤
│ Total unique tests    │  47  │     4     │     14      │   65  │
└────────────────────────────────────────────────────────────────┘

 * chunked end-to-end relay counted under Integration
 † XFF unit tests are also part of the HTTP Parser unit count
 ‡ Reactor/Connection exercised via integration, timeouts, retries,
   workers, and chunked modules (not double-counted in the 65 total)
```

---

## Stress Testing

Beyond the pytest suite, the system can be stress-tested with industry-standard benchmarking tools to validate performance under load:

### Apache Bench (`ab`)

```bash
# 10,000 requests, 100 concurrent connections
ab -n 10000 -c 100 http://localhost:8080/

# 50,000 requests, 500 concurrent (aggressive)
ab -n 50000 -c 500 http://localhost:8080/
```

### wrk

```bash
# 30-second run, 4 threads, 400 connections
wrk -t4 -c400 -d30s http://localhost:8080/

# With a custom Lua script for POST requests
wrk -t2 -c200 -d10s -s post.lua http://localhost:8080/api
```

### Manual Verification

```bash
# Start mock backends (terminal 1)
python3 mock_backends.py

# Start load balancer (terminal 2)
python3 main.py --algorithm least_connections

# Multi-worker (SO_REUSEPORT)
python3 main.py --workers 4

# Send requests (terminal 3)
for i in $(seq 1 10); do curl -s http://localhost:8080/test | jq .backend; done
```

Expected output (least-connections, sequential): backends rotate because each connection finishes before the next begins, keeping all counts equal.
