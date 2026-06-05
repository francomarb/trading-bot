"""
Trade logging and structured JSON sink (Phase 9 → migrated to SQLite in Step 5).

Two responsibilities:

1. **Structured JSON log sink** — adds a loguru sink that writes JSONL
   alongside the human-readable console output. Every log line becomes a
   machine-parseable record for downstream monitoring.

2. **TradeLogger** — inserts a row to the SQLite trades table after every
   fill. The database is the append-only audit trail of every order the bot
   placed, with slippage data baked in.

Design principles:
  - The schema is fixed at table-creation time; reads happen in
    `reporting/pnl.py`, `backtest/reconcile.py`, and in operator tools —
    never in the hot path.
  - All writes are append-only (INSERT). We never UPDATE or DELETE rows.
  - `_ensure_db` is called lazily on first write, not at import time, so
    tests that never write don't need the filesystem.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal


SlippageBenchmarkKind = Literal[
    "arrival_midpoint",
    "decision_price",
    "fallback_latest_close",
    "active_stop_price",
    "combo_limit",
    "limit_price",
    "unavailable",
]

SlippageMeasurementQuality = Literal[
    "primary",
    "fallback",
    "recovered",
    "unavailable",
]

from loguru import logger

from config import settings
from utils.option_symbols import owner_key_for

_OCC_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")


def _contract_multiplier(symbol: str) -> int:
    """Return the contract multiplier for equities vs OCC option symbols."""
    return 100 if _OCC_OPTION_SYMBOL.match(symbol or "") else 1


def single_leg_realized_slippage_bps(
    *,
    side: str,
    reference_price: float | None,
    actual_fill_price: float | None,
) -> float:
    """
    Signed single-leg slippage against the intended reference price.

    Positive is adverse execution; negative is price improvement. For buys,
    paying more than the reference is adverse. For sells, receiving less than
    the reference is adverse.
    """
    if reference_price is None or actual_fill_price is None:
        return 0.0
    reference = float(reference_price)
    actual = float(actual_fill_price)
    if reference <= 0:
        return 0.0
    normalized_side = side.lower()
    if normalized_side == "buy":
        bps = (actual - reference) / reference * 10_000
    elif normalized_side == "sell":
        bps = (reference - actual) / reference * 10_000
    else:
        bps = abs(actual - reference) / reference * 10_000
    return round(bps, 2)


def mleg_realized_slippage_bps(
    *,
    opening: bool,
    submitted_limit_price: float | None,
    actual_net_price: float | None,
) -> float:
    """
    Realized combo slippage vs the submitted MLEG limit.

    Positive is adverse execution; negative is price improvement. Opening
    credit orders prefer a larger credit, while closing debit orders prefer a
    smaller debit. Prices may arrive signed from Alpaca, so compare absolute
    net prices.
    """
    if submitted_limit_price is None or actual_net_price is None:
        return 0.0
    benchmark = abs(float(submitted_limit_price))
    actual = abs(float(actual_net_price))
    if benchmark <= 0:
        return 0.0
    if opening:
        bps = (benchmark - actual) / benchmark * 10_000
    else:
        bps = (actual - benchmark) / benchmark * 10_000
    return round(bps, 2)


# ── Structured JSON sink ────────────────────────────────────────────────────


def install_json_sink(path: str | None = None) -> int:
    """
    Add a JSONL loguru sink. Returns the sink ID so callers can remove it
    in tests if needed.
    """
    path = path or settings.JSON_LOG_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sink_id = logger.add(
        path,
        serialize=True,
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
    )
    return sink_id


# ── Schema ──────────────────────────────────────────────────────────────────

TRADE_COLUMNS = [
    "timestamp",
    "symbol",
    "side",
    "qty",
    "avg_fill_price",
    "order_id",
    "strategy",
    "reason",
    "stop_price",
    "entry_reference_price",
    "modeled_slippage_bps",
    "realized_slippage_bps",
    "order_type",
    "status",
    "requested_qty",
    "filled_qty",
    "initial_stop_loss",
    "initial_risk_per_share",
    "initial_risk_dollars",
    "realized_pnl",
    "r_multiple",
    "entry_timestamp",
    "exit_timestamp",
    "position_id",
    "position_type",
    "position_uid",
    "slippage_benchmark_price",
    "slippage_benchmark_kind",
    "slippage_benchmark_timestamp",
    "slippage_measurement_quality",
    "slippage_signed_bps",
    "slippage_adverse_bps",
    "stop_trigger_price",
]

# Keep the old name as an alias for backwards compatibility with tests
# that import TRADE_CSV_COLUMNS.
TRADE_CSV_COLUMNS = TRADE_COLUMNS

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,
    symbol                TEXT    NOT NULL,
    side                  TEXT    NOT NULL,
    qty                   REAL    NOT NULL,
    avg_fill_price        REAL,
    order_id              TEXT,
    strategy              TEXT    NOT NULL,
    reason                TEXT    NOT NULL,
    stop_price            REAL,
    entry_reference_price REAL,
    modeled_slippage_bps  REAL,
    realized_slippage_bps REAL,
    order_type            TEXT,
    status                TEXT    NOT NULL,
    requested_qty         REAL,
    filled_qty            REAL,
    initial_stop_loss     REAL,
    initial_risk_per_share REAL,
    initial_risk_dollars  REAL,
    realized_pnl          REAL,
    r_multiple            REAL,
    entry_timestamp       TEXT,
    exit_timestamp        TEXT,
    position_id           TEXT,
    position_type         TEXT,
    position_uid          TEXT,
    slippage_benchmark_price     REAL,
    slippage_benchmark_kind      TEXT,
    slippage_benchmark_timestamp TEXT,
    slippage_measurement_quality TEXT,
    slippage_signed_bps          REAL,
    slippage_adverse_bps         REAL,
    stop_trigger_price           REAL
);
"""

