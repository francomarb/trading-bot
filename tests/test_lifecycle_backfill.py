"""Unit tests for `PositionLifecycleStore.synthesize_for_existing` —
the backfill helper called by `engine.trader._backfill_position_lifecycle()`
on startup for broker-open positions that have no lifecycle row yet.

Operator Controls Phase A. Verifies:

- A new lifecycle row is created in `open` status with `synthesized=true`
  metadata.
- Calling the helper twice for the same owner_key is idempotent —
  no duplicate rows.
- Legs are persisted for spread backfill.
- An owner_key that already has a non-terminal lifecycle row is
  recognised; backfill returns that uid and does not create a new row.
"""

from __future__ import annotations

import pytest

from engine.lifecycle import (
    PositionLifecycleLeg,
    PositionLifecycleStore,
    new_position_uid,
)
from reporting.logger import TradeLogger


@pytest.fixture
def store(tmp_path) -> PositionLifecycleStore:
    db_path = tmp_path / "trades.db"
    conn = TradeLogger(path=str(db_path))._ensure_db()
    return PositionLifecycleStore(conn)


class TestSynthesizeForExisting:
    def test_creates_open_row_with_synthesized_metadata(self, store):
        uid = store.synthesize_for_existing(
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            current_qty=10.0,
            avg_entry_price=884.20,
        )
        row = store.get_by_position_uid(uid)
        assert row is not None
        assert row.status == "open"
        assert row.symbol == "NVDA"
        assert row.owner_key == "NVDA"
        assert row.current_qty == 10.0
        assert row.avg_entry_price == 884.20
        assert row.metadata.get("synthesized") is True
        assert row.first_fill_at is not None

    def test_idempotent_for_same_owner_key(self, store):
        uid_first = store.synthesize_for_existing(
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            current_qty=10.0,
            avg_entry_price=884.20,
        )
        uid_second = store.synthesize_for_existing(
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            current_qty=10.0,
            avg_entry_price=884.20,
        )
        assert uid_first == uid_second
        # Exactly one row exists for owner_key=NVDA.
        all_rows = store.get_open()
        nvda_rows = [r for r in all_rows if r.owner_key == "NVDA"]
        assert len(nvda_rows) == 1

    def test_does_not_overwrite_existing_open_row(self, store):
        """If `create_pending` + `mark_open` already produced a row for
        this owner_key (normal flow), backfill must return that row's
        uid and leave it alone — no metadata 'synthesized' flag added."""
        original_uid = new_position_uid()
        store.create_pending(
            position_uid=original_uid,
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            entry_qty=10.0,
        )
        store.mark_open(
            position_uid=original_uid,
            avg_entry_price=884.20,
            current_qty=10.0,
        )

        returned_uid = store.synthesize_for_existing(
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            current_qty=10.0,
            avg_entry_price=884.20,
        )
        assert returned_uid == original_uid
        row = store.get_by_position_uid(original_uid)
        # Synthesized flag NEVER added to a real lifecycle row.
        assert row.metadata.get("synthesized") is not True

    def test_persists_spread_legs(self, store):
        legs = [
            PositionLifecycleLeg("placeholder", "SPY250620C00450000", "buy", 1.0, 5.20),
            PositionLifecycleLeg("placeholder", "SPY250620C00460000", "sell", 1.0, 2.40),
        ]
        uid = store.synthesize_for_existing(
            symbol="SPY",
            owner_key="some-spread-uuid",
            strategy="credit_spread",
            position_type="spread",
            current_qty=1.0,
            avg_entry_price=2.80,
            legs=legs,
        )
        row = store.get_by_position_uid(uid)
        assert len(row.legs) == 2
        # Leg position_uid was re-keyed from placeholder to the
        # generated uid.
        for leg in row.legs:
            assert leg.position_uid == uid

    def test_separate_owner_keys_get_separate_rows(self, store):
        uid_nvda = store.synthesize_for_existing(
            symbol="NVDA", owner_key="NVDA", strategy="sma_crossover",
            position_type="single_leg",
            current_qty=10.0, avg_entry_price=884.20,
        )
        uid_mu = store.synthesize_for_existing(
            symbol="MU", owner_key="MU", strategy="sma_crossover",
            position_type="single_leg",
            current_qty=20.0, avg_entry_price=116.30,
        )
        assert uid_nvda != uid_mu
        rows = store.get_open()
        assert len(rows) == 2
