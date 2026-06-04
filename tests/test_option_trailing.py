import sqlite3

import pytest

from engine.option_trailing import (
    OptionTrailingStopStore,
    _CREATE_OPTION_TRAILING_STOPS_SQL,
    _OPTION_TRAILING_STOPS_INDEXES_SQL,
)


@pytest.fixture
def store() -> OptionTrailingStopStore:
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_OPTION_TRAILING_STOPS_SQL)
    for sql in _OPTION_TRAILING_STOPS_INDEXES_SQL:
        conn.execute(sql)
    return OptionTrailingStopStore(conn)


class TestOptionTrailingStopStore:
    def test_upsert_round_trips_by_position_uid_and_occ(self, store):
        store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.13,
            alpaca_stop_order_id="stop-1",
            stop_order_status="accepted",
            last_observed_premium=20.16,
        )

        row = store.get_by_occ("SPY260618C00746000")

        assert row is not None
        assert row.position_uid == "pos_abc123"
        assert row.occ_symbol == "SPY260618C00746000"
        assert row.strategy == "generic_single_leg_options"
        assert row.hwm_premium == pytest.approx(20.16)
        assert row.current_stop_price == pytest.approx(17.13)

    def test_requires_position_uid(self, store):
        with pytest.raises(ValueError, match="position_uid"):
            store.upsert(
                position_uid="",
                occ_symbol="SPY260618C00746000",
                strategy="generic_single_leg_options",
                owner_key="SPY",
                qty=1,
                entry_premium=10.0,
                hwm_premium=10.0,
                trail_activation_pct=0.10,
                trail_pct=0.15,
            )
