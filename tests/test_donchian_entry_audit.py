"""
Unit tests for scripts.donchian_entry_audit (PLAN 11.56).

Covers the pure pieces — the breakout level, excursion maths, entry/exit
pairing, and the filter sweep. Offline; the bar/DB loaders are I/O and are
exercised by running the script.
"""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.donchian_entry_audit import (
    breakout_level,
    excursions,
    exits_for_entry,
    filter_sweep,
)


def _bars(rows):
    """rows: list of (date, high, low)."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _, _ in rows])
    return pd.DataFrame(
        {"high": [h for _, h, _ in rows], "low": [l for _, _, l in rows]},
        index=idx,
    )


class TestBreakoutLevel:
    def test_excludes_the_signal_bar(self):
        """The signal bar IS the new high. Including it makes the level
        equal that bar's own high, so every fill scores as 'below the
        breakout' and the metric measures nothing — the error that made
        extension look systematically negative on a first pass.
        """
        rows = [(f"2026-01-{d:02d}", 100.0 + d, 90.0) for d in range(1, 32)]
        rows[-1] = ("2026-01-31", 999.0, 90.0)          # the breakout bar
        lvl = breakout_level(_bars(rows), window=30)
        assert lvl != 999.0
        assert lvl == pytest.approx(130.0)              # highest of the prior 30

    def test_uses_the_configured_window(self):
        rows = [(f"2026-01-{d:02d}", 100.0 + d, 90.0) for d in range(1, 32)]
        wide = breakout_level(_bars(rows), window=30)
        narrow = breakout_level(_bars(rows), window=5)
        assert narrow >= wide     # a shorter lookback sits nearer the recent high

    def test_insufficient_history_is_nan_not_a_wrong_number(self):
        rows = [(f"2026-01-{d:02d}", 100.0, 90.0) for d in range(1, 6)]
        assert breakout_level(_bars(rows), window=30) != breakout_level(
            _bars(rows), window=30
        )  # NaN


class TestExcursions:
    def _frame(self):
        return _bars([
            ("2026-02-02", 105.0, 98.0),
            ("2026-02-03", 110.0, 94.0),
            ("2026-02-04", 108.0, 90.0),
            ("2026-02-05", 120.0, 99.0),
        ])

    def test_includes_the_entry_session(self):
        """The claim under test is drawdown IMMEDIATELY after filling, so
        excluding the entry day would define the question away."""
        mfe, mae = excursions(self._frame(), pd.Timestamp("2026-02-02"), 100.0, 1)
        assert mfe == pytest.approx(5.0)
        assert mae == pytest.approx(-2.0)

    def test_window_extends_over_later_sessions(self):
        mfe, mae = excursions(self._frame(), pd.Timestamp("2026-02-02"), 100.0, 3)
        assert mfe == pytest.approx(10.0)     # 110 high on day 2
        assert mae == pytest.approx(-10.0)    # 90 low on day 3

    def test_anchored_on_the_fill_not_the_close(self):
        mfe, _ = excursions(self._frame(), pd.Timestamp("2026-02-02"), 50.0, 1)
        assert mfe == pytest.approx(110.0)    # 105 vs a 50 fill

    def test_no_forward_bars_is_nan(self):
        mfe, mae = excursions(self._frame(), pd.Timestamp("2027-01-01"), 100.0, 3)
        assert mfe != mfe and mae != mae


class TestExitsForEntry:
    """A symbol can be traded repeatedly — QCOM, AAPL, AVGO, GOOG and AMZN
    each appear twice — so exits must be bounded by the NEXT entry on that
    symbol, not taken as 'everything after'."""

    ENTRIES = pd.DataFrame([
        {"symbol": "QCOM", "timestamp": "2026-05-11T00:00:00"},
        {"symbol": "QCOM", "timestamp": "2026-06-01T00:00:00"},
        {"symbol": "AMD", "timestamp": "2026-05-01T00:00:00"},
    ])
    EXITS = pd.DataFrame([
        {"symbol": "QCOM", "timestamp": "2026-05-18T00:00:00", "qty": 14, "reason": "stop", "realized_pnl": -700.0},
        {"symbol": "QCOM", "timestamp": "2026-06-09T00:00:00", "qty": 16, "reason": "stop", "realized_pnl": -530.0},
        {"symbol": "AMD", "timestamp": "2026-07-17T00:00:00", "qty": 11, "reason": "signal", "realized_pnl": 1315.0},
    ])

    def test_does_not_absorb_a_later_round_trip(self):
        got = exits_for_entry(self.EXITS, self.ENTRIES, "QCOM", "2026-05-11T00:00:00")
        assert len(got) == 1
        assert got.realized_pnl.iloc[0] == pytest.approx(-700.0)

    def test_second_entry_takes_only_its_own_exit(self):
        got = exits_for_entry(self.EXITS, self.ENTRIES, "QCOM", "2026-06-01T00:00:00")
        assert len(got) == 1
        assert got.realized_pnl.iloc[0] == pytest.approx(-530.0)

    def test_last_entry_for_a_symbol_takes_everything_after(self):
        got = exits_for_entry(self.EXITS, self.ENTRIES, "AMD", "2026-05-01T00:00:00")
        assert len(got) == 1

    def test_open_position_returns_nothing(self):
        got = exits_for_entry(self.EXITS, self.ENTRIES, "AMD", "2026-08-01T00:00:00")
        assert len(got) == 0


class TestFilterSweep:
    DF = pd.DataFrame({
        "atr_pct": [2.0, 8.0, 3.0, 9.0],
        "pnl": [500.0, -900.0, 300.0, -700.0],
    })

    def test_reports_what_the_rule_would_have_kept(self):
        out = filter_sweep(self.DF, [("atr_pct", "ATR%", ">", 5.0)])
        row = out.iloc[0]
        assert row.kept == 2 and row.skipped == 2
        assert row.pnl_kept == pytest.approx(800.0)
        assert row.delta == pytest.approx(1600.0)   # baseline was -800

    def test_reports_the_share_skipped(self):
        """A rule skipping half the entries is a different strategy, not a
        filter — the share is what makes that visible."""
        out = filter_sweep(self.DF, [("atr_pct", "ATR%", ">", 5.0)])
        assert out.iloc[0].pct_skipped == pytest.approx(50.0)

    def test_a_rule_that_removes_a_winner_scores_worse(self):
        out = filter_sweep(self.DF, [("atr_pct", "ATR%", "<", 5.0)])
        assert out.iloc[0].delta < 0

    def test_sorted_best_first(self):
        out = filter_sweep(self.DF, [
            ("atr_pct", "ATR%", "<", 5.0), ("atr_pct", "ATR%", ">", 5.0),
        ])
        assert out.iloc[0].delta >= out.iloc[1].delta