_MIGRATION_COLUMNS = {
    "initial_stop_loss": "REAL",
    "initial_risk_per_share": "REAL",
    "initial_risk_dollars": "REAL",
    "realized_pnl": "REAL",
    "r_multiple": "REAL",
    "entry_timestamp": "TEXT",
    "exit_timestamp": "TEXT",
    "position_id": "TEXT",
    "position_type": "TEXT",
    # Operator Controls Phase A — immutable per-lifecycle ID. Added as a
    # nullable column; pre-existing rows are NULL (no consumer in
    # Phase A relies on backfilling them). New trades passed
    # `position_uid=...` populate it. The other three columns from
    # proposal §8 (parent_position_uid, operator_command_uid,
    # client_order_id) are deferred to Phase C bundle per the
    # implementation plan — they will be added when their first
    # consumer ships.
    "position_uid": "TEXT",
    # Slippage unification — see docs/slippage_unification_design.md and
    # docs/slippage_unification_tracker.md. All nullable; pre-existing
    # rows remain NULL. Writers populate these per the per-codepath
    # contract over Phase 1; legacy columns continue to dual-write.
    "slippage_benchmark_price": "REAL",
    "slippage_benchmark_kind": "TEXT",
    "slippage_benchmark_timestamp": "TEXT",
    "slippage_measurement_quality": "TEXT",
    "slippage_signed_bps": "REAL",
    "slippage_adverse_bps": "REAL",
    "stop_trigger_price": "REAL",
}

# Index on position_id for fast spread leg grouping. Idempotent.
_POSITION_ID_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_trades_position_id "
    "ON trades(position_id)"
)

# Operator Controls Phase A — index on position_uid for fast
# show-position lookups in the operator CLI.
_POSITION_UID_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_trades_position_uid "
    "ON trades(position_uid)"
)

# Backfill: existing rows pre-PR-11.27 are all single-leg, with
# position_id = symbol. Run once per database after the ALTERs.
# One-shot backfill for rows that pre-date PR 11.27. The OWNER_KEY() SQLite
# UDF (registered in _ensure_db, wired to utils.option_symbols.owner_key_for)
# collapses OCC option symbols to their underlying ticker — so legacy equity
# rows store position_id = symbol, and legacy option rows store
# position_id = underlying. This matches what engine.positions builds for
# new positions, keeping the engine lookup key and the DB key consistent.
# Guarded by `WHERE position_id IS NULL` so explicit writes (future spreads)
# are never overwritten on subsequent startups.
_BACKFILL_SQL = (
    "UPDATE trades "
    "SET position_id = OWNER_KEY(symbol), position_type = 'single_leg' "
    "WHERE position_id IS NULL"
)


# ── Trade record ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TradeRecord:
    """One row in the trades table — built from a RiskDecision + OrderResult."""

    timestamp: str
    symbol: str
    side: str
    qty: float
    avg_fill_price: float | None
    order_id: str | None
    strategy: str
    reason: str
    stop_price: float
    entry_reference_price: float
    modeled_slippage_bps: float | None
    realized_slippage_bps: float | None
    order_type: str
    status: str
    requested_qty: float
    filled_qty: float
    initial_stop_loss: float | None = None
    initial_risk_per_share: float | None = None
    initial_risk_dollars: float | None = None
    realized_pnl: float | None = None
    r_multiple: float | None = None
    entry_timestamp: str | None = None
    exit_timestamp: str | None = None
    position_id: str | None = None
    position_type: str | None = None
    # Operator Controls Phase A — immutable per-lifecycle ID. Optional
    # because exit/close records built without an entry-side lifecycle
    # reference can still log; the broker entry path passes it through
    # from `engine.lifecycle.new_position_uid()`.
    position_uid: str | None = None
    # Slippage unification — see docs/slippage_unification_design.md.
    # Populated per-codepath by writers over Phase 1. Legacy columns
    # (modeled_slippage_bps / realized_slippage_bps) continue to
    # dual-write until consumers migrate in Phase 2.
    slippage_benchmark_price: float | None = None
    slippage_benchmark_kind: SlippageBenchmarkKind | None = None
    slippage_benchmark_timestamp: str | None = None
    slippage_measurement_quality: SlippageMeasurementQuality | None = None
    slippage_signed_bps: float | None = None
    slippage_adverse_bps: float | None = None
    stop_trigger_price: float | None = None

    def as_dict(self) -> dict:
        """Column-ordered dict (same interface as before migration)."""
        return {col: getattr(self, col) for col in TRADE_COLUMNS}


# ── TradeLogger ─────────────────────────────────────────────────────────────


