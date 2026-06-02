"""
Unit tests for scripts/backfill_spread_r_multiple.py.

Covers the happy path (open + close backfilled with correct math), the
idempotency requirement (re-run is a no-op), and the documented skip
conditions (missing open, unparseable OCC, degenerate basis).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from reporting.logger import TradeLogger
from scripts.backfill_spread_r_multiple import (
    _build_plans,
    _load_spread_legs,
    _parse_strike,
    main,
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def tl(tmp_path: Path):
    """Schema-correct trades.db backed by the real TradeLogger."""
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    yield logger
    logger.close()


def _seed_open(tl: TradeLogger, *, position_id: str, short_occ: str,
               long_occ: str, qty: float, net_credit: float,
               strategy: str = "credit_spread") -> None:
    tl.log_spread_fill(
        position_id=position_id,
        strategy=strategy,
        short_occ=short_occ,
        long_occ=long_occ,
        qty=qty,
        net_price=net_credit,
        opening=True,
    )


def _seed_close(tl: TradeLogger, *, position_id: str, short_occ: str,
                long_occ: str, qty: float, net_debit: float,
                realized_pnl: float,
                strategy: str = "credit_spread") -> None:
    tl.log_spread_fill(
        position_id=position_id,
        strategy=strategy,
        short_occ=short_occ,
        long_occ=long_occ,
        qty=qty,
        net_price=net_debit,
        opening=False,
        realized_pnl=realized_pnl,
    )


def _read_short_leg(conn: sqlite3.Connection, position_id: str,
                    realized_pnl_is_null: bool) -> dict:
    is_null = "IS NULL" if realized_pnl_is_null else "IS NOT NULL"
    cur = conn.execute(
        f"SELECT id, side, symbol, qty, avg_fill_price, realized_pnl, "
        f"initial_risk_dollars, r_multiple "
        f"FROM trades "
        f"WHERE position_id = ? AND position_type = 'spread' "
        f"AND realized_pnl {is_null} "
        # Open: short=sell. Close: short=buy. Either way, the row that
        # carries net_price + the basis is the "short side" of the legs.
        f"ORDER BY id ASC",
        (position_id,),
    )
    rows = cur.fetchall()
    # Open: short=sell (first row). Close: short=buy (first row).
    return {
        "id": rows[0][0], "side": rows[0][1], "symbol": rows[0][2],
        "qty": rows[0][3], "avg_fill_price": rows[0][4],
        "realized_pnl": rows[0][5],
        "initial_risk_dollars": rows[0][6], "r_multiple": rows[0][7],
    }


# ── _parse_strike ──────────────────────────────────────────────────────


class TestParseStrike:
    def test_standard_occ(self):
        assert _parse_strike("SPY260618P00689000") == pytest.approx(689.0)

    def test_high_strike(self):
        assert _parse_strike("QQQ260626P00674500") == pytest.approx(674.5)

    def test_non_occ_returns_none(self):
        assert _parse_strike("AAPL") is None

    def test_empty_returns_none(self):
        assert _parse_strike("") is None


# ── Happy path ────────────────────────────────────────────────────────


class TestBackfill:
    def test_open_and_close_both_get_basis_and_r_multiple(self, tl, tmp_path):
        # Bull-put: width=15 (689−674), net_credit=2.54/sh, qty=1
        # → max_loss = (15 − 2.54) × 100 × 1 = 1246
        # Close at 0.80 debit → realized = (2.54 − 0.80) × 100 × 1 = 174
        # → r_multiple = 174 / 1246 ≈ 0.13965
        _seed_open(
            tl, position_id="uuid-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_credit=2.54,
        )
        _seed_close(
            tl, position_id="uuid-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_debit=0.80, realized_pnl=174.0,
        )
        # Sanity: pre-backfill both columns are NULL on both rows.
        conn = tl._ensure_db()
        before_open = _read_short_leg(conn, "uuid-1", realized_pnl_is_null=True)
        before_close = _read_short_leg(conn, "uuid-1", realized_pnl_is_null=False)
        assert before_open["initial_risk_dollars"] is None
        assert before_close["initial_risk_dollars"] is None
        assert before_close["r_multiple"] is None

        rc = main(["--db", str(tmp_path / "trades.db"), "--apply"])
        assert rc == 0

        after_open = _read_short_leg(conn, "uuid-1", realized_pnl_is_null=True)
        after_close = _read_short_leg(conn, "uuid-1", realized_pnl_is_null=False)
        assert after_open["initial_risk_dollars"] == pytest.approx(1246.0)
        assert after_close["initial_risk_dollars"] == pytest.approx(1246.0)
        assert after_close["r_multiple"] == pytest.approx(174.0 / 1246.0)

    def test_dry_run_does_not_write(self, tl, tmp_path, capsys):
        _seed_open(
            tl, position_id="uuid-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_credit=2.54,
        )
        _seed_close(
            tl, position_id="uuid-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_debit=0.80, realized_pnl=174.0,
        )
        rc = main(["--db", str(tmp_path / "trades.db")])  # no --apply
        assert rc == 0
        captured = capsys.readouterr()
        assert "dry-run only" in captured.out

        conn = tl._ensure_db()
        after_open = _read_short_leg(conn, "uuid-1", realized_pnl_is_null=True)
        after_close = _read_short_leg(conn, "uuid-1", realized_pnl_is_null=False)
        assert after_open["initial_risk_dollars"] is None
        assert after_close["initial_risk_dollars"] is None
        assert after_close["r_multiple"] is None

    def test_open_only_position_is_backfilled(self, tl, tmp_path):
        # Position opened but not yet closed — backfill writes basis on
        # the open's short leg so a future close (via the post-PR-34
        # logger fallback) can compute r_multiple.
        _seed_open(
            tl, position_id="uuid-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_credit=2.54,
        )
        rc = main(["--db", str(tmp_path / "trades.db"), "--apply"])
        assert rc == 0
        conn = tl._ensure_db()
        after_open = _read_short_leg(conn, "uuid-1", realized_pnl_is_null=True)
        assert after_open["initial_risk_dollars"] == pytest.approx(1246.0)

    def test_idempotent_second_run(self, tl, tmp_path, capsys):
        _seed_open(
            tl, position_id="uuid-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_credit=2.54,
        )
        _seed_close(
            tl, position_id="uuid-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_debit=0.80, realized_pnl=174.0,
        )
        main(["--db", str(tmp_path / "trades.db"), "--apply"])
        capsys.readouterr()  # discard
        rc = main(["--db", str(tmp_path / "trades.db"), "--apply"])
        assert rc == 0
        captured = capsys.readouterr()
        # Second run reports no eligible plans (already populated).
        assert "plans (eligible for backfill): 0" in captured.out

    def test_multi_contract_scales_basis(self, tl, tmp_path):
        # 5 contracts → basis = 5 × 1246 = 6230, realized = 5 × 174 = 870,
        # r_multiple = 870 / 6230 = same per-contract R (sizing-invariant).
        _seed_open(
            tl, position_id="uuid-5",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=5, net_credit=2.54,
        )
        _seed_close(
            tl, position_id="uuid-5",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=5, net_debit=0.80, realized_pnl=870.0,
        )
        main(["--db", str(tmp_path / "trades.db"), "--apply"])
        conn = tl._ensure_db()
        after_close = _read_short_leg(conn, "uuid-5", realized_pnl_is_null=False)
        assert after_close["initial_risk_dollars"] == pytest.approx(6230.0)
        assert after_close["r_multiple"] == pytest.approx(870.0 / 6230.0)
        # Identical R to the qty=1 case in test_open_and_close_both_get_basis.
        assert after_close["r_multiple"] == pytest.approx(174.0 / 1246.0)


# ── Skip conditions ───────────────────────────────────────────────────


class TestSkipConditions:
    def test_skips_when_no_open_row(self, tl, tmp_path, capsys):
        # Only a close row (e.g. external-close detection without a prior
        # matching open in the DB) — cannot infer basis, must be skipped
        # without writing anything. The skip-reason classifier may flag
        # this as "no open short-leg row found" OR "missing leg OCC
        # symbols" depending on which side mimics the open-row shape
        # first; either is acceptable, what we must guarantee is that
        # no UPDATE happens and the close row stays NULL.
        _seed_close(
            tl, position_id="orphan-1",
            short_occ="SPY260618P00689000", long_occ="SPY260618P00674000",
            qty=1, net_debit=0.80, realized_pnl=174.0,
        )
        rc = main(["--db", str(tmp_path / "trades.db"), "--apply"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "orphan-1: skipped" in captured.out
        conn = tl._ensure_db()
        after_close = _read_short_leg(conn, "orphan-1", realized_pnl_is_null=False)
        assert after_close["initial_risk_dollars"] is None
        assert after_close["r_multiple"] is None

    def test_skips_degenerate_basis(self, tl, tmp_path, capsys):
        # net_credit ≥ width: max-loss is ≤ 0, the position cannot be a
        # valid defined-risk spread. Skip rather than write a nonsense
        # basis / r_multiple.
        _seed_open(
            tl, position_id="bad-1",
            # width = 1.0 (689 − 688), but net_credit = 1.5 → degenerate.
            short_occ="SPY260618P00689000", long_occ="SPY260618P00688000",
            qty=1, net_credit=1.5,
        )
        rc = main(["--db", str(tmp_path / "trades.db"), "--apply"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "degenerate basis" in captured.out
        conn = tl._ensure_db()
        after_open = _read_short_leg(conn, "bad-1", realized_pnl_is_null=True)
        assert after_open["initial_risk_dollars"] is None

    def test_skips_unparseable_occ(self, tl, tmp_path, capsys):
        # Defensive: if a legacy row's symbol isn't a valid OCC string
        # (e.g. a strategy with a hand-rolled identifier), skip it.
        conn = TradeLogger(path=str(tmp_path / "trades.db"))._ensure_db()
        conn.execute(
            "INSERT INTO trades ("
            "timestamp, symbol, side, qty, avg_fill_price, order_id, "
            "strategy, reason, stop_price, entry_reference_price, "
            "modeled_slippage_bps, realized_slippage_bps, order_type, "
            "status, requested_qty, filled_qty, position_id, position_type"
            ") VALUES ('2026-05-01T00:00:00+00:00', 'NOT_OCC', 'sell', "
            "1, 2.54, NULL, 'credit_spread', 'spread entry', 0, 0, 0, 0, "
            "'mleg', 'filled', 1, 1, 'bad-occ-1', 'spread')"
        )
        conn.execute(
            "INSERT INTO trades ("
            "timestamp, symbol, side, qty, avg_fill_price, order_id, "
            "strategy, reason, stop_price, entry_reference_price, "
            "modeled_slippage_bps, realized_slippage_bps, order_type, "
            "status, requested_qty, filled_qty, position_id, position_type"
            ") VALUES ('2026-05-01T00:00:00+00:00', 'ALSO_NOT_OCC', 'buy', "
            "1, 0.0, NULL, 'credit_spread', 'spread entry', 0, 0, 0, 0, "
            "'mleg', 'filled', 1, 1, 'bad-occ-1', 'spread')"
        )
        conn.commit()
        rc = main(["--db", str(tmp_path / "trades.db"), "--apply"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "non-OCC symbol" in captured.out
