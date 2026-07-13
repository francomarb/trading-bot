"""
Unit tests for scripts/repair_stop_fill_pnl.py — the one-time repair
for single-leg exit rows whose realized P&L was booked against a
corrupted entry basis (reference-price fallback / position-blend
replay, 2026-07 audit).

The script shares its replay walk with production
(``reporting.logger.replay_single_leg_rows``), so these tests focus on
the diff/apply contract: what gets flagged, what gets skipped, what
gets written, and idempotency.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reporting.logger import TradeLogger, TradeRecord
from scripts.repair_stop_fill_pnl import apply_findings, main, scan


T1 = "2026-05-01T10:00:00+00:00"
T2 = "2026-05-10T10:00:00+00:00"
T3 = "2026-06-01T10:00:00+00:00"
T4 = "2026-06-09T10:00:00+00:00"


def _row(
    *,
    symbol: str,
    side: str,
    qty: float,
    fill: float | None,
    reference: float,
    timestamp: str,
    order_id: str,
    strategy: str = "fake_strategy",
    reason: str = "test",
    order_type: str = "market",
    entry_timestamp: str | None = None,
    initial_stop_loss: float | None = None,
    initial_risk_per_share: float | None = None,
    initial_risk_dollars: float | None = None,
    realized_pnl: float | None = None,
    r_multiple: float | None = None,
) -> TradeRecord:
    return TradeRecord(
        timestamp=timestamp,
        symbol=symbol,
        side=side,
        qty=qty,
        avg_fill_price=fill,
        order_id=order_id,
        strategy=strategy,
        reason=reason,
        stop_price=0.0,
        entry_reference_price=reference,
        modeled_slippage_bps=None,
        realized_slippage_bps=None,
        order_type=order_type,
        status="filled",
        requested_qty=qty,
        filled_qty=qty,
        initial_stop_loss=initial_stop_loss,
        initial_risk_per_share=initial_risk_per_share,
        initial_risk_dollars=initial_risk_dollars,
        realized_pnl=realized_pnl,
        r_multiple=r_multiple,
        entry_timestamp=entry_timestamp or timestamp,
        exit_timestamp=timestamp if side == "sell" else None,
        position_id=symbol,
        position_type="single_leg",
    )


@pytest.fixture
def corrupted_db(tmp_path: Path) -> str:
    """A trade log reproducing the audited corruption patterns.

    - QCOM: two adjacent positions with a backfilled exit — the second
      position's stop row carries a blended-basis P&L and the FIRST
      position's entry context (trade-320 pattern).
    - DK: entry fill was NULL at stop time (P&L booked against the
      reference price) and has since been backfilled — repairable
      (trade-285/288/290/301 pattern).
    - MSFT: entry fill STILL NULL — truth unknowable, must be skipped.
    - NVDA: 'exit signal' row with wrong P&L whose
      entry_reference_price column carries the EXIT's modeled
      benchmark and must not be rewritten.
    """
    path = str(tmp_path / "trades.db")
    tl = TradeLogger(path=path)

    # ── QCOM blend ──
    tl.log(_row(symbol="QCOM", side="buy", qty=14.0, fill=245.0,
                reference=245.0, timestamp=T1, order_id="q-b1",
                initial_stop_loss=195.0, initial_risk_per_share=50.0))
    tl.log(_row(symbol="QCOM", side="buy", qty=16.0, fill=228.0,
                reference=251.0, timestamp=T3, order_id="q-b2",
                initial_stop_loss=218.0, initial_risk_per_share=10.0))
    # Backfilled exit of position 1 (higher id, earlier execution) —
    # its own P&L was booked correctly at the time.
    tl.log(_row(symbol="QCOM", side="sell", qty=14.0, fill=195.5,
                reference=245.0, timestamp=T2, order_id="q-s1",
                reason="stop_triggered", order_type="stop",
                entry_timestamp=T1,
                realized_pnl=(195.5 - 245.0) * 14.0))
    # Position 2's stop — recorded against the blended basis
    # (245*14 + 228*16)/30 = 235.9333, with position 1's context.
    blended = (245.0 * 14.0 + 228.0 * 16.0) / 30.0
    tl.log(_row(symbol="QCOM", side="sell", qty=16.0, fill=195.0,
                reference=245.0, timestamp=T4, order_id="q-s2",
                reason="stop_triggered", order_type="stop",
                entry_timestamp=T1,
                initial_stop_loss=195.0, initial_risk_per_share=50.0,
                initial_risk_dollars=50.0 * 16.0,
                realized_pnl=(195.0 - blended) * 16.0,
                r_multiple=(195.0 - blended) / 50.0))

    # ── DK reference-basis booking, entry since backfilled ──
    tl.log(_row(symbol="DK", side="buy", qty=10.0, fill=100.5,
                reference=105.0, timestamp=T1, order_id="d-b1",
                initial_risk_per_share=5.0))
    tl.log(_row(symbol="DK", side="sell", qty=10.0, fill=95.0,
                reference=105.0, timestamp=T2, order_id="d-s1",
                reason="stop_triggered", order_type="stop",
                entry_timestamp=T1,
                initial_risk_per_share=5.0, initial_risk_dollars=50.0,
                realized_pnl=(95.0 - 105.0) * 10.0,
                r_multiple=-2.0))

    # ── MSFT: basis still unknowable ──
    tl.log(_row(symbol="MSFT", side="buy", qty=5.0, fill=None,
                reference=200.0, timestamp=T1, order_id="m-b1"))
    tl.log(_row(symbol="MSFT", side="sell", qty=5.0, fill=190.0,
                reference=200.0, timestamp=T2, order_id="m-s1",
                reason="stop_triggered", order_type="stop",
                entry_timestamp=T1,
                realized_pnl=(190.0 - 200.0) * 5.0))

    # ── NVDA: exit-signal row; entry_reference_price is the exit's
    # modeled benchmark (build_close_record semantics) ──
    tl.log(_row(symbol="NVDA", side="buy", qty=10.0, fill=100.0,
                reference=100.2, timestamp=T1, order_id="n-b1"))
    tl.log(_row(symbol="NVDA", side="sell", qty=10.0, fill=98.0,
                reference=98.5, timestamp=T2, order_id="n-s1",
                reason="exit signal", order_type="market",
                entry_timestamp=T1,
                realized_pnl=-60.0))

    tl.close()
    return path


def _fetch(path: str, order_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM trades WHERE order_id = ?", (order_id,)
        ).fetchone()
    finally:
        conn.close()


class TestRepairScan:
    def _scan(self, path: str):
        conn = sqlite3.connect(path)
        try:
            return scan(conn)
        finally:
            conn.close()

    def test_flags_blend_and_reference_corruption(self, corrupted_db):
        findings, _ = self._scan(corrupted_db)
        by_symbol = {f.symbol: f for f in findings}

        assert set(by_symbol) == {"QCOM", "DK", "NVDA"}

        qcom = by_symbol["QCOM"].changes
        assert qcom["realized_pnl"][1] == pytest.approx((195.0 - 228.0) * 16.0)
        # The blend also corrupted the entry context — repaired from
        # position 2's entry row.
        assert qcom["entry_timestamp"][1] == T3
        assert qcom["initial_stop_loss"][1] == pytest.approx(218.0)
        assert qcom["initial_risk_per_share"][1] == pytest.approx(10.0)
        assert qcom["initial_risk_dollars"][1] == pytest.approx(160.0)
        assert qcom["r_multiple"][1] == pytest.approx(
            ((195.0 - 228.0) * 16.0) / 160.0
        )
        # Stop rows store the ENTRY's reference price — repairable.
        assert qcom["entry_reference_price"][1] == pytest.approx(251.0)

        dk = by_symbol["DK"].changes
        assert dk["realized_pnl"][1] == pytest.approx((95.0 - 100.5) * 10.0)
        assert dk["r_multiple"][1] == pytest.approx(-55.0 / 50.0)

    def test_correctly_booked_backfilled_exit_untouched(self, corrupted_db):
        """The backfilled exit itself booked against position 1's real
        fill — the scan must not flag it."""
        findings, _ = self._scan(corrupted_db)
        backfilled_row_id = int(_fetch(corrupted_db, "q-s1")["id"])
        assert backfilled_row_id not in {f.row_id for f in findings}

    def test_skips_unknowable_reference_basis(self, corrupted_db):
        findings, skips = self._scan(corrupted_db)
        assert all(f.symbol != "MSFT" for f in findings)
        assert any(s.symbol == "MSFT" for s in skips)

    def test_exit_signal_entry_reference_price_untouched(self, corrupted_db):
        findings, _ = self._scan(corrupted_db)
        nvda = next(f for f in findings if f.symbol == "NVDA")
        assert nvda.changes["realized_pnl"][1] == pytest.approx(-20.0)
        assert "entry_reference_price" not in nvda.changes

    def test_clean_db_yields_nothing(self, tmp_path):
        path = str(tmp_path / "trades.db")
        tl = TradeLogger(path=path)
        tl.log(_row(symbol="AAPL", side="buy", qty=10.0, fill=100.0,
                    reference=100.0, timestamp=T1, order_id="b1"))
        tl.log(_row(symbol="AAPL", side="sell", qty=10.0, fill=105.0,
                    reference=100.0, timestamp=T2, order_id="s1",
                    reason="stop_triggered", order_type="stop",
                    entry_timestamp=T1,
                    realized_pnl=50.0))
        tl.close()
        findings, skips = self._scan(path)
        assert findings == []
        assert skips == []


class TestRepairApply:
    def test_dry_run_writes_nothing(self, corrupted_db, capsys):
        before = _fetch(corrupted_db, "q-s2")
        exit_code = main(["--db", corrupted_db])
        assert exit_code == 1  # findings exist, nothing written
        after = _fetch(corrupted_db, "q-s2")
        assert dict(before) == dict(after)
        assert "DRY RUN" in capsys.readouterr().out

    def test_apply_repairs_and_is_idempotent(self, corrupted_db, capsys):
        exit_code = main(["--db", corrupted_db, "--apply"])
        assert exit_code == 0

        qcom = _fetch(corrupted_db, "q-s2")
        assert qcom["realized_pnl"] == pytest.approx((195.0 - 228.0) * 16.0)
        assert qcom["entry_timestamp"] == T3
        assert qcom["entry_reference_price"] == pytest.approx(251.0)

        dk = _fetch(corrupted_db, "d-s1")
        assert dk["realized_pnl"] == pytest.approx(-55.0)

        nvda = _fetch(corrupted_db, "n-s1")
        assert nvda["realized_pnl"] == pytest.approx(-20.0)
        assert nvda["entry_reference_price"] == pytest.approx(98.5)

        # Skipped row untouched.
        msft = _fetch(corrupted_db, "m-s1")
        assert msft["realized_pnl"] == pytest.approx(-50.0)

        # A backup was written next to the DB.
        backups = list(Path(corrupted_db).parent.glob("*.pre_repair_*.bak"))
        assert len(backups) == 1

        # Second run: nothing left to repair (only the MSFT skip).
        capsys.readouterr()
        conn = sqlite3.connect(corrupted_db)
        try:
            findings, skips = scan(conn)
        finally:
            conn.close()
        assert findings == []
        assert [s.symbol for s in skips] == ["MSFT"]

    def test_apply_transaction_rolls_back_on_error(self, corrupted_db):
        conn = sqlite3.connect(corrupted_db)
        try:
            findings, _ = scan(conn)
            # Poison one finding with a column that doesn't exist.
            findings[0].changes["nonexistent_column"] = (None, 1.0)
            with pytest.raises(sqlite3.OperationalError):
                apply_findings(conn, findings)
        finally:
            conn.close()
        # Every row is unchanged — including the other findings.
        conn = sqlite3.connect(corrupted_db)
        try:
            remaining, _ = scan(conn)
        finally:
            conn.close()
        assert len(remaining) == len(findings)

    def test_missing_db_errors(self, tmp_path, capsys):
        exit_code = main(["--db", str(tmp_path / "missing.db")])
        assert exit_code == 2
        assert "not found" in capsys.readouterr().out