class TradeLogger:
    """
    Appends trade records to a SQLite database.

    Usage (from engine):
        trade_logger = TradeLogger()
        record = trade_logger.build_record(decision, result)
        trade_logger.log(record)
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or settings.TRADE_LOG_DB
        self._conn: sqlite3.Connection | None = None

    def _ensure_db(self) -> sqlite3.Connection:
        """Create the database and table if they don't exist yet."""
        if self._conn is not None:
            return self._conn
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        # Register OWNER_KEY() as a SQLite UDF so the backfill SQL can
        # normalize OCC option symbols to their underlying. Keeps the
        # stored position_id consistent with engine.positions.owner_key_for().
        self._conn.create_function("OWNER_KEY", 1, owner_key_for, deterministic=True)
        self._conn.execute(_CREATE_TABLE_SQL)
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        for column, col_type in _MIGRATION_COLUMNS.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {column} {col_type}"
                )
        # 11.27: backfill position_id/position_type on pre-existing rows, then
        # ensure the lookup index exists. Both statements are idempotent.
        self._conn.execute(_BACKFILL_SQL)
        self._conn.execute(_POSITION_ID_INDEX_SQL)
        self._conn.execute(_POSITION_UID_INDEX_SQL)
        # 11.10c: signal-lifecycle counter table for the Strategy Health
        # Monitor. Local import avoids a circular dependency
        # (strategies.health.* can import reporting.logger types).
        from strategies.health.lifecycle import _CREATE_LIFECYCLE_TABLE_SQL
        self._conn.execute(_CREATE_LIFECYCLE_TABLE_SQL)
        # Operator Controls Phase A — position_lifecycle and
        # position_lifecycle_legs. Created by the same migration path
        # so all schema lives behind one connection bootstrap. Local
        # import keeps `engine.lifecycle` standalone-importable; this
        # is the only place that pulls the DDL constants.
        from engine.lifecycle import (
            _CREATE_POSITION_LIFECYCLE_SQL,
            _CREATE_POSITION_LIFECYCLE_LEGS_SQL,
            _CREATE_POSITION_LIFECYCLE_INDEXES_SQL,
        )
        self._conn.execute(_CREATE_POSITION_LIFECYCLE_SQL)
        self._conn.execute(_CREATE_POSITION_LIFECYCLE_LEGS_SQL)
        for index_sql in _CREATE_POSITION_LIFECYCLE_INDEXES_SQL:
            self._conn.execute(index_sql)
        from engine.option_trailing import (
            _CREATE_OPTION_TRAILING_STOPS_SQL,
            _OPTION_TRAILING_STOPS_INDEXES_SQL,
        )
        self._conn.execute(_CREATE_OPTION_TRAILING_STOPS_SQL)
        for index_sql in _OPTION_TRAILING_STOPS_INDEXES_SQL:
            self._conn.execute(index_sql)
        # Operator Controls Phase A PR-2 — operator_commands queue.
        # Same migration scaffolding; local import keeps
        # engine.operator_queue independently importable.
        from engine.operator_queue import (
            _CREATE_OPERATOR_COMMANDS_SQL,
            _CREATE_OPERATOR_COMMANDS_INDEXES_SQL,
        )
        self._conn.execute(_CREATE_OPERATOR_COMMANDS_SQL)
        for index_sql in _CREATE_OPERATOR_COMMANDS_INDEXES_SQL:
            self._conn.execute(index_sql)
        self._conn.commit()
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def build_record(
        self,
        decision,  # RiskDecision
        result,    # OrderResult
        *,
        modeled_price: float | None = None,
        position_uid: str | None = None,
        record_slippage: bool = True,
        timestamp_override: datetime | None = None,
    ) -> TradeRecord:
        """
        Build a TradeRecord from a RiskDecision + OrderResult.

        `modeled_price` is the arrival-price benchmark for execution-quality
        slippage measurement — the NBBO midpoint at order submission. The
        engine fetches this via ``broker.get_latest_quote_midpoint(symbol)``
        immediately before placing the order. Industry TCA practice
        (Implementation Shortfall framework, Talos/QuestDB references)
        treats arrival price as the canonical pre-trade benchmark, distinct
        from the decision-time price; the latter remains available in
        ``decision.entry_reference_price`` for downstream Implementation-
        Shortfall analysis.

        `record_slippage=False` writes NULL on both slippage columns. Used
        by the recovered-entry-context path in ``engine.trader`` where the
        bot reconstructs an open position whose original arrival quote is
        unrecoverable — recording an honest NULL is correct rather than
        synthesizing a misleading number from the current bar close (the
        Issue A failure mode that produced QCOM's 1205 bps phantom
        slippage on the May 11 recovery row).

        `timestamp_override` exists for recovery / reconciliation writes.
        When broker history gives us the original execution time
        (typically Alpaca ``filled_at``), we should preserve that broker
        timestamp instead of stamping "time of reconstruction", which would
        make recovered rows look like after-the-fact fills.
        """
        # Arrival-price slippage is only a meaningful execution-quality
        # signal for MARKET orders. A resting LIMIT fill at $95 against a
        # $100 limit is excellent execution — the operator got the price
        # they asked for or better. The signed `realized_slippage_bps`
        # would store −500 bps (price improvement); the L2 alarm now
        # uses adverse-only semantics (max(0, realized − modeled)) so
        # price improvement no longer trips it directly, but writing a
        # large negative value into the row still pollutes downstream
        # consumers and the operator-facing column. RSI mean-reversion
        # entries are all LIMIT (strategies/rsi_reversion.py), so the
        # cleanest answer for LIMIT is to write NULL on both slippage
        # columns: the IS NOT NULL filter on the L2 query naturally
        # excludes them; LIMIT execution quality lives in a separate
        # (out-of-PR-scope) limit-fill-vs-limit-price metric.
        is_market_order = decision.order_type.value == "market"
        if record_slippage and is_market_order:
            modeled_bps: float | None = 0.0
            realized_bps: float | None = 0.0
            ref_price = modeled_price or decision.entry_reference_price
            if modeled_price is not None:
                modeled_bps = settings.SLIPPAGE_MODEL_MARKET_BPS
            if (
                result.avg_fill_price is not None
                and ref_price > 0
            ):
                realized_bps = single_leg_realized_slippage_bps(
                    side=decision.side.value,
                    reference_price=ref_price,
                    actual_fill_price=result.avg_fill_price,
                )
        else:
            modeled_bps = None
            realized_bps = None
        initial_stop_loss = float(decision.stop_price)
        initial_risk_per_share = max(
            0.0,
            float(decision.entry_reference_price) - float(decision.stop_price),
        )
        multiplier = _contract_multiplier(decision.symbol)
        initial_risk_dollars = (
            initial_risk_per_share
            * float(result.filled_qty or result.requested_qty or 0)
            * multiplier
        )
        timestamp_dt = timestamp_override or datetime.now(timezone.utc)
        now_iso = timestamp_dt.astimezone(timezone.utc).isoformat()

        return TradeRecord(
            timestamp=now_iso,
            symbol=decision.symbol,
            side=decision.side.value,
            qty=result.filled_qty,
            avg_fill_price=result.avg_fill_price,
            order_id=result.order_id,
            strategy=decision.strategy_name,
            reason=decision.reason,
            stop_price=decision.stop_price,
            entry_reference_price=decision.entry_reference_price,
            modeled_slippage_bps=modeled_bps,
            realized_slippage_bps=(
                round(realized_bps, 2) if realized_bps is not None else None
            ),
            order_type=decision.order_type.value,
            status=result.status.value,
            requested_qty=result.requested_qty,
            filled_qty=result.filled_qty,
            initial_stop_loss=initial_stop_loss,
            initial_risk_per_share=initial_risk_per_share,
            initial_risk_dollars=initial_risk_dollars,
            realized_pnl=None,
            r_multiple=None,
            entry_timestamp=now_iso,
            exit_timestamp=None,
            position_id=owner_key_for(decision.symbol),
            position_type="single_leg",
            position_uid=position_uid,
        )

    def build_close_record(
        self,
        result,    # OrderResult from close_position
        *,
        strategy_name: str,
        modeled_price: float,
    ) -> TradeRecord:
        """
        Build a TradeRecord for a position close (exit). Closes don't have
        a RiskDecision — they come from the strategy exit signal directly.
        """
        realized_bps = 0.0
        modeled_bps = settings.SLIPPAGE_MODEL_MARKET_BPS if modeled_price > 0 else 0.0
        context = self._read_latest_open_entry_context(
            symbol=result.symbol,
            strategy=strategy_name,
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        if result.avg_fill_price is not None and modeled_price > 0:
            realized_bps = single_leg_realized_slippage_bps(
                side="sell",
                reference_price=modeled_price,
                actual_fill_price=result.avg_fill_price,
            )
        realized_pnl = None
        r_multiple = None
        initial_stop_loss = None
        initial_risk_per_share = None
        initial_risk_dollars = None
        entry_timestamp = None
        multiplier = _contract_multiplier(result.symbol)
        if context is not None:
            initial_stop_loss = context["initial_stop_loss"]
            initial_risk_per_share = context["initial_risk_per_share"]
            entry_timestamp = context["entry_timestamp"]
            if initial_risk_per_share is not None:
                initial_risk_dollars = (
                    float(initial_risk_per_share)
                    * float(result.filled_qty or 0)
                    * multiplier
                )
            if (
                result.avg_fill_price is not None
                and context["entry_reference_price"] is not None
            ):
                realized_pnl = (
                    (float(result.avg_fill_price) - float(context["entry_reference_price"]))
                    * float(result.filled_qty or 0)
                    * multiplier
                )
                if initial_risk_dollars and initial_risk_dollars > 0:
                    r_multiple = realized_pnl / initial_risk_dollars

        return TradeRecord(
            timestamp=now_iso,
            symbol=result.symbol,
            side="sell",
            qty=result.filled_qty,
            avg_fill_price=result.avg_fill_price,
            order_id=result.order_id,
            strategy=strategy_name,
            reason="exit signal",
            stop_price=0.0,
            entry_reference_price=modeled_price,
            modeled_slippage_bps=modeled_bps,
            realized_slippage_bps=round(realized_bps, 2),
            order_type="market",
            status=result.status.value,
            requested_qty=result.requested_qty,
            filled_qty=result.filled_qty,
            initial_stop_loss=initial_stop_loss,
            initial_risk_per_share=initial_risk_per_share,
            initial_risk_dollars=initial_risk_dollars,
            realized_pnl=realized_pnl,
            r_multiple=r_multiple,
            entry_timestamp=entry_timestamp,
            exit_timestamp=now_iso,
            position_id=owner_key_for(result.symbol),
            position_type="single_leg",
        )

    def log(self, record: TradeRecord) -> None:
        """Insert one trade record into the database."""
        conn = self._ensure_db()
        d = record.as_dict()
        columns = ", ".join(d.keys())
        placeholders = ", ".join(["?"] * len(d))
        conn.execute(
            f"INSERT INTO trades ({columns}) VALUES ({placeholders})",
            list(d.values()),
        )
        conn.commit()
        logger.info(
            f"trade logged: {record.side} {record.qty} {record.symbol} "
            f"@ ${record.avg_fill_price} [{record.strategy}] "
            f"slip={record.realized_slippage_bps}bps"
        )

    def read_all(self) -> list[dict]:
        """Read all trade records from the database. For reporting, not hot path."""
        if not os.path.exists(self._path):
            return []
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT {', '.join(TRADE_COLUMNS)} FROM trades ORDER BY id"
        )
        return [dict(row) for row in cursor.fetchall()]

    def read_trades_in_range(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Read trades whose timestamp falls within [start_date, end_date] (date prefix match)."""
        if not os.path.exists(self._path):
            return []
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT {', '.join(TRADE_COLUMNS)} FROM trades "
            "WHERE substr(timestamp, 1, 10) >= ? AND substr(timestamp, 1, 10) <= ? "
            "ORDER BY id",
            (start_date, end_date),
        )
        return [dict(row) for row in cursor.fetchall()]

    def read_recent(self, last_n: int) -> list[dict]:
        """Read the last N trade records."""
        if not os.path.exists(self._path):
            return []
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT {', '.join(TRADE_COLUMNS)} FROM trades ORDER BY id DESC LIMIT ?",
            (last_n,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        rows.reverse()  # Return in chronological order
        return rows

    def log_external_close(self, *, symbol: str, strategy: str, reason: str) -> None:
        """
        Write a synthetic sell record when a position disappears externally
        (stop-out, manual liquidation, margin call, etc.) without the bot
        placing the closing order.

        Fill price is None because we don't know the exact execution price.
        The reason field carries the detection context.
        """
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            side="sell",
            qty=0,
            avg_fill_price=None,
            order_id=None,
            strategy=strategy,
            reason=reason,
            stop_price=0.0,
            entry_reference_price=0.0,
            modeled_slippage_bps=0.0,
            realized_slippage_bps=0.0,
            order_type="unknown",
            status="filled",
            requested_qty=0,
            filled_qty=0,
            initial_stop_loss=None,
            initial_risk_per_share=None,
            initial_risk_dollars=None,
            realized_pnl=None,
            r_multiple=None,
            entry_timestamp=None,
            exit_timestamp=datetime.now(timezone.utc).isoformat(),
            position_id=owner_key_for(symbol),
            position_type="single_leg",
        )
        self.log(record)

    def log_spread_fill(
        self,
        *,
        position_id: str,
        strategy: str,
        short_occ: str,
        long_occ: str,
        qty: float,
        net_price: float,
        order_id: str | None = None,
        opening: bool,
        realized_pnl: float | None = None,
        reason: str = "",
        submitted_limit_price: float | None = None,
        realized_slippage_bps: float | None = None,
        initial_risk_dollars: float | None = None,
    ) -> None:
        """
        Write trade-log rows for a multi-leg (MLEG) credit-spread fill (11.29).

        **One row per leg**, both keyed by the same ``position_id`` with
        ``position_type='spread'``. Recording both OCC legs is what lets
        startup reconciliation rebuild the full spread Position after a
        restart — a single short-leg row alone cannot identify the long leg.

        Leg sides reflect the actual trade: opening a bull put credit spread
        sells the short (higher-strike) put and buys the long (lower-strike)
        put; closing reverses both. The net economics (``net_price`` — credit
        received on open, debit paid on close, both positive) go on the
        short-leg row; the long-leg row carries ``avg_fill_price=0.0``.
        When ``submitted_limit_price`` is provided, the short-leg
        ``entry_reference_price`` stores the absolute submitted combo limit
        used for slippage attribution rather than the actual net fill price.

        ``realized_pnl`` is the closed spread's net P&L
        (``(net_credit − net_debit) × qty × 100``) — provided only on a close.
        It is written to the short-leg row so
        ``read_strategy_realized_pnl_summary`` (which counts spread rows)
        rolls credit-spread P&L into the sleeve HWM / drawdown gate.

        ``initial_risk_dollars`` is the spread's max-loss basis at open
        (``(width − net_credit) × 100 × qty``). The caller computes it from
        the ``SpreadExecutionPlan`` / ``OpenSpread`` in scope. On the open
        path it is written to the short-leg row so the close path can read
        it back. On the close path, it is combined with ``realized_pnl`` to
        produce the R-multiple: ``r_multiple = realized_pnl /
        initial_risk_dollars``. If the caller omits it on the close path
        (e.g. external-close detection without an in-memory ``OpenSpread``),
        the close path falls back to the most recent open row for
        ``position_id``. When neither source yields a finite positive
        basis, ``r_multiple`` stays NULL — the Strategy Health design
        (§5.1, edge.py:149-156) tolerates this and falls back to dollar
        metrics.

        ``position_type='spread'`` rows are deliberately excluded from
        ``read_all_open_owners`` / ``read_owner_for_symbol`` so the engine's
        single-leg ownership restore never mistakes a spread leg for a
        standalone position — see ``read_open_spread_positions`` for the
        spread-aware restore path.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        default_reason = "spread entry" if opening else "spread exit"
        # Short leg: sold to open / bought to close. Long leg: the reverse.
        short_side = "sell" if opening else "buy"
        long_side = "buy" if opening else "sell"
        short_ref_price = (
            abs(float(submitted_limit_price))
            if submitted_limit_price is not None
            else net_price
        )
        short_slippage_bps = (
            round(float(realized_slippage_bps), 2)
            if realized_slippage_bps is not None
            else mleg_realized_slippage_bps(
                opening=opening,
                submitted_limit_price=submitted_limit_price,
                actual_net_price=net_price,
            )
        )

        # On a close, fall back to the open row's stored basis when the
        # caller didn't supply one (external-close path can lose `released`).
        if not opening and initial_risk_dollars is None:
            initial_risk_dollars = self._read_latest_open_spread_risk_basis(
                position_id=position_id,
            )

        basis = (
            float(initial_risk_dollars)
            if initial_risk_dollars is not None
            and math.isfinite(float(initial_risk_dollars))
            and float(initial_risk_dollars) > 0
            else None
        )
        r_multiple: float | None = None
        if not opening and basis is not None and realized_pnl is not None:
            pnl_f = float(realized_pnl)
            if math.isfinite(pnl_f):
                r_multiple = pnl_f / basis

        def _leg_record(
            symbol: str,
            side: str,
            price: float,
            pnl: float | None,
            *,
            entry_reference_price: float,
            slippage_bps: float,
            risk_dollars: float | None,
            r_mult: float | None,
        ) -> TradeRecord:
            return TradeRecord(
                timestamp=now_iso,
                symbol=symbol,
                side=side,
                qty=qty,
                avg_fill_price=price,
                order_id=order_id,
                strategy=strategy,
                reason=reason or default_reason,
                stop_price=0.0,
                entry_reference_price=entry_reference_price,
                modeled_slippage_bps=0.0,
                realized_slippage_bps=slippage_bps,
                order_type="mleg",
                status="filled",
                requested_qty=qty,
                filled_qty=qty,
                initial_stop_loss=None,
                initial_risk_per_share=None,
                initial_risk_dollars=risk_dollars,
                realized_pnl=pnl,
                r_multiple=r_mult,
                entry_timestamp=now_iso if opening else None,
                exit_timestamp=None if opening else now_iso,
                position_id=position_id,
                position_type="spread",
            )

        # realized_pnl, initial_risk_dollars, and r_multiple ride the short-leg
        # row alongside the net economics; the long-leg row stays at 0.0 / None.
        self.log(_leg_record(
            short_occ,
            short_side,
            net_price,
            realized_pnl,
            entry_reference_price=short_ref_price,
            slippage_bps=short_slippage_bps,
            risk_dollars=basis,
            r_mult=r_multiple,
        ))
        self.log(_leg_record(
            long_occ,
            long_side,
            0.0,
            None,
            entry_reference_price=0.0,
            slippage_bps=0.0,
            risk_dollars=None,
            r_mult=None,
        ))
        logger.info(
            f"spread {'entry' if opening else 'exit'} logged: {short_occ}/{long_occ} "
            f"qty={qty} net=${net_price:.2f}/sh [{strategy}] position_id={position_id[:8]}"
        )

    def _read_latest_open_spread_risk_basis(
        self, *, position_id: str
    ) -> float | None:
        """Return the most recent open spread row's initial_risk_dollars
        for ``position_id``, or None if no usable basis exists.

        Used by the close path in ``log_spread_fill`` when the caller did
        not pass an explicit basis (external-close detection has no
        in-memory ``OpenSpread``). Matches the spread leg open by
        ``position_type='spread'``, ``side='sell'`` (the short leg, which
        carries the basis), ``realized_pnl IS NULL`` (open rows, not prior
        closes), and ``initial_risk_dollars IS NOT NULL``.
        """
        if not os.path.exists(self._path):
            return None
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return None
        cursor = conn.execute(
            "SELECT initial_risk_dollars FROM trades "
            "WHERE position_id = ? "
            "AND position_type = 'spread' "
            "AND side = 'sell' "
            "AND realized_pnl IS NULL "
            "AND initial_risk_dollars IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (position_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        try:
            value = float(row[0])
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    def log_stop_fill(
        self,
        *,
        symbol: str,
        strategy: str,
        qty: float,
        avg_fill_price: float,
        stop_price: float | None = None,
        measurement_quality: SlippageMeasurementQuality = "primary",
        order_id: str | None = None,
        timestamp_override: datetime | None = None,
    ) -> None:
        """
        Write a confirmed stop-fill record when the WebSocket stream delivers
        an exact bracket stop execution (price and qty known).

        Distinct from log_external_close, which is the fallback for positions
        that disappear without a confirmed fill event.

        `stop_price` is the broker order's actual stop trigger at fill time
        (from ``update.order.stop_price`` on the WebSocket path, or
        ``ClosedOrderInfo.stop_price`` on the recovery path). It is the
        authoritative slippage benchmark for stop fills — see
        docs/slippage_unification_design.md §Stop Lifecycle. When None or
        non-positive, the new slippage columns are written as
        ``unavailable`` and signed/adverse values stay NULL; legacy
        ``realized_slippage_bps`` / ``modeled_slippage_bps`` retain their
        old behavior (fall back to ``initial_stop_loss``) for Phase 1
        consumer compatibility.

        `measurement_quality` lets the recovery path tag rows as
        ``recovered`` rather than ``primary``. Forced to ``unavailable``
        when no broker stop_price is provided.

        `timestamp_override` is used by recovery/reconciliation paths that
        discover a missed stop fill later from broker history. When broker
        truth includes the original execution time, prefer that timestamp
        over the later time we noticed and reconstructed the event.
        """
        context = self._read_latest_open_entry_context(
            symbol=symbol,
            strategy=strategy,
        )
        timestamp_dt = timestamp_override or datetime.now(timezone.utc)
        now_iso = timestamp_dt.astimezone(timezone.utc).isoformat()
        initial_stop_loss = None
        initial_risk_per_share = None
        initial_risk_dollars = None
        realized_pnl = None
        r_multiple = None
        entry_timestamp = None
        entry_reference_price = 0.0
        multiplier = _contract_multiplier(symbol)
        if context is not None:
            initial_stop_loss = context["initial_stop_loss"]
            initial_risk_per_share = context["initial_risk_per_share"]
            entry_timestamp = context["entry_timestamp"]
            entry_reference_price = float(context["entry_reference_price"] or 0.0)
            if initial_risk_per_share is not None:
                initial_risk_dollars = float(initial_risk_per_share) * qty * multiplier
            if entry_reference_price > 0:
                realized_pnl = (avg_fill_price - entry_reference_price) * qty * multiplier
                if initial_risk_dollars and initial_risk_dollars > 0:
                    r_multiple = realized_pnl / initial_risk_dollars

        # ── Slippage unification (Phase 1) ──
        # Broker stop_price is the authoritative benchmark. When absent,
        # the new columns honestly report `unavailable`; legacy columns
        # retain their pre-unification fallback for Phase 1 compat.
        broker_stop_available = stop_price is not None and float(stop_price) > 0
        new_slippage_benchmark_price: float | None = None
        new_slippage_benchmark_kind: SlippageBenchmarkKind = "unavailable"
        new_slippage_benchmark_timestamp: str | None = None
        new_slippage_quality: SlippageMeasurementQuality = "unavailable"
        new_slippage_signed_bps: float | None = None
        new_slippage_adverse_bps: float | None = None
        new_stop_trigger_price: float | None = None

        if broker_stop_available:
            benchmark = float(stop_price)
            signed = single_leg_realized_slippage_bps(
                side="sell",
                reference_price=benchmark,
                actual_fill_price=avg_fill_price,
            )
            new_slippage_benchmark_price = benchmark
            new_slippage_benchmark_kind = "active_stop_price"
            new_slippage_benchmark_timestamp = now_iso
            new_slippage_quality = measurement_quality
            new_slippage_signed_bps = signed
            new_slippage_adverse_bps = max(0.0, signed)
            new_stop_trigger_price = benchmark

        # ── Legacy dual-write (Phase 1 compat) ──
        # Prefer broker stop_price when available so legacy consumers
        # immediately benefit from the more accurate benchmark; otherwise
        # fall back to initial_stop_loss exactly as before this change.
        legacy_reference = (
            float(stop_price)
            if broker_stop_available
            else float(initial_stop_loss or 0.0)
        )
        realized_slippage_bps = 0.0
        modeled_slippage_bps = 0.0
        if legacy_reference > 0:
            modeled_slippage_bps = settings.SLIPPAGE_MODEL_MARKET_BPS
            realized_slippage_bps = single_leg_realized_slippage_bps(
                side="sell",
                reference_price=legacy_reference,
                actual_fill_price=avg_fill_price,
            )

        record = TradeRecord(
            timestamp=now_iso,
            symbol=symbol,
            side="sell",
            qty=qty,
            avg_fill_price=avg_fill_price,
            order_id=order_id,
            strategy=strategy,
            reason="stop_triggered",
            stop_price=avg_fill_price,
            entry_reference_price=entry_reference_price,
            modeled_slippage_bps=modeled_slippage_bps,
            realized_slippage_bps=round(realized_slippage_bps, 2),
            order_type="stop",
            status="filled",
            requested_qty=qty,
            filled_qty=qty,
            initial_stop_loss=initial_stop_loss,
            initial_risk_per_share=initial_risk_per_share,
            initial_risk_dollars=initial_risk_dollars,
            realized_pnl=realized_pnl,
            r_multiple=r_multiple,
            entry_timestamp=entry_timestamp,
            exit_timestamp=now_iso,
            position_id=owner_key_for(symbol),
            position_type="single_leg",
            slippage_benchmark_price=new_slippage_benchmark_price,
            slippage_benchmark_kind=new_slippage_benchmark_kind,
            slippage_benchmark_timestamp=new_slippage_benchmark_timestamp,
            slippage_measurement_quality=new_slippage_quality,
            slippage_signed_bps=(
                round(new_slippage_signed_bps, 2)
                if new_slippage_signed_bps is not None
                else None
            ),
            slippage_adverse_bps=(
                round(new_slippage_adverse_bps, 2)
                if new_slippage_adverse_bps is not None
                else None
            ),
            stop_trigger_price=new_stop_trigger_price,
        )
        self.log(record)

    def read_owner_for_symbol(self, symbol: str) -> str | None:
        """
        Return the strategy_name that owns the currently-open position for
        `symbol` per the trade log, or None if the position is closed (or
        never traded).
        """
        state = self._read_single_leg_open_state().get(symbol)
        if state is None or state["open_qty"] <= 0:
            return None
        return state["strategy"]

    def read_all_open_owners(self) -> dict[str, str]:
        """
        Return ``{symbol: strategy_name}`` for every still-open single-leg
        position reconstructed from the append-only trade log.

        This is the trade log's view of currently-open positions and their
        owning strategies, including positions that have been partially
        reduced by one or more sell rows. Used by the engine on startup to
        restore durable ownership without guessing from slot order.
        """
        return {
            symbol: state["strategy"]
            for symbol, state in self._read_single_leg_open_state().items()
            if state["open_qty"] > 0 and state["strategy"]
        }

    def read_open_spread_positions(self) -> list[dict]:
        """
        Reconstruct currently-open multi-leg credit spreads from the trade log.

        Returns one dict per open spread::

            {
              "position_id": str,
              "strategy": str,
              "leg_symbols": [occ, occ],   # both legs, append order
              "net_credit": float,         # $/share collected at open
              "qty": float,
            }

        A spread is *open* when its ``position_id`` has no row carrying an
        ``exit_timestamp`` (the close writes ``exit_timestamp`` on both leg
        rows). ``net_credit`` is taken from the open row whose
        ``avg_fill_price`` is non-zero — ``log_spread_fill`` puts the combo
        net on the short-leg row and 0.0 on the long-leg row.

        Used by the engine on startup to rebuild spread ``Position`` records
        instead of mis-assigning the legs as single-leg positions.
        """
        if not os.path.exists(self._path):
            return []
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return []
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT position_id, strategy, symbol, side, avg_fill_price, "
            "       qty, entry_timestamp, exit_timestamp, id "
            "FROM trades "
            "WHERE position_type = 'spread' "
            "AND status IN ('filled', 'partial') "
            "ORDER BY id"
        )
        grouped: dict[str, dict] = {}
        for row in cursor.fetchall():
            pid = row["position_id"]
            if pid is None:
                continue
            entry = grouped.setdefault(
                pid,
                {
                    "position_id": pid,
                    "strategy": row["strategy"],
                    "leg_symbols": [],
                    "net_credit": 0.0,
                    "qty": float(row["qty"] or 0.0),
                    "_closed": False,
                },
            )
            if row["exit_timestamp"] is not None:
                entry["_closed"] = True
                continue
            # Open-side leg row.
            sym = row["symbol"]
            if sym not in entry["leg_symbols"]:
                entry["leg_symbols"].append(sym)
            price = float(row["avg_fill_price"] or 0.0)
            if price > 0.0:
                entry["net_credit"] = price
                entry["qty"] = float(row["qty"] or entry["qty"])
        return [
            {k: v for k, v in entry.items() if k != "_closed"}
            for entry in grouped.values()
            if not entry["_closed"] and len(entry["leg_symbols"]) == 2
        ]

    def read_strategy_realized_pnl_summary(
        self,
        strategies: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> dict[str, dict[str, float]]:
        """
        Reconstruct per-strategy cumulative realized P&L and HWM from the trade log.

        Contributing rows: single-leg sell-side closes, plus credit-spread
        rows (``position_type='spread'``) — both with a non-null
        ``realized_pnl``. The HWM is the running maximum of cumulative
        realized P&L in append order.
        """
        include = set(strategies or [])
        summary = {
            strategy: {"realized_pnl": 0.0, "hwm": 0.0}
            for strategy in include
        }
        if not os.path.exists(self._path):
            return summary
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return summary
        conn.row_factory = sqlite3.Row
        # Single-leg closes are sell-side; credit-spread closes write
        # realized_pnl on the short-leg row, which is side='buy' (bought to
        # close) — so also accept position_type='spread' rows. The
        # `realized_pnl IS NOT NULL` filter prevents double-counting: spread
        # opens and the long-leg close row all carry NULL realized_pnl.
        cursor = conn.execute(
            "SELECT strategy, realized_pnl "
            "FROM trades "
            "WHERE (side = 'sell' OR position_type = 'spread') "
            "AND status IN ('filled', 'partial') "
            "AND realized_pnl IS NOT NULL "
            "ORDER BY id ASC"
        )
        for row in cursor.fetchall():
            strategy = row["strategy"]
            if include and strategy not in include:
                continue
            if strategy not in summary:
                summary[strategy] = {"realized_pnl": 0.0, "hwm": 0.0}
            running = summary[strategy]["realized_pnl"] + float(row["realized_pnl"])
            summary[strategy]["realized_pnl"] = running
            if running > summary[strategy]["hwm"]:
                summary[strategy]["hwm"] = running
        return summary

    def read_latest_open_stop_price(
        self, *, symbol: str, strategy: str
    ) -> float | None:
        """
        Return the original fixed stop price for the latest still-open trade
        on `symbol` owned by `strategy`.
        """
        state = self._read_single_leg_open_state().get(symbol)
        if state is None or state["open_qty"] <= 0 or state["strategy"] != strategy:
            return None
        stop_price = float(state.get("stop_price") or 0.0)
        return stop_price if stop_price > 0 else None

    def _read_latest_open_entry_context(
        self, *, symbol: str, strategy: str
    ) -> dict | None:
        """Return the latest still-open entry context for symbol/strategy."""
        state = self._read_single_leg_open_state().get(symbol)
        if state is None or state["open_qty"] <= 0 or state["strategy"] != strategy:
            return None
        return {
            "entry_reference_price": state.get("entry_reference_price"),
            "initial_stop_loss": state.get("initial_stop_loss"),
            "initial_risk_per_share": state.get("initial_risk_per_share"),
            "entry_timestamp": state.get("entry_timestamp"),
        }

    def has_recorded_order_id(self, order_id: str | None) -> bool:
        """True if the trade log already contains ``order_id``."""
        if not order_id or not os.path.exists(self._path):
            return False
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return False
        cursor = conn.execute(
            "SELECT 1 FROM trades WHERE order_id = ? LIMIT 1",
            (order_id,),
        )
        return cursor.fetchone() is not None

    def _read_single_leg_open_state(self) -> dict[str, dict[str, Any]]:
        """Return the current single-leg open-position state keyed by symbol."""
        if not os.path.exists(self._path):
            return {}
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return {}
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT symbol, strategy, side, qty, filled_qty, stop_price, "
            "entry_reference_price, initial_stop_loss, initial_risk_per_share, "
            "entry_timestamp "
            "FROM trades "
            "WHERE status IN ('filled', 'partial') "
            "AND (position_type IS NULL OR position_type != 'spread') "
            "ORDER BY id"
        )

        state: dict[str, dict[str, Any]] = {}
        for row in cursor.fetchall():
            symbol = row["symbol"]
            side = str(row["side"] or "").lower()
            qty_raw = row["filled_qty"] if row["filled_qty"] is not None else row["qty"]
            qty = float(qty_raw or 0.0)
            current = state.get(symbol)
            if current is None:
                current = {
                    "strategy": None,
                    "open_qty": 0.0,
                    "stop_price": None,
                    "entry_reference_price": None,
                    "initial_stop_loss": None,
                    "initial_risk_per_share": None,
                    "entry_timestamp": None,
                }
                state[symbol] = current

            if side == "buy":
                if current["open_qty"] <= 0:
                    current["strategy"] = row["strategy"]
                    current["stop_price"] = (
                        float(row["stop_price"]) if row["stop_price"] is not None else None
                    )
                    current["entry_reference_price"] = (
                        float(row["entry_reference_price"])
                        if row["entry_reference_price"] is not None else None
                    )
                    current["initial_stop_loss"] = (
                        float(row["initial_stop_loss"])
                        if row["initial_stop_loss"] is not None else None
                    )
                    current["initial_risk_per_share"] = (
                        float(row["initial_risk_per_share"])
                        if row["initial_risk_per_share"] is not None else None
                    )
                    current["entry_timestamp"] = row["entry_timestamp"]
                current["open_qty"] += qty
                continue

            if side == "sell":
                if qty <= 0:
                    current["open_qty"] = 0.0
                else:
                    current["open_qty"] = max(0.0, current["open_qty"] - qty)
                if current["open_qty"] <= 1e-9:
                    current["strategy"] = None
                    current["stop_price"] = None
                    current["entry_reference_price"] = None
                    current["initial_stop_loss"] = None
                    current["initial_risk_per_share"] = None
                    current["entry_timestamp"] = None
        return state

    def read_latest_open_entry_context(
        self, *, symbol: str, strategy: str
    ) -> dict | None:
        """Public wrapper for the latest open entry context lookup."""
        return self._read_latest_open_entry_context(symbol=symbol, strategy=strategy)
