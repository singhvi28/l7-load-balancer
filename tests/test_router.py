"""Unit tests for the routing algorithms."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from router import RoundRobinRouter, LeastConnectionsRouter, create_router


BACKENDS = [("127.0.0.1", 8001), ("127.0.0.1", 8002), ("127.0.0.1", 8003)]


class TestRoundRobin:
    def test_cycles_through_backends(self):
        rr = RoundRobinRouter(BACKENDS)
        results = [rr.next_backend() for _ in range(6)]
        assert results == BACKENDS * 2

    def test_empty_pool_returns_none(self):
        rr = RoundRobinRouter(BACKENDS)
        for b in BACKENDS:
            rr.mark_unhealthy(b)
        assert rr.next_backend() is None

    def test_mark_unhealthy_removes(self):
        rr = RoundRobinRouter(BACKENDS)
        rr.mark_unhealthy(BACKENDS[1])
        for _ in range(4):
            b = rr.next_backend()
            assert b != BACKENDS[1]

    def test_mark_healthy_restores(self):
        rr = RoundRobinRouter(BACKENDS)
        rr.mark_unhealthy(BACKENDS[0])
        rr.mark_healthy(BACKENDS[0])
        healthy = rr.get_healthy_backends()
        assert BACKENDS[0] in healthy

    def test_double_mark_healthy_no_duplicate(self):
        rr = RoundRobinRouter(BACKENDS)
        rr.mark_healthy(BACKENDS[0])  # already healthy
        assert rr.get_healthy_backends().count(BACKENDS[0]) == 1


class TestLeastConnections:
    def test_initial_selection(self):
        lc = LeastConnectionsRouter(BACKENDS)
        # All start at 0 connections, so first pick should work
        b = lc.next_backend()
        assert b in BACKENDS

    def test_prefers_least_loaded(self):
        lc = LeastConnectionsRouter(BACKENDS)
        # Pick first backend (increments its count to 1)
        first = lc.next_backend()
        # Disconnect it (back to 0)
        lc.on_disconnect(first)
        # Now all are at 0 again; pick another
        second = lc.next_backend()
        assert second in BACKENDS

    def test_load_distribution(self):
        lc = LeastConnectionsRouter(BACKENDS)
        picks = [lc.next_backend() for _ in range(3)]
        # With 3 backends at 0 connections, all 3 should be picked
        assert set(picks) == set(BACKENDS)

    def test_empty_pool(self):
        lc = LeastConnectionsRouter(BACKENDS)
        for b in BACKENDS:
            lc.mark_unhealthy(b)
        assert lc.next_backend() is None

    def test_disconnect_reduces_count(self):
        lc = LeastConnectionsRouter(BACKENDS)
        b = lc.next_backend()
        lc.on_disconnect(b)
        # Should be at 0 now — verify by picking again
        b2 = lc.next_backend()
        assert b2 in BACKENDS


class TestFactory:
    def test_round_robin(self):
        r = create_router("round_robin", BACKENDS)
        assert isinstance(r, RoundRobinRouter)

    def test_least_connections(self):
        r = create_router("least_connections", BACKENDS)
        assert isinstance(r, LeastConnectionsRouter)

    def test_unknown_raises(self):
        import pytest
        with pytest.raises(ValueError):
            create_router("random", BACKENDS)
