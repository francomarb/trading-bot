"""Symbol-level locks for operator-driven destructive commands.

Operator Controls Phase C primitive. Wraps the foundation's durable
DB-level uniqueness constraints (``uniq_one_active_position_per_owner_key``
and ``uniq_one_active_close_per_position``) with an in-memory registry
so the engine can:

  1. Fail fast — reject competing strategy or operator actions BEFORE
     a broker submit attempts the DB constraint and gets a noisy
     IntegrityError back.
  2. Carry an audit reason — "owner_key=AAPL locked by
     operator_command cmd_<hex>" is more useful in logs than the bare
     constraint name.
  3. Express scopes the DB constraints don't cover by themselves
     (proposal §11.1) — e.g. blocking a NEW strategy entry on AAPL
     while an operator close is mid-flight, even though entry and
     close are on different DB indexes.

The DB constraints remain authoritative. This module is the
application-layer wrapper; on a race where the in-memory lock
disagrees with the DB, the DB wins and the in-memory state catches up
via release on terminal events.

Thread-safety: a single ``RLock`` guards the registry. Heartbeat
thread + cycle thread + Telegram listener thread all read/write
through the registry; SQLite-side serialisation is irrelevant here
because the registry never touches the database. Re-entrant lock so a
handler can acquire then call into engine code that performs an
``is_locked`` check without deadlocking.

The registry is engine-process-local. A bot restart wipes it — that
is intentional: on restart, the substrate's startup reconcile pass
walks non-terminal rows and the engine re-derives the locking state
implicitly. No locks "persist" across restart; an in-flight operator
command crosses restart via the operator_commands queue's `executing`
status, not via this registry.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class LockHolder:
    """Identity of whoever holds a lock.

    ``kind`` is one of:
      - ``operator_command`` — operator-issued command (cmd_<hex>)
      - ``strategy_exit`` — engine's automatic close-position path
      - ``stop_recovery`` — substrate-driven stop-fill recovery
      - ``test`` — synthetic in unit tests

    ``identifier`` is the disambiguator (operator command uid,
    strategy name, etc.). Together they make the lock holder unique
    for logging / debugging purposes; the lock itself is keyed only on
    ``owner_key``.
    """

    kind: str
    identifier: str
    acquired_at: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.identifier}"


class SymbolLockRegistry:
    """Engine-process-local mutex over `owner_key` strings.

    Mirrors the proposal §11.1 "central lock registry" requirement.
    Every broker mutation path (operator destructive commands, future
    refactored strategy close path) calls ``acquire`` immediately
    before broker dispatch. Release happens after broker confirmation
    AND after substrate row transition (whichever is later in the
    handler).

    All public methods are O(1) and thread-safe.
    """

    def __init__(self) -> None:
        # Re-entrant lock so a handler holding the lock can call into
        # engine code that does an `is_locked` check without
        # deadlocking. The internal dict is mutated only under this
        # lock; reads return safe snapshots.
        self._mu = threading.RLock()
        self._held: dict[str, LockHolder] = {}

    def acquire(
        self,
        *,
        owner_key: str,
        kind: str,
        identifier: str,
    ) -> LockHolder | None:
        """Try to acquire the lock for ``owner_key``.

        Returns the freshly-installed ``LockHolder`` on success.
        Returns None when the lock is already held — the caller MUST
        treat this as a rejection (do not retry without inspecting
        the existing holder via ``is_locked``).

        ``owner_key`` is the foundation's engine.positions.owner_key_for
        result (ticker / underlying / spread UUID). The registry does
        NOT validate it; the caller is expected to normalise.
        """
        if not owner_key:
            raise ValueError("owner_key must be a non-empty string")
        if not kind:
            raise ValueError("kind must be a non-empty string")
        if not identifier:
            raise ValueError("identifier must be a non-empty string")
        holder = LockHolder(
            kind=kind,
            identifier=identifier,
            acquired_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._mu:
            existing = self._held.get(owner_key)
            if existing is not None:
                return None
            self._held[owner_key] = holder
            return holder

    def release(
        self,
        *,
        owner_key: str,
        holder: LockHolder | None = None,
    ) -> bool:
        """Release the lock for ``owner_key``.

        When ``holder`` is provided, the release is asserted to match
        the current holder — defensive against an operator handler
        accidentally releasing a strategy-held lock. When omitted,
        any holder is released (used by the engine's shutdown sweep
        and tests).

        Returns True if a lock was released, False if no lock was held.
        Raises ``ValueError`` when ``holder`` does not match.
        """
        with self._mu:
            existing = self._held.get(owner_key)
            if existing is None:
                return False
            if holder is not None and existing != holder:
                raise ValueError(
                    f"lock holder mismatch for owner_key={owner_key!r}: "
                    f"caller={holder!s} but actual={existing!s}"
                )
            del self._held[owner_key]
            return True

    def is_locked(self, owner_key: str) -> LockHolder | None:
        """Return the current holder if locked, None if free.

        Snapshot read — the holder may have been released by another
        thread between this call returning and the caller acting on
        the result. Callers that need atomic "check and dispatch"
        must use ``acquire`` (which is atomic) rather than
        ``is_locked`` then ``acquire``.
        """
        with self._mu:
            return self._held.get(owner_key)

    def snapshot(self) -> dict[str, LockHolder]:
        """Read-only snapshot of all held locks. Deep-copy for safe
        iteration outside the lock. Used by status-style displays."""
        with self._mu:
            return dict(self._held)

    def clear(self) -> None:
        """Drop all locks. Used by engine shutdown and tests. NOT
        called during normal command processing — production releases
        are explicit and matched."""
        with self._mu:
            self._held.clear()

    def __len__(self) -> int:
        with self._mu:
            return len(self._held)
