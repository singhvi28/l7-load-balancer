"""
Routing algorithms for backend selection.

Supported strategies
────────────────────
• **Round-Robin** — simple cyclic iteration over the healthy pool.
• **Least Connections** — O(log N) selection via a min-heap keyed on active
  connection count.

Thread-safety: the health-checker mutates the healthy pool from a daemon
thread, so all shared state is guarded by a ``threading.Lock``.
"""

from __future__ import annotations

import heapq
import threading
from typing import List, Optional, Tuple

Backend = Tuple[str, int]  # (host, port)


class RoundRobinRouter:
    """Cycle through healthy backends with modulo arithmetic."""

    def __init__(self, backends: List[Backend]) -> None:
        self._lock = threading.Lock()
        self._all_backends: List[Backend] = list(backends)
        self._healthy: List[Backend] = list(backends)
        self._index: int = 0

    # ── Selection ─────────────────────────────────────────────────────────

    def next_backend(self) -> Optional[Backend]:
        with self._lock:
            if not self._healthy:
                return None
            backend = self._healthy[self._index % len(self._healthy)]
            self._index += 1
            return backend

    # ── Health management ─────────────────────────────────────────────────

    def mark_unhealthy(self, backend: Backend) -> None:
        with self._lock:
            if backend in self._healthy:
                self._healthy.remove(backend)

    def mark_healthy(self, backend: Backend) -> None:
        with self._lock:
            if backend not in self._healthy:
                self._healthy.append(backend)

    def get_all_backends(self) -> List[Backend]:
        with self._lock:
            return list(self._all_backends)

    def get_healthy_backends(self) -> List[Backend]:
        with self._lock:
            return list(self._healthy)

    # ── Stubs so both routers share the same interface ────────────────────

    def on_connect(self, backend: Backend) -> None:  # noqa: D401
        """No-op for round-robin."""

    def on_disconnect(self, backend: Backend) -> None:  # noqa: D401
        """No-op for round-robin."""


class LeastConnectionsRouter:
    """Select the backend with the fewest active connections.

    Internal structure
    ──────────────────
    ``_heap`` — min-heap of ``(active_count, tiebreak, (host, port))``.
    ``_counts`` — ``dict[Backend, int]`` canonical count per backend.
    ``_tiebreak`` — monotonic counter to stabilise heap ordering.

    Uses **lazy deletion**: stale heap entries (whose count no longer matches
    the canonical ``_counts`` value) are skipped during ``next_backend``.
    This avoids the fairness bug of full heap rebuilds where deterministic
    set-iteration order biased tiebreaks toward the same backend.
    """

    def __init__(self, backends: List[Backend]) -> None:
        self._lock = threading.Lock()
        self._all_backends: List[Backend] = list(backends)
        self._counts: dict[Backend, int] = {b: 0 for b in backends}
        self._tiebreak: int = 0
        self._heap: List[Tuple[int, int, Backend]] = []
        self._healthy_set: set[Backend] = set(backends)
        # Initial heap — one entry per backend
        for b in backends:
            self._tiebreak += 1
            heapq.heappush(self._heap, (0, self._tiebreak, b))

    # ── Selection ─────────────────────────────────────────────────────────

    def next_backend(self) -> Optional[Backend]:
        with self._lock:
            # Drain unhealthy and stale entries
            while self._heap:
                count, tb, backend = self._heap[0]
                if backend not in self._healthy_set:
                    heapq.heappop(self._heap)
                    continue
                if count != self._counts.get(backend, 0):
                    heapq.heappop(self._heap)  # stale entry
                    continue
                break
            else:
                return None

            count, tb, backend = heapq.heappop(self._heap)
            # Increment and push back with a new tiebreak
            new_count = count + 1
            self._counts[backend] = new_count
            self._tiebreak += 1
            heapq.heappush(self._heap, (new_count, self._tiebreak, backend))
            return backend

    # ── Connection tracking ───────────────────────────────────────────────

    def on_connect(self, backend: Backend) -> None:
        """Called after a connection to *backend* is established.

        Note: ``next_backend`` already increments the count, so this is a
        no-op by default.  Override if you want two-phase tracking.
        """

    def on_disconnect(self, backend: Backend) -> None:
        """Decrement active connections and push an updated heap entry."""
        with self._lock:
            current = self._counts.get(backend, 0)
            new_count = max(0, current - 1)
            self._counts[backend] = new_count
            # Push updated entry; stale entries are skipped in next_backend
            self._tiebreak += 1
            heapq.heappush(self._heap, (new_count, self._tiebreak, backend))

    # ── Health management ─────────────────────────────────────────────────

    def mark_unhealthy(self, backend: Backend) -> None:
        with self._lock:
            self._healthy_set.discard(backend)
            # Stale entries will be drained lazily in next_backend

    def mark_healthy(self, backend: Backend) -> None:
        with self._lock:
            if backend not in self._healthy_set:
                self._healthy_set.add(backend)
                self._counts.setdefault(backend, 0)
                self._tiebreak += 1
                heapq.heappush(
                    self._heap,
                    (self._counts[backend], self._tiebreak, backend),
                )

    def get_all_backends(self) -> List[Backend]:
        with self._lock:
            return list(self._all_backends)

    def get_healthy_backends(self) -> List[Backend]:
        with self._lock:
            return list(self._healthy_set)


# ─── Factory ──────────────────────────────────────────────────────────────────

def create_router(
    algorithm: str, backends: List[Backend]
) -> RoundRobinRouter | LeastConnectionsRouter:
    """Instantiate the appropriate router for *algorithm*."""
    if algorithm == "round_robin":
        return RoundRobinRouter(backends)
    if algorithm == "least_connections":
        return LeastConnectionsRouter(backends)
    raise ValueError(f"Unknown routing algorithm: {algorithm!r}")
