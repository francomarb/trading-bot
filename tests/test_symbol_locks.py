"""Tests for the Phase C symbol-lock registry.

Pure-Python module — no engine, broker, or DB integration. Verifies
the contract that the engine handlers rely on.
"""

from __future__ import annotations

import threading
import time

import pytest

from engine.symbol_locks import LockHolder, SymbolLockRegistry


class TestAcquireRelease:
    def test_first_acquire_succeeds(self):
        reg = SymbolLockRegistry()
        holder = reg.acquire(
            owner_key="AAPL",
            kind="operator_command",
            identifier="cmd_abc",
        )
        assert holder is not None
        assert holder.kind == "operator_command"
        assert holder.identifier == "cmd_abc"

    def test_second_acquire_returns_none(self):
        reg = SymbolLockRegistry()
        first = reg.acquire(
            owner_key="AAPL", kind="operator_command", identifier="cmd_a",
        )
        second = reg.acquire(
            owner_key="AAPL", kind="strategy_exit", identifier="sma_crossover",
        )
        assert first is not None
        assert second is None

    def test_different_owner_keys_independent(self):
        reg = SymbolLockRegistry()
        a = reg.acquire(
            owner_key="AAPL", kind="operator_command", identifier="c1",
        )
        b = reg.acquire(
            owner_key="MU", kind="operator_command", identifier="c2",
        )
        assert a is not None
        assert b is not None
        assert len(reg) == 2

    def test_release_with_matching_holder(self):
        reg = SymbolLockRegistry()
        h = reg.acquire(owner_key="AAPL", kind="op", identifier="c")
        assert reg.release(owner_key="AAPL", holder=h) is True
        assert reg.is_locked("AAPL") is None

    def test_release_with_no_lock_returns_false(self):
        reg = SymbolLockRegistry()
        assert reg.release(owner_key="AAPL") is False

    def test_release_after_release_is_idempotent_to_false(self):
        reg = SymbolLockRegistry()
        h = reg.acquire(owner_key="AAPL", kind="op", identifier="c")
        reg.release(owner_key="AAPL", holder=h)
        assert reg.release(owner_key="AAPL", holder=h) is False

    def test_release_with_mismatched_holder_raises(self):
        reg = SymbolLockRegistry()
        reg.acquire(owner_key="AAPL", kind="op", identifier="c1")
        wrong = LockHolder(kind="op", identifier="c2", acquired_at="2026-01-01")
        with pytest.raises(ValueError, match="holder mismatch"):
            reg.release(owner_key="AAPL", holder=wrong)

    def test_release_without_holder_arg_force_clears(self):
        """`release(owner_key=..., holder=None)` is used by the engine
        shutdown sweep — any holder is dropped."""
        reg = SymbolLockRegistry()
        reg.acquire(owner_key="AAPL", kind="op", identifier="c")
        assert reg.release(owner_key="AAPL") is True
        assert reg.is_locked("AAPL") is None

    def test_acquire_after_release_succeeds(self):
        reg = SymbolLockRegistry()
        first = reg.acquire(owner_key="AAPL", kind="op", identifier="c1")
        reg.release(owner_key="AAPL", holder=first)
        second = reg.acquire(owner_key="AAPL", kind="op", identifier="c2")
        assert second is not None
        assert second.identifier == "c2"

    def test_empty_owner_key_rejected(self):
        reg = SymbolLockRegistry()
        with pytest.raises(ValueError):
            reg.acquire(owner_key="", kind="op", identifier="c")

    def test_empty_kind_rejected(self):
        reg = SymbolLockRegistry()
        with pytest.raises(ValueError):
            reg.acquire(owner_key="AAPL", kind="", identifier="c")

    def test_empty_identifier_rejected(self):
        reg = SymbolLockRegistry()
        with pytest.raises(ValueError):
            reg.acquire(owner_key="AAPL", kind="op", identifier="")


class TestObservability:
    def test_is_locked_returns_holder(self):
        reg = SymbolLockRegistry()
        h = reg.acquire(owner_key="AAPL", kind="op", identifier="c")
        observed = reg.is_locked("AAPL")
        assert observed == h

    def test_is_locked_returns_none_when_free(self):
        reg = SymbolLockRegistry()
        assert reg.is_locked("AAPL") is None

    def test_snapshot_returns_deep_copy(self):
        reg = SymbolLockRegistry()
        reg.acquire(owner_key="AAPL", kind="op", identifier="c1")
        reg.acquire(owner_key="MU", kind="op", identifier="c2")
        snap = reg.snapshot()
        assert set(snap.keys()) == {"AAPL", "MU"}
        # Mutate snapshot — registry must be unaffected.
        snap.clear()
        assert len(reg) == 2

    def test_str_format(self):
        h = LockHolder(kind="op", identifier="cmd_abc", acquired_at="2026")
        assert str(h) == "op:cmd_abc"


class TestThreading:
    """Concurrent acquire from N threads on the same owner_key —
    exactly one wins. This is the contract the engine handler depends
    on: heartbeat thread + cycle thread + Telegram thread can race on
    the same symbol and only one walks away with the lock."""

    def test_only_one_thread_wins_acquire_race(self):
        reg = SymbolLockRegistry()
        winners: list[LockHolder | None] = []
        start = threading.Barrier(20)

        def worker(i):
            start.wait()
            h = reg.acquire(
                owner_key="AAPL", kind="op", identifier=f"c{i}",
            )
            winners.append(h)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        successful = [w for w in winners if w is not None]
        rejected = [w for w in winners if w is None]
        assert len(successful) == 1, (
            f"expected exactly one acquire to win; "
            f"got {len(successful)} winners"
        )
        assert len(rejected) == 19

    def test_re_entrant_acquire_from_same_thread_still_blocked(self):
        """RLock is for serialising the registry's own internal state;
        a holder acquiring a SECOND time on the same owner_key still
        returns None. The re-entrance is for nested code on the
        thread holding the lock that wants to call `is_locked` etc.
        without deadlocking."""
        reg = SymbolLockRegistry()
        first = reg.acquire(owner_key="AAPL", kind="op", identifier="c1")
        # Same thread re-acquire on the same key fails by design.
        second = reg.acquire(owner_key="AAPL", kind="op", identifier="c2")
        assert first is not None
        assert second is None

    def test_concurrent_release_and_acquire_dont_deadlock(self):
        reg = SymbolLockRegistry()
        h = reg.acquire(owner_key="AAPL", kind="op", identifier="c1")

        ok = []
        def release_thread():
            time.sleep(0.05)
            ok.append(reg.release(owner_key="AAPL", holder=h))

        def acquire_thread():
            # Spin until released.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                got = reg.acquire(owner_key="AAPL", kind="op", identifier="c2")
                if got is not None:
                    ok.append(got)
                    return
                time.sleep(0.01)
            ok.append(None)

        t1 = threading.Thread(target=release_thread)
        t2 = threading.Thread(target=acquire_thread)
        t1.start()
        t2.start()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
        assert True in ok
        assert any(isinstance(x, LockHolder) for x in ok)


class TestClear:
    def test_clear_drops_all(self):
        reg = SymbolLockRegistry()
        reg.acquire(owner_key="AAPL", kind="op", identifier="c1")
        reg.acquire(owner_key="MU", kind="op", identifier="c2")
        reg.clear()
        assert len(reg) == 0
        assert reg.is_locked("AAPL") is None
        assert reg.is_locked("MU") is None
