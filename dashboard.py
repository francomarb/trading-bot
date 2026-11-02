"""
Read-only operator and analytics dashboard.

Connects to trades.db and the engine state snapshot to visualize:
  - A concise operational overview
  - Positions, sleeve allocation, and concentration risk
  - Per-strategy performance and health evidence
  - Read-only market observations
  - Trade and execution-quality history

Run with:
    bash start_dashboard.sh
    # or directly:
    streamlit run dashboard.py

The dashboard is read-only: it never places orders or mutates engine state.
Operational pages refresh independently so slow research panels do not force
the whole application to rerun.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone, tzinfo
from dataclasses import dataclass
from pathlib import Path
from collections import deque
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings
from engine.lifecycle_orders import OWNER_KEY_LOCK_STATUSES
from engine.positions import build_credit_spread_snapshot
from reporting.logger import (
    STOP_GAP_KINDS,
    is_execution_quality_measurement,
)
from strategies.health.edge import NEGATIVE_PERSISTENCE_WEEKS_REQUIRED
from monitors.leveraged_trend import (
    Phase,
    TrendMonitorState,
    load_all_monitor_states,
)


# ── Data loading helpers (pure functions — tested independently) ─────────────


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def broker_position_detail(position: Any) -> dict[str, Any]:
    """
    Normalize a broker position for dashboard reads without recomputing P/L.

    Alpaca already reports option P/L at the contract-multiplier scale. The
    dashboard preserves those broker fields instead of deriving leg P/L from
    market value, which is easy to get wrong for options.
    """
    unrealized_pl = _as_float(getattr(position, "unrealized_pl", None))
    if unrealized_pl is None:
        unrealized_pl = _as_float(getattr(position, "unrealized_pnl", None))
    return {
        "qty": _as_float(getattr(position, "qty", None)),
        "avg_entry_price": _as_float(getattr(position, "avg_entry_price", None)),
        "current_price": _as_float(getattr(position, "current_price", None)),
        "market_value": _as_float(getattr(position, "market_value", None)),
        "cost_basis": _as_float(getattr(position, "cost_basis", None)),
        "unrealized_pl": unrealized_pl,
        "unrealized_pnl": unrealized_pl,
        "unrealized_plpc": _as_float(getattr(position, "unrealized_plpc", None)),
    }


def merge_display_positions_detail(
    state: dict[str, Any],
    broker_positions_detail: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """
    Merge broker position detail onto snapshot-owned position keys.

    Single-leg positions are keyed by their exact broker symbol. This preserves
    separate rows for distinct option contracts on the same underlying.
    """
    if not broker_positions_detail:
        return dict(state.get("positions_detail") or {})

    positions_detail: dict[str, dict[str, Any]] = {}
    snapshot_positions = state.get("positions_detail") or {}
    for sym, detail in broker_positions_detail.items():
        snapshot_detail = snapshot_positions.get(sym, {})
        enriched = dict(snapshot_detail)
        enriched.update(detail)
        positions_detail[sym] = enriched

    for sym, detail in snapshot_positions.items():
        positions_detail.setdefault(sym, dict(detail))

    return positions_detail


def _local_timezone() -> tzinfo:
    """Best-effort local timezone for the host running Streamlit."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def format_local_timestamp(
    value: Any,
    *,
    target_tz: tzinfo | None = None,
    include_zone: bool = True,
) -> str:
    """
    Format a timestamp in the host's local timezone.

    Accepts pandas timestamps, datetimes, or ISO-ish strings. Naive values are
    treated as UTC because the trade log and engine snapshot persist UTC.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"

    tz = target_tz or _local_timezone()
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return str(value)

    if pd.isna(ts):
        return "—"

    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    else:
        ts = ts.tz_convert(timezone.utc)

    local_ts = ts.tz_convert(tz)
    fmt = "%Y-%m-%d %H:%M %Z" if include_zone else "%Y-%m-%d %H:%M"
    return local_ts.strftime(fmt)


def load_trades(db_path: str) -> pd.DataFrame:
    """Load all rows from the trades table. Returns empty DataFrame if missing."""
    def _empty(error: str | None = None) -> pd.DataFrame:
        df = pd.DataFrame(columns=[
            "id", "timestamp", "symbol", "side", "qty", "avg_fill_price",
            "order_id", "strategy", "reason", "stop_price",
            "entry_reference_price", "modeled_slippage_bps",
            "realized_slippage_bps", "order_type", "status",
            "requested_qty", "filled_qty", "initial_stop_loss",
            "initial_risk_per_share", "initial_risk_dollars",
            "realized_pnl", "r_multiple", "entry_timestamp",
            "exit_timestamp",
            "slippage_benchmark_kind", "slippage_measurement_quality",
            "slippage_signed_bps", "slippage_adverse_bps",
        ])
        if error is not None:
            df.attrs["load_error"] = error
        return df

    path = Path(db_path)
    if not path.exists():
        return _empty()
    try:
        with sqlite3.connect(str(path)) as conn:
            df = pd.read_sql_query("SELECT * FROM trades ORDER BY timestamp ASC", conn)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    except Exception as exc:
        return _empty(f"Failed to load trades DB '{db_path}': {type(exc).__name__}: {exc}")


def load_open_lifecycle_identities(db_path: str) -> dict[str, dict[str, str]]:
    """Return unambiguous durable identities for active single-leg owners.

    The dashboard opens SQLite explicitly in read-only mode. Duplicate active
    claims should be prevented by the lifecycle schema; if a damaged database
    contains them, omit that owner rather than displaying a guessed identity.
    """
    path = Path(db_path)
    if not path.exists():
        return {}

    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            status_placeholders = ", ".join(
                "?" for _ in OWNER_KEY_LOCK_STATUSES
            )
            rows = conn.execute(
                f"""
                SELECT owner_key, position_uid, strategy
                FROM position_lifecycle
                WHERE position_type = 'single_leg'
                  AND status IN ({status_placeholders})
                  AND owner_key IS NOT NULL
                ORDER BY created_at ASC
                """,
                OWNER_KEY_LOCK_STATUSES,
            ).fetchall()
    except (OSError, sqlite3.Error):
        return {}

    by_owner: dict[str, list[tuple[str, str]]] = {}
    for owner_key, position_uid, strategy in rows:
        if not owner_key or not position_uid or not strategy:
            continue
        by_owner.setdefault(str(owner_key), []).append(
            (str(position_uid), str(strategy))
        )

    return {
        owner_key: {"position_uid": claims[0][0], "strategy": claims[0][1]}
        for owner_key, claims in by_owner.items()
        if len(claims) == 1
    }


def attach_open_position_lifecycle_ids(
    positions_detail: dict[str, dict[str, Any]],
    open_positions: dict[str, str],
    lifecycle_identities: dict[str, dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Attach exact, strategy-matched lifecycle IDs to display-only rows."""
    enriched = {symbol: dict(detail) for symbol, detail in positions_detail.items()}
    for symbol, strategy in open_positions.items():
        identity = lifecycle_identities.get(symbol)
        if identity is None or identity.get("strategy") != strategy:
            continue
        detail = enriched.setdefault(symbol, {})
        detail["position_uid"] = identity.get("position_uid")
    return enriched


def _realized_pnl_events(
    trades_df: pd.DataFrame,
    *,
    key_columns: tuple[str, ...] = ("symbol",),
) -> list[dict]:
    """
    FIFO-match buy lots to sells and return realized P&L events.

    Partial exits are handled correctly: any remaining quantity stays on the
    front lot until fully consumed by later sells.
    """
    if trades_df.empty or "side" not in trades_df.columns:
        return []

    open_lots: dict[tuple, deque] = {}
    events: list[dict] = []

    for _, row in trades_df.sort_values("timestamp").iterrows():
        key = tuple(row.get(col, "") for col in key_columns)
        side = (row.get("side") or "").lower()
        qty = float(row.get("filled_qty") or row.get("qty") or 0)
        price = float(row.get("avg_fill_price") or 0)
        ts = row.get("timestamp")

        if qty <= 0 or price <= 0:
            continue

        if side == "buy":
            open_lots.setdefault(key, deque()).append([qty, price])
            continue

        if side != "sell":
            continue

        lots = open_lots.setdefault(key, deque())
        remaining_qty = qty
        realized_pnl = 0.0
        matched_qty = 0.0

        while remaining_qty > 0 and lots:
            lot_qty, lot_price = lots[0]
            fill_qty = min(remaining_qty, lot_qty)
            realized_pnl += (price - lot_price) * fill_qty
            matched_qty += fill_qty
            remaining_qty -= fill_qty
            lot_qty -= fill_qty

            if lot_qty == 0:
                lots.popleft()
            else:
                lots[0][0] = lot_qty

        if matched_qty > 0:
            events.append({
                "timestamp": ts,
                "pnl": realized_pnl,
                "matched_qty": matched_qty,
            })

    return events


def realized_trade_events(trades_df: pd.DataFrame) -> list[dict]:
    """
    Return normalized realized-trade events across single-leg and spread trades.

    Single-leg trades are counted only once the logged exit quantity fully
    closes the original entry quantity. Spread trades are keyed by
    ``position_id`` and use rows with non-null ``realized_pnl``.
    """
    if trades_df.empty or "strategy" not in trades_df.columns:
        return []

    events: list[dict] = []
    position_type = trades_df.get(
        "position_type", pd.Series("", index=trades_df.index)
    )
    is_mleg = position_type.astype(str).str.lower().isin({"spread", "mleg"})
    single_leg_group = trades_df[~is_mleg].copy()
    single_leg_entry_ts = single_leg_group.get(
        "entry_timestamp", pd.Series(index=single_leg_group.index)
    )
    entries = single_leg_group[
        (single_leg_group.get("side", "").astype(str).str.lower() == "buy")
        & single_leg_entry_ts.notna()
    ].copy()
    exits = single_leg_group[
        (single_leg_group.get("side", "").astype(str).str.lower() == "sell")
        & single_leg_entry_ts.notna()
    ].copy()

    if not entries.empty and not exits.empty:
        entries["filled_qty_num"] = pd.to_numeric(
            entries["filled_qty"], errors="coerce"
        ).fillna(0.0)
        exits["filled_qty_num"] = pd.to_numeric(
            exits["filled_qty"], errors="coerce"
        ).fillna(0.0)
        exits["realized_pnl"] = pd.to_numeric(
            exits["realized_pnl"], errors="coerce"
        )

        entry_groups = entries.groupby(
            ["strategy", "symbol", "entry_timestamp"], dropna=True, sort=False
        ).agg(entry_qty=("filled_qty_num", "sum"))
        grouped = exits.groupby(
            ["strategy", "symbol", "entry_timestamp"], dropna=True, sort=False
        ).agg(
            realized_pnl=("realized_pnl", "sum"),
            exit_qty=("filled_qty_num", "sum"),
            timestamp=("timestamp", "max"),
        )
        grouped = grouped.join(entry_groups, how="inner")
        grouped = grouped[grouped["exit_qty"] >= (grouped["entry_qty"] - 1e-9)]

        for _, row in grouped.reset_index().iterrows():
            events.append({
                "timestamp": row["timestamp"],
                "pnl": float(row["realized_pnl"] or 0.0),
            })

    mleg_exits = trades_df[
        is_mleg
        & trades_df.get("position_id", pd.Series(index=trades_df.index)).notna()
        & trades_df.get("realized_pnl", pd.Series(index=trades_df.index)).notna()
    ].copy()
    if not mleg_exits.empty:
        mleg_exits["realized_pnl"] = pd.to_numeric(
            mleg_exits["realized_pnl"], errors="coerce"
        )
        grouped = mleg_exits.groupby(
            ["strategy", "position_id"], dropna=True, sort=False
        ).agg(
            realized_pnl=("realized_pnl", "sum"),
            timestamp=("timestamp", "max"),
        )
        for _, row in grouped.reset_index().iterrows():
            if pd.isna(row["realized_pnl"]):
                continue
            events.append({
                "timestamp": row["timestamp"],
                "pnl": float(row["realized_pnl"]),
            })

    events.sort(key=lambda e: e["timestamp"])
    return events


def load_engine_state(path: str) -> dict:
    """Read engine_state.json. Returns {} if missing or malformed."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def watchlist_symbol_state(
    watchlist_map: dict[str, dict[str, Any]],
    strategy_name: str,
    symbol: str,
) -> Any:
    """Read per-symbol watchlist state from legacy or slot-qualified keys."""
    qualified_key = f"{strategy_name}:{symbol}"
    if qualified_key in watchlist_map and symbol in watchlist_map[qualified_key]:
        return watchlist_map[qualified_key][symbol]

    legacy_state = watchlist_map.get(strategy_name, {})
    if symbol in legacy_state:
        return legacy_state[symbol]

    for key, symbol_map in watchlist_map.items():
        if key.startswith(f"{strategy_name}:") and symbol in symbol_map:
            return symbol_map[symbol]

    return None


def regime_gate_allows(
    regime: str | None,
    allowed_regimes: set[str] | None,
) -> bool | None:
    """Return dashboard entry-gate status for a strategy/regime pair.

    `None` or an empty set means unrestricted for dashboard/config display,
    matching the engine's `StrategySlot.allowed_regimes=None` convention.
    """
    if regime is None:
        return None
    if not allowed_regimes:
        return True
    return regime in allowed_regimes


@st.cache_data(ttl=60, show_spinner=False)
def load_broker_account_curve(live_trading: bool, period: str = "1M") -> pd.DataFrame:
    """
    Best-effort broker portfolio history for the account equity curve.

    The result is broker-reported account equity, so it includes unrealized
    P&L and should track Alpaca more closely than the realized trade log.
    """
    del live_trading  # Environment selection is already derived from settings.
    valid_periods = {"1W", "1M", "3M"}
    if period not in valid_periods:
        raise ValueError(f"unsupported broker account curve period: {period!r}")

    request_period = {
        "1W": "1W",
        "1M": "1M",
        "3M": "3M",
    }[period]

    def _empty(error: str | None = None) -> pd.DataFrame:
        df = pd.DataFrame(columns=["timestamp", "equity", "profit_loss", "profit_loss_pct"])
        if error is not None:
            df.attrs["load_error"] = error
        return df

    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        from execution.broker import AlpacaBroker

        history = AlpacaBroker()._api.get_portfolio_history(
            GetPortfolioHistoryRequest(
                period=request_period,
                timeframe="1D",
                extended_hours=False,
            )
        )
        timestamps = list(getattr(history, "timestamp", []) or [])
        equity = list(getattr(history, "equity", []) or [])
        profit_loss = list(getattr(history, "profit_loss", []) or [])
        profit_loss_pct = list(getattr(history, "profit_loss_pct", []) or [])
        if not timestamps or not equity:
            return _empty()

        length = min(len(timestamps), len(equity))
        if profit_loss:
            length = min(length, len(profit_loss))
        if profit_loss_pct:
            length = min(length, len(profit_loss_pct))

        df = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps[:length], unit="s", utc=True),
            "equity": pd.to_numeric(equity[:length], errors="coerce"),
            "profit_loss": pd.to_numeric(profit_loss[:length], errors="coerce"),
            "profit_loss_pct": pd.to_numeric(profit_loss_pct[:length], errors="coerce"),
        }).dropna(subset=["timestamp", "equity"])
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as exc:
        return _empty(
            f"Direct broker account curve unavailable: {type(exc).__name__}: {exc}"
        )


@st.cache_data(ttl=15, show_spinner=False)
def load_broker_account_metrics(
    live_trading: bool,
    session_start_equity: float | None,
) -> dict[str, Any]:
    """
    Best-effort direct broker refresh for top account metrics.

    This keeps Equity / Daily P&L closer to Alpaca than the engine snapshot
    cadence alone. The dashboard remains read-only: it only issues broker
    reads and falls back silently to the snapshot on failure.
    """
    del live_trading  # Environment selection is already derived from settings.
    try:
        from execution.broker import AlpacaBroker

        account = AlpacaBroker().get_account(
            session_start_equity=session_start_equity,
        )
        session_pnl = account.equity - account.session_start_equity
        daily_pnl = (
            account.equity - account.previous_close_equity
            if account.previous_close_equity is not None
            else session_pnl
        )
        return {
            "equity": account.equity,
            "session_start_equity": account.session_start_equity,
            "previous_close_equity": account.previous_close_equity,
            "daily_pnl": daily_pnl,
            "session_pnl": session_pnl,
            "positions_detail": {
                symbol: broker_position_detail(position)
                for symbol, position in account.open_positions.items()
            },
            "source": "broker",
        }
    except Exception as exc:
        return {
            "error": (
                f"Direct broker account refresh unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
            "source": "snapshot",
        }


def resolve_account_metrics(
    state: dict,
    broker_metrics: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Merge snapshot and live broker account data for dashboard metrics."""
    equity = float(state.get("equity", 0.0) or 0.0)
    daily_pnl = float(state.get("daily_pnl", 0.0) or 0.0)
    session_pnl = float(state.get("session_pnl", daily_pnl) or daily_pnl)
    previous_close = state.get("previous_close_equity")
    session_start_equity = float(state.get("session_start_equity", equity) or equity)
    source = "snapshot"
    warning = None

    if broker_metrics:
        if broker_metrics.get("error"):
            warning = str(broker_metrics["error"])
        else:
            equity = float(broker_metrics.get("equity", equity) or equity)
            daily_pnl = float(broker_metrics.get("daily_pnl", daily_pnl) or daily_pnl)
            session_pnl = float(
                broker_metrics.get("session_pnl", session_pnl) or session_pnl
            )
            previous_close = broker_metrics.get("previous_close_equity", previous_close)
            session_start_equity = float(
                broker_metrics.get("session_start_equity", session_start_equity)
                or session_start_equity
            )
            source = str(broker_metrics.get("source", "broker"))

    equity_delta = daily_pnl if previous_close is not None else session_pnl
    return {
        "equity": equity,
        "daily_pnl": daily_pnl,
        "session_pnl": session_pnl,
        "previous_close_equity": previous_close,
        "session_start_equity": session_start_equity,
        "equity_delta": equity_delta,
        "source": source,
    }, warning


def refresh_multi_leg_positions(
    state: dict[str, Any],
    broker_positions_detail: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Refresh normalized multi-leg snapshots with broker leg marks when available.

    Falls back to the engine snapshot row on any missing data so the dashboard
    stays read-only and resilient.
    """
    rows = list(state.get("multi_leg_positions") or [])
    if not rows:
        return []
    if not broker_positions_detail:
        return rows

    refreshed: list[dict[str, Any]] = []
    for row in rows:
        if row.get("structure") != "put_credit_spread":
            refreshed.append(row)
            continue
        try:
            refreshed.append(build_credit_spread_snapshot(
                position_id=str(row.get("position_id", "")),
                strategy=str(row.get("strategy", "")),
                underlying=str(row.get("underlying", "")),
                short_occ=str(row.get("short_occ", "")),
                long_occ=str(row.get("long_occ", "")),
                short_strike=float(row.get("short_strike", 0.0) or 0.0),
                long_strike=float(row.get("long_strike", 0.0) or 0.0),
                expiration=str(row.get("expiration", "")),
                entry_net_price=float(row.get("entry_net_price", 0.0) or 0.0),
                width=float(row.get("width", 0.0) or 0.0),
                qty=float(row.get("qty", 1.0) or 1.0),
                broker_positions=broker_positions_detail,
                underlying_price=row.get("underlying_price"),
                pending_close=bool(row.get("pending_close", False)),
            ))
        except Exception:
            refreshed.append(row)
    return refreshed


def multi_leg_display_rows(
    multi_leg_positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build compact dashboard rows for normalized multi-leg positions."""
    rows: list[dict[str, Any]] = []
    for pos in multi_leg_positions:
        short_strike = pos.get("short_strike")
        long_strike = pos.get("long_strike")
        distance_pct = pos.get("distance_to_short_strike_pct")
        legs = pos.get("legs") or []
        short_leg = next(
            (leg for leg in legs if str(leg.get("role", "")).lower() == "short"),
            {},
        )
        long_leg = next(
            (leg for leg in legs if str(leg.get("role", "")).lower() == "long"),
            {},
        )
        rows.append({
            "Structure": str(pos.get("structure", "")).replace("_", " ").title(),
            "Underlying": pos.get("underlying", ""),
            "Strikes": (
                f"{short_strike:.0f} / {long_strike:.0f}"
                if short_strike is not None and long_strike is not None
                else ""
            ),
            "Expiration": pos.get("expiration", ""),
            "DTE": pos.get("dte"),
            "Entry Credit": (
                float(pos.get("entry_net_price") or 0.0)
                * 100.0 * float(pos.get("qty") or 0.0)
            ),
            "Mark Debit": (
                None if pos.get("current_exit_price") is None
                else float(pos.get("current_exit_price") or 0.0)
                * 100.0 * float(pos.get("qty") or 0.0)
            ),
            "Net Spread P&L": pos.get("unrealized_pnl"),
            "Short Leg P&L": short_leg.get("unrealized_pnl"),
            "Long Leg P&L": long_leg.get("unrealized_pnl"),
            "Max Profit": pos.get("max_profit"),
            "Max Loss": pos.get("max_loss"),
            "Underlying Price": pos.get("underlying_price"),
            "Distance": pos.get("distance_to_short_strike"),
            "Distance %": (
                distance_pct * 100.0 if distance_pct is not None else None
            ),
            "Close State": "Closing" if pos.get("pending_close") else "Open",
            "Status": pos.get("status", ""),
        })
    return rows


def compute_equity_curve(trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build a cumulative P&L series from the trades table.

    Each sell row represents a closed trade; its P&L = (sell_price -
    last buy price) × qty. For simplicity we use avg_fill_price on each
    side and match buys to sells by symbol in chronological order.

    Returns a DataFrame with columns [timestamp, cumulative_pnl].
    """
    if trades_df.empty or "side" not in trades_df.columns:
        return pd.DataFrame(columns=["timestamp", "cumulative_pnl"])

    rows = realized_trade_events(trades_df)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "cumulative_pnl"])

    df = pd.DataFrame(rows).sort_values("timestamp")
    df["cumulative_pnl"] = df["pnl"].cumsum()
    return df[["timestamp", "cumulative_pnl"]]


def filter_realized_curve_window(
    curve_df: pd.DataFrame,
    period: str,
) -> pd.DataFrame:
    """Filter a realized equity curve to 1W, 1M, or All."""
    if curve_df.empty:
        return curve_df
    if period == "All":
        return curve_df

    latest_ts = pd.to_datetime(curve_df["timestamp"]).max()
    if pd.isna(latest_ts):
        return curve_df

    if period == "1W":
        cutoff = latest_ts - pd.Timedelta(days=7)
    elif period == "1M":
        cutoff = latest_ts - pd.Timedelta(days=31)
    else:
        raise ValueError(f"unsupported realized curve period: {period}")

    filtered = curve_df[pd.to_datetime(curve_df["timestamp"]) >= cutoff].copy()
    if filtered.empty:
        return filtered

    baseline = float(filtered["cumulative_pnl"].iloc[0])
    filtered["cumulative_pnl"] = filtered["cumulative_pnl"] - baseline
    return filtered


def compute_rolling_sharpe(
    equity_series: pd.Series, window: int = 20
) -> pd.Series:
    """
    Rolling annualized Sharpe ratio from a cumulative P&L series.

    Uses daily returns (diff). Window must be >= 2 for std to be defined.
    """
    if len(equity_series) < 2:
        return pd.Series(dtype=float)
    returns = equity_series.diff().dropna()
    if len(returns) < window:
        window = max(2, len(returns))
    rolling_mean = returns.rolling(window).mean()
    rolling_std = returns.rolling(window).std()
    sharpe = (rolling_mean / rolling_std.replace(0, float("nan"))) * (252 ** 0.5)
    return sharpe


# OCC option symbols carry a 100x contract multiplier. Same shape as
# `reporting.logger._OCC_OPTION_SYMBOL`; kept local so the dashboard does
# not import a private name.
_OCC_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")


def slippage_measurement_note(row: pd.Series) -> str:
    """Explain what a Recent Trades slippage cell does—or does not—measure.

    The note is derived only from persisted trade provenance. It deliberately
    avoids claiming that ``unavailable`` means a stale quote: that state is
    shared by option exits, fractional-residual cleanup, recovery/external
    rows, passive spread legs, and genuinely missing benchmarks.
    """
    def _text(name: str) -> str:
        value = row.get(name)
        return "" if value is None or pd.isna(value) else str(value).strip()

    kind = _text("slippage_benchmark_kind").lower()
    quality = _text("slippage_measurement_quality").lower()
    order_type = _text("order_type").lower()
    position_type = _text("position_type").lower()
    reason = _text("reason").lower()
    symbol = _text("symbol").upper()
    qty = _as_float(row.get("filled_qty"))
    if qty is None:
        qty = _as_float(row.get("qty"))

    quality_suffix = (
        " (recovered from broker history)" if quality == "recovered" else ""
    )
    if kind == "arrival_midpoint":
        return "Execution vs IEX midpoint at submission" + quality_suffix
    if kind == "combo_limit":
        return "Spread execution vs submitted combo limit" + quality_suffix
    if kind == "active_stop_price":
        return "Stop gap vs active trigger; not execution quality" + quality_suffix
    if kind == "fallback_latest_close":
        return "Fallback vs latest bar close; not execution quality"
    if kind == "decision_price":
        return "Implementation shortfall vs strategy decision price"
    if kind == "limit_price":
        return "Not measured: limit/stop-limit fill"

    if kind == "unavailable":
        if _OCC_SYMBOL_RE.match(symbol) and order_type == "market":
            return "Not measured: option arrival benchmark unsupported"
        if position_type in {"spread", "mleg"}:
            return "No per-leg measurement; combo metric is on the economic row"
        if "external" in reason:
            return "Not measured: external close has no original benchmark"
        if quality == "recovered":
            return "Not measured: original benchmark unavailable during recovery"
        if (
            order_type == "market"
            and _text("side").lower() == "sell"
            and qty is not None
            and 0 < abs(qty) < 1
        ):
            return "No benchmark; fractional residual cleanups use this state"
        return "Not measured: no valid benchmark was persisted"

    if not kind:
        return "Not measured: legacy row has no benchmark provenance"
    return f"Measured vs {kind} benchmark"


def recent_trade_realized_pnl(row: pd.Series) -> float | None:
    """Return persisted realized P&L for a genuine close row.

    Single-leg closes are sells. Credit-spread economics ride the short-leg
    row, which is a buy-to-close, so side alone cannot identify every close.
    Fractional-residual cleanup remains hidden because the whole-share stop
    row is the operator-facing lifecycle result. The trade log remains the
    source of truth; the dashboard never reconstructs P&L from prices.
    """
    side_value = row.get("side")
    type_value = row.get("position_type")
    reason_value = row.get("reason")
    side = "" if pd.isna(side_value) else str(side_value).strip().lower()
    position_type = "" if pd.isna(type_value) else str(type_value).strip().lower()
    reason = "" if pd.isna(reason_value) else str(reason_value).strip().lower()
    is_close_side = side == "sell" or (
        side == "buy" and position_type == "spread"
    )
    if not is_close_side or reason == "fractional residual cleanup":
        return None
    return _as_float(row.get("realized_pnl"))


def _stop_gap_dollars(frame: pd.DataFrame) -> float:
    """Total dollars lost past the stop trigger for one strategy's rows.

    PLAN 11.49. Stop-gap erosion is the loss taken *beyond* the planned
    risk when a protective stop fills below the level it was set at —
    AAPL 2026-07-31 held a stop at 320.74, the stock opened at 304.81,
    and $204 of a $448.81 loss landed past the point the bot believed it
    had exited.

    Adverse-only, matching `slippage_adverse_bps`: a fill better than the
    trigger contributes 0 rather than netting away a real gap elsewhere.
    Options carry a 100× contract multiplier.

    Returns NaN when the strategy has no stop fills at all, so the column
    renders blank. A zero would read as "stops cost nothing" when the
    truth is "this strategy has no stops" — credit_spread is defined-risk
    and never places one.
    """
    kind = frame.get(
        "slippage_benchmark_kind", pd.Series(index=frame.index, dtype=object)
    )
    rows = frame[kind.isin(STOP_GAP_KINDS)]
    if rows.empty:
        return float("nan")
    trigger = pd.to_numeric(rows.get("stop_trigger_price"), errors="coerce")
    fill = pd.to_numeric(rows.get("avg_fill_price"), errors="coerce")
    qty = pd.to_numeric(rows.get("filled_qty"), errors="coerce")
    mult = rows.get(
        "symbol", pd.Series(index=rows.index, dtype=object)
    ).map(lambda s: 100 if _OCC_SYMBOL_RE.match(str(s or "")) else 1)
    excess = ((trigger - fill) * qty * mult).clip(lower=0.0)
    total = float(excess.sum(skipna=True))
    return total if excess.notna().any() else float("nan")


def _execution_quality_mask(
    frame: pd.DataFrame, index: pd.Index
) -> "pd.Series[bool]":
    """Pandas adapter for the canonical execution-quality predicate."""
    kind = frame.get(
        "slippage_benchmark_kind", pd.Series(index=index, dtype=object)
    ).reindex(index)
    quality = frame.get(
        "slippage_measurement_quality", pd.Series(index=index, dtype=object)
    ).reindex(index)
    version = pd.to_numeric(
        frame.get(
            "slippage_measurement_version",
            pd.Series(index=index, dtype=object),
        ).reindex(index),
        errors="coerce",
    )
    return pd.Series(
        (
            is_execution_quality_measurement(
                benchmark_kind,
                measurement_quality,
                None if pd.isna(measurement_version) else int(measurement_version),
            )
            for benchmark_kind, measurement_quality, measurement_version
            in zip(kind, quality, version, strict=True)
        ),
        index=index,
        dtype=bool,
    )


def compute_strategy_stats(trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute per-strategy summary: trades, wins, win_rate, total_pnl,
    avg_adverse_slippage_bps.

    Single-leg trades are fully closed positions aggregated across exit rows
    that share strategy / symbol / entry_timestamp. Multi-leg trades are
    realized-P&L close events keyed by position_id, so spreads do not need to
    mimic single-leg buy/sell shape to show up in the dashboard.

    Phase 2 slippage unification: reads `slippage_adverse_bps` and
    applies the same positive quality whitelist used by health /
    calibration / reconcile / pnl (`slippage_measurement_quality IN
    ('primary', 'fallback')`). Rows whose quality is not in the
    whitelist (`recovered`, `unavailable`, future enums) are masked
    to NaN before aggregation so reconstructed or synthetic
    measurements don't pollute the operator-facing strategy-level
    average.

    The weighted average uses the same `.notna()` mask (post-whitelist)
    for numerator and denominator so rows without a calibration-grade
    measurement contribute neither value nor weight. Pre-Phase 2 the
    denominator summed all `filled_qty_num` for the exit group and
    silently diluted the average by counting rows whose slippage was
    NULL / zero-by-default.
    """
    if trades_df.empty or "strategy" not in trades_df.columns:
        return pd.DataFrame(columns=[
            "strategy", "trades", "wins", "win_rate",
            "total_pnl", "avg_adverse_slippage_bps",
        ])

    results = []
    for strategy, group in trades_df.groupby("strategy"):
        position_type = group.get("position_type", pd.Series("", index=group.index))
        is_mleg = position_type.astype(str).str.lower().isin({"spread", "mleg"})
        single_leg_group = group[~is_mleg].copy()

        single_leg_entry_ts = single_leg_group.get(
            "entry_timestamp", pd.Series(index=single_leg_group.index)
        )
        entries = single_leg_group[
            (single_leg_group.get("side", "").astype(str).str.lower() == "buy")
            & single_leg_entry_ts.notna()
        ].copy()
        exits = single_leg_group[
            (single_leg_group.get("side", "").astype(str).str.lower() == "sell")
            & single_leg_entry_ts.notna()
        ].copy()
        pnls: list[float] = []
        slippage_numer = 0.0
        slippage_denom = 0.0

        if entries.empty or exits.empty:
            pass
        else:
            entries["filled_qty_num"] = pd.to_numeric(
                entries["filled_qty"], errors="coerce"
            ).fillna(0.0)
            exits["realized_pnl"] = pd.to_numeric(exits["realized_pnl"], errors="coerce")
            # No fillna here — preserving NaN is what drives the
            # denominator-dilution fix below. Rows whose slippage
            # column is NULL (LIMIT entries, external closes, etc.)
            # must contribute neither value nor weight to the
            # weighted average.
            #
            # Shared execution-quality contract: mask wrong benchmark
            # families, reconstructed qualities and pre-contract arrival
            # rows, matching health / calibration / reconcile / pnl.
            adverse_series = pd.to_numeric(
                exits.get("slippage_adverse_bps", pd.Series(index=exits.index)),
                errors="coerce",
            )
            exits["slippage_adverse_bps"] = adverse_series.where(
                _execution_quality_mask(exits, exits.index)
            )
            exits["filled_qty_num"] = pd.to_numeric(
                exits["filled_qty"], errors="coerce"
            ).fillna(0.0)

            entry_groups = entries.groupby(
                ["symbol", "entry_timestamp"], dropna=True, sort=False
            ).agg(entry_qty=("filled_qty_num", "sum"))
            grouped = exits.groupby(
                ["symbol", "entry_timestamp"], dropna=True, sort=False
            ).agg(
                realized_pnl=("realized_pnl", "sum"),
                exit_qty=("filled_qty_num", "sum"),
                # Numerator + denominator gated on the same notna()
                # mask: a row with NULL slippage contributes 0 to
                # both, so the weighted average is over the measured
                # rows only and not diluted by the missing ones.
                slippage_numer=(
                    "slippage_adverse_bps",
                    lambda s: float(
                        (s * exits.loc[s.index, "filled_qty_num"])
                        .where(s.notna())
                        .sum()
                    ),
                ),
                slippage_denom=(
                    "slippage_adverse_bps",
                    lambda s: float(
                        exits.loc[s.index, "filled_qty_num"]
                        .where(s.notna())
                        .sum()
                    ),
                ),
            )
            grouped = grouped.join(entry_groups, how="inner")
            grouped = grouped[
                grouped["exit_qty"] >= (grouped["entry_qty"] - 1e-9)
            ]

            if not grouped.empty:
                pnls.extend(grouped["realized_pnl"].fillna(0.0).tolist())
                slippage_numer += float(grouped["slippage_numer"].sum())
                slippage_denom += float(grouped["slippage_denom"].sum())

        mleg_exits = group[
            is_mleg
            & group.get("position_id", pd.Series(index=group.index)).notna()
            & group.get("realized_pnl", pd.Series(index=group.index)).notna()
        ].copy()
        if not mleg_exits.empty:
            mleg_exits["realized_pnl"] = pd.to_numeric(
                mleg_exits["realized_pnl"], errors="coerce"
            )
            mleg_exits["filled_qty_num"] = pd.to_numeric(
                mleg_exits["filled_qty"], errors="coerce"
            ).fillna(0.0)
            mleg_grouped = mleg_exits.groupby(
                "position_id", dropna=True, sort=False
            ).agg(
                realized_pnl=("realized_pnl", "sum"),
            )
            mleg_grouped = mleg_grouped[mleg_grouped["realized_pnl"].notna()]
            pnls.extend(mleg_grouped["realized_pnl"].fillna(0.0).tolist())

            # For completed MLEG positions, include both entry and
            # close rows. The long-leg row writes NULL on
            # slippage_adverse_bps under Phase 1 codepath §11; the
            # short leg carries the economic combo_limit measurement.
            # The same .notna() mask used below excludes the long-leg
            # rows naturally — no need for the pre-Phase 2 avg_fill_
            # price>0 workaround that special-cased the structural-zero
            # row.
            completed_ids = set(mleg_grouped.index)
            position_ids = group.get("position_id", pd.Series(index=group.index))
            mleg_slippage_rows = group[
                is_mleg & position_ids.isin(completed_ids)
            ].copy()
            if not mleg_slippage_rows.empty:
                mleg_adverse = pd.to_numeric(
                    mleg_slippage_rows.get(
                        "slippage_adverse_bps",
                        pd.Series(index=mleg_slippage_rows.index),
                    ),
                    errors="coerce",
                )
                # Execution-quality filter — same shape as the
                # single-leg branch above. Recovered / unavailable MLEG
                # rows and any non-execution benchmark family are masked
                # out of the strategy average. Spread short legs carry
                # `combo_limit`, which is execution quality, so completed
                # MLEG positions still contribute.
                mleg_slippage_rows["slippage_adverse_bps"] = mleg_adverse.where(
                    _execution_quality_mask(
                        mleg_slippage_rows, mleg_slippage_rows.index
                    )
                )
                mleg_slippage_rows["filled_qty_num"] = pd.to_numeric(
                    mleg_slippage_rows["filled_qty"], errors="coerce"
                ).fillna(0.0)
                mask = mleg_slippage_rows["slippage_adverse_bps"].notna()
                slippage_numer += float(
                    (
                        mleg_slippage_rows["slippage_adverse_bps"]
                        * mleg_slippage_rows["filled_qty_num"]
                    )
                    .where(mask)
                    .sum()
                )
                slippage_denom += float(
                    mleg_slippage_rows["filled_qty_num"].where(mask).sum()
                )

        wins = sum(1 for p in pnls if p > 0)
        trade_count = len(pnls)
        total_pnl = sum(pnls)
        win_rate = wins / trade_count if trade_count > 0 else 0.0
        # NaN, not 0.0, when nothing qualified. A zero denominator means
        # "no execution-quality measurement exists for this strategy",
        # which is a different statement from "execution was perfect" —
        # and 0.0 bps reads as the latter, i.e. a positive signal, which
        # is the worst possible way to render an absence.
        #
        # This is the same no-silent-zero-defaults rule already enforced
        # on the numerator by the `.notna()` mask below, and in
        # `reporting/pnl.py::_adverse_bps` ("rows with NULL slippage are
        # skipped — not treated as zero"). The zero-denominator branch
        # was the one place it hadn't been applied.
        #
        # It became load-bearing when the execution-quality family filter
        # landed: strategies whose fills are all LIMIT entries and stop
        # exits (spy_options_reversion) or whose exits are all stop-gap /
        # prior-close benchmarks (donchian_breakout, sma_crossover) now
        # legitimately have zero qualifying rows. Before the filter they
        # had non-zero — if wrong — numbers, so the absence never showed.
        avg_slip = (
            slippage_numer / slippage_denom
            if slippage_denom > 0
            else float("nan")
        )

        # PLAN 11.49 — stop-gap erosion, in dollars. How far past its
        # trigger a protective stop actually filled, i.e. loss taken
        # beyond the planned risk. Deliberately a SEPARATE column from
        # the slippage figure above: different metric family, and a large
        # value here means the market gapped through the level, not that
        # execution was poor. Merging the two is the defect PR #84
        # removed; showing them adjacent without conflating them is the
        # point.
        #
        # NaN (not 0.0) when the strategy has no stop fills, for the same
        # reason as the slippage column: credit_spread is structurally
        # exempt (defined-risk, places no stops), and a zero there would
        # read as "stops cost nothing" rather than "there are no stops".
        stop_gap_dollars = _stop_gap_dollars(group)

        results.append({
            "strategy": strategy,
            "trades": trade_count,
            "wins": wins,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_adverse_slippage_bps": avg_slip,
            "stop_gap_dollars": stop_gap_dollars,
        })

    return pd.DataFrame(results)


def compute_sleeve_usage(
    state: dict,
    *,
    equity: float,
    allocations: dict[str, dict],
    total_gross_pct: float,
) -> pd.DataFrame:
    """Return allocator usage from snapshot, falling back to position math."""
    allocator = state.get("allocator") or {}
    if allocator:
        rows = []
        for strategy_name, detail in allocator.items():
            effective_budget = float(detail.get("effective_budget", 0.0) or 0.0)
            used_notional = float(detail.get("used", 0.0) or 0.0)
            rows.append({
                "Strategy": strategy_name,
                "Target Budget": float(detail.get("target_budget", 0.0) or 0.0),
                "Effective Budget": effective_budget,
                "Stretch Headroom": float(detail.get("borrowed_budget", 0.0) or 0.0),
                "Used Notional": used_notional,
                "Remaining": float(detail.get("available", 0.0) or 0.0),
                "Utilization": (
                    used_notional / effective_budget if effective_budget > 0 else 0.0
                ),
                "Open Positions": int(detail.get("positions_open", 0) or 0),
                "Hard Max Positions": int(detail.get("hard_max_positions", 0) or 0),
                "Max Position Notional": float(
                    detail.get("max_position_notional", 0.0) or 0.0
                ),
            })
        return pd.DataFrame(rows)

    positions_detail = state.get("positions_detail") or {}
    rows = []

    for strategy_name, cfg in allocations.items():
        target_pct = float(cfg.get("target_pct", 0.0) or 0.0)
        budget = equity * total_gross_pct * target_pct
        open_positions = [
            detail for detail in positions_detail.values()
            if detail.get("strategy") == strategy_name
        ]
        used_notional = 0.0
        for detail in open_positions:
            market_value = detail.get("market_value")
            qty = detail.get("qty")
            entry = detail.get("avg_entry_price")
            if market_value is not None:
                used_notional += abs(float(market_value))
            elif qty is not None and entry is not None:
                used_notional += abs(float(qty) * float(entry))
        remaining = max(0.0, budget - used_notional)
        utilization = (used_notional / budget) if budget > 0 else 0.0
        rows.append({
            "Strategy": strategy_name,
            "Target Budget": budget,
            "Effective Budget": budget,
            "Stretch Headroom": 0.0,
            "Used Notional": used_notional,
            "Remaining": remaining,
            "Utilization": utilization,
            "Open Positions": len(open_positions),
            "Hard Max Positions": cfg.get("hard_max_positions", "?"),
            "Max Position Notional": budget * float(
                cfg.get("max_position_pct_of_sleeve", 0.0) or 0.0
            ),
        })

    return pd.DataFrame(rows)


def compute_sleeve_drawdown_state(state: dict) -> pd.DataFrame:
    """Return observable or entry-blocking sleeve drawdowns from the snapshot."""
    drawdown_state = (state.get("risk_controls") or {}).get("sleeve_dd_state") or {}
    rows = []
    for strategy_name, detail in drawdown_state.items():
        if not isinstance(detail, dict) or not (
            detail.get("observed_in_drawdown") or detail.get("in_drawdown")
        ):
            continue
        rows.append({
            "Strategy": strategy_name,
            "Status": "Blocking" if detail.get("in_drawdown") else "Observed",
            "Drawdown": float(detail.get("drawdown_dollars", 0.0) or 0.0),
            "Threshold": 100.0 * float(
                detail.get("observed_threshold_pct", 0.0) or 0.0
            ),
            "Trades": int(detail.get("trade_count", 0) or 0),
            "Gate Armed": bool(detail.get("gate_armed")),
        })
    return pd.DataFrame(rows)


# ── Dashboard layout ─────────────────────────────────────────────────────────


def inject_styles() -> None:
    """Inject a small visual system on top of the default Streamlit theme."""
    st.markdown(
        """
        <style>
        .stApp {
            background:
                radial-gradient(circle at top right, rgba(0, 176, 155, 0.12), transparent 28%),
                radial-gradient(circle at top left, rgba(255, 75, 75, 0.10), transparent 24%),
                linear-gradient(180deg, #0f1318 0%, #11161d 100%);
        }

        .block-container {
            padding-top: 4.25rem;
            padding-bottom: 2.25rem;
            max-width: 1400px;
        }

        div[data-testid="metric-container"] {
            background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.025));
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            padding: 0.85rem 1rem;
            box-shadow: 0 18px 45px rgba(0,0,0,0.18);
        }

        div[data-testid="metric-container"] label {
            letter-spacing: 0.04em;
        }

        div[data-testid="stHorizontalBlock"] div[data-testid="metric-container"] p {
            font-variant-numeric: tabular-nums;
        }

        .dashboard-title {
            margin: 0 0 0.4rem 0;
            font-size: 2rem;
            font-weight: 700;
            letter-spacing: -0.03em;
        }

        .dashboard-subtitle {
            color: rgba(255,255,255,0.70);
            margin-bottom: 1.1rem;
        }

        .section-kicker {
            color: #88d6cb;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            font-size: 0.72rem;
            font-weight: 700;
            margin-bottom: 0.2rem;
        }

        .section-title {
            font-size: 1.15rem;
            font-weight: 650;
            margin-bottom: 0.2rem;
        }

        .section-note {
            color: rgba(255,255,255,0.64);
            font-size: 0.93rem;
            margin-bottom: 0.8rem;
        }

        div[data-testid="stDataFrame"] {
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 14px 36px rgba(0,0,0,0.12);
        }

        div[data-baseweb="tab-list"] {
            gap: 0.35rem;
        }

        button[data-baseweb="tab"] {
            border-radius: 999px;
            border: 1px solid rgba(255,255,255,0.08);
            background: rgba(255,255,255,0.04);
            padding: 0.35rem 0.95rem;
        }

        button[data-baseweb="tab"][aria-selected="true"] {
            background: linear-gradient(90deg, rgba(0,176,155,0.22), rgba(0,176,155,0.10));
            border-color: rgba(0,176,155,0.35);
        }

        hr {
            border-color: rgba(255,255,255,0.07);
            margin-top: 1.15rem;
            margin-bottom: 1.15rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_section_header(title: str, note: str, *, kicker: str) -> None:
    """Render a compact section heading with a visual hierarchy."""
    st.markdown(
        f"""
        <div class="section-kicker">{kicker}</div>
        <div class="section-title">{title}</div>
        <div class="section-note">{note}</div>
        """,
        unsafe_allow_html=True,
    )


def symbol_url(symbol: str) -> str:
    """Return the default external chart page for a ticker."""
    return f"https://finance.yahoo.com/quote/{symbol}/"


def format_delta_currency(value: float | None) -> str | None:
    """Format a metric delta so Streamlit can infer sign color correctly."""
    if value is None:
        return None
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _regime_color(regime: str | None) -> str:
    colors = {
        "TRENDING": "🟢",
        "RANGING": "🟡",
        "VOLATILE": "🟠",
        "BEAR": "🔴",
    }
    key = (regime or "").upper()
    return colors.get(key, "⚪")


def _phase_badge(phase: Phase) -> str:
    return {
        Phase.IN: "🟢 IN",
        Phase.OUT: "🔴 OUT",
        Phase.UNKNOWN: "⚪ UNKNOWN",
    }[phase]


def _streak_label(state: TrendMonitorState) -> str:
    """
    Human-readable trailing streak, e.g. '2 sessions below'.

    Both counters are zero only when the latest close landed exactly on the
    SMA, which is neither side. Falling through to the above-branch there
    would print '0 sessions above' and assert a run that does not exist.
    """
    if state.phase is Phase.UNKNOWN:
        return "—"
    if state.below_streak:
        n = state.below_streak
        return f"{n} session{'s' if n != 1 else ''} below"
    if state.above_streak:
        n = state.above_streak
        return f"{n} session{'s' if n != 1 else ''} above"
    return "on the SMA"


def _phase_since_label(state: TrendMonitorState) -> str:
    """
    Date of the confirmed signal, or the window start marked as a lower bound.

    A seeded phase — one already held when the data begins — has no confirmed
    start date. Rendering its lower bound as "Phase Since" would present the
    edge of the dataset as a signal that never fired, so the seeded case is
    prefixed with '≥'. The bound is the first session on which the phase
    could be determined, which is not necessarily where the data starts.
    """
    if state.phase_since is not None:
        return state.phase_since.isoformat()
    if state.phase_is_seeded and state.observed_from is not None:
        return f"≥ {state.observed_from.isoformat()}"
    return "—"


def _days_in_phase_label(state: TrendMonitorState) -> str:
    """Age of the current phase, '≥'-prefixed when it predates the window."""
    if state.days_since_phase_change is not None:
        return str(state.days_since_phase_change)
    if state.phase_is_seeded and state.days_observed is not None:
        return f"≥ {state.days_observed}"
    return "—"


def _next_signal_label(state: TrendMonitorState) -> str:
    """
    How close the noise filter is to flipping the phase.

    '—' means the current streak runs with the phase already held, so no
    signal is pending. A count of 0 cannot appear: the replay flips the
    phase on the session the requirement is met.
    """
    remaining = state.sessions_to_signal
    if remaining is None:
        return "—"
    target = "OUT" if state.phase is Phase.IN else "IN"
    return f"{target} in {remaining} session{'s' if remaining != 1 else ''}"


@st.cache_data(ttl=900, show_spinner=False)
def load_leveraged_trend_states() -> list[TrendMonitorState]:
    """
    Phase state for every configured underlying.

    Cached for 15 minutes: the underlying signal is a daily close, and the
    monitor deliberately ignores the in-progress bar, so a shorter TTL would
    re-fetch bars that cannot have changed.
    """
    return load_all_monitor_states()


def _render_leveraged_trend_panel() -> None:
    """
    Render the read-only 200-day SMA phase view used by leveraged trend.

    The panel does not place orders; the paper engine independently evaluates
    the same configured rule through ``strategies/leveraged_trend.py``. Each
    underlying remains an independent state machine.
    """
    try:
        states = load_leveraged_trend_states()
    except Exception as exc:  # noqa: BLE001 — never break the page on a monitor
        st.warning(f"Leveraged trend monitor unavailable: {exc}")
        return

    if not states:
        st.info("No underlyings configured for the leveraged trend monitor.")
        return

    exit_days = states[0].exit_days
    entry_days = states[0].entry_days
    render_section_header(
        "Leveraged Fund Trend Monitor",
        f"Daily close vs {states[0].sma_length_label} on the unleveraged "
        f"underlying. Phase-OUT after {exit_days} consecutive closes below; "
        f"phase-IN after {entry_days} consecutive closes above. The panel is "
        "read-only; when the paper strategy switch is enabled, the engine "
        "reconciles its four configured execution funds to these confirmed "
        "position targets through the normal risk and lifecycle gates.",
        kicker="Context",
    )

    rows = []
    for s in states:
        rows.append({
            "Underlying": s.underlying,
            "Fund": s.leveraged or "—",
            "Close": s.close,
            "SMA 200": s.sma,
            "Dist %": s.dist_pct,
            "Phase": _phase_badge(s.phase),
            "Streak": _streak_label(s),
            "Next Signal": _next_signal_label(s),
            "Phase Since": _phase_since_label(s),
            "Days In Phase": _days_in_phase_label(s),
            "Last Cross": (
                s.last_cross_date.isoformat() if s.last_cross_date else "—"
            ),
            "Days Since Cross": s.days_since_cross,
        })

    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "Close": st.column_config.NumberColumn(format="$%.2f"),
            "SMA 200": st.column_config.NumberColumn(format="$%.2f"),
            "Dist %": st.column_config.NumberColumn(
                format="%.2f%%",
                help="Distance of the latest close above (+) or below (-) "
                     "its 200-day SMA.",
            ),
            "Streak": st.column_config.TextColumn(
                help="Trailing consecutive completed sessions on the current "
                     "side of the SMA. Resets to zero on any close that "
                     "crosses back.",
            ),
            "Next Signal": st.column_config.TextColumn(
                help="Sessions remaining before the noise filter would flip "
                     "the phase. '—' means nothing is pending.",
            ),
            "Phase Since": st.column_config.TextColumn(
                help="Date of the confirmed signal. A '≥' prefix is not a "
                     "signal date: it is the earliest session on which the "
                     "phase could be determined at all, because the phase "
                     "was already held when the data begins. Its true start "
                     "is unknown and no later than this.",
            ),
            "Days In Phase": st.column_config.TextColumn(
                help="Calendar days since the confirmed signal. A '≥' prefix "
                     "means no signal fired inside the loaded window, so the "
                     "phase has held at least this long and probably longer.",
            ),
            "Last Cross": st.column_config.TextColumn(
                help="Last session the close crossed the SMA line in either "
                     "direction — the raw crossing, before the noise filter.",
            ),
            "Days Since Cross": st.column_config.NumberColumn(format="%d"),
        },
    )

    errored = [s for s in states if s.error]
    if errored:
        for s in errored:
            st.caption(f"⚠️ {s.underlying}: {s.error}")

    dated = [s for s in states if s.last_bar_date is not None]
    if dated:
        last_bar = max(s.last_bar_date for s in dated)
        st.caption(
            f"Last completed session: {last_bar.isoformat()} "
            f"(SIP consolidated closes; the in-progress session is excluded "
            f"until {settings.LEVERAGED_TREND_SESSION_COMPLETE_ET} ET)."
        )

    with st.expander("Phase transition history"):
        history_rows = []
        for s in states:
            for t in s.transitions:
                history_rows.append({
                    "Date": t.transition_date.isoformat(),
                    "Underlying": s.underlying,
                    "Fund": s.leveraged or "—",
                    "Signal": _phase_badge(t.phase),
                    "Close": t.close,
                    "SMA 200": t.sma,
                })
        if not history_rows:
            st.caption("No confirmed phase changes in the loaded window.")
        else:
            st.dataframe(
                pd.DataFrame(history_rows).sort_values(
                    "Date", ascending=False,
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Close": st.column_config.NumberColumn(format="$%.2f"),
                    "SMA 200": st.column_config.NumberColumn(format="$%.2f"),
                },
            )
        st.caption(
            "Confirmed signals only — the session each streak requirement "
            "was met, not the raw SMA crossing that preceded it."
        )


def _sector_class_badge(classification: str | None) -> str:
    labels = {
        "hot": "🔥 HOT",
        "neutral": "➖ NEUTRAL",
        "cold": "🧊 COLD",
    }
    return labels.get((classification or "").lower(), "⚪ UNKNOWN")


# ── Strategy Health & Edge panel (PLAN 11.10g) ──────────────────────────


def _render_health_and_edge_panel() -> None:
    """Render the Strategy Health & Edge Monitor section.

    Operates on three data sources, all of which may legitimately be
    missing on a fresh install (gracefully degrades to an empty-state
    info message):
      1. The latest weekly report markdown in data/health_reports/
      2. The lifecycle_counters SQLite table (post-11.10f)
      3. data/health_state.json — the 3-week persistence file

    Per design §13: the silent-killer alarm gets a prominent banner
    when any strategy reaches the shared negative-persistence threshold in the
    state file. The full report content is rendered in an expander
    below so the operator can drill in without scroll-burying the
    summary.
    """
    render_section_header(
        "Strategy Health & Edge",
        "Latest weekly Edge + Health assessment per strategy.",
        kicker="Health Monitor",
    )

    health_state = _load_health_state()
    latest_weekly = _find_latest_report("weekly_*.md")
    latest_monthly = _find_latest_report("monthly_*.md")

    # ── Silent-killer banner ────────────────────────────────────────
    killers = [
        (strategy_name, state)
        for strategy_name, state in (health_state or {}).items()
        if isinstance(state, dict)
        and state.get("negative_weeks", 0)
        >= NEGATIVE_PERSISTENCE_WEEKS_REQUIRED
    ]
    if killers:
        st.error(
            "🚨 **SILENT-KILLER ALARM** — the following strategies have "
            "clean execution but are losing money. Per design §13, this "
            "is the case the monitor exists to catch loudly. **Operator "
            "action: pause and investigate.**"
        )
        for strategy_name, state in killers:
            st.markdown(
                f"- **{strategy_name}** — {state.get('negative_weeks', '?')} "
                f"consecutive weeks of negative signals "
                f"(last check {state.get('last_check', '?')})"
            )

    # ── Per-strategy persistence summary ────────────────────────────
    if health_state:
        rows = []
        for strategy_name, state in sorted(health_state.items()):
            if not isinstance(state, dict):
                continue  # skip schema_version etc.
            rows.append({
                "Strategy": strategy_name,
                "Last Verdict": state.get("last_verdict", "—"),
                "Negative-signal Weeks": state.get("negative_weeks", 0),
                "Last Check": state.get("last_check", "—"),
            })
        if rows:
            persistence_df = pd.DataFrame(rows)
            st.dataframe(
                persistence_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "Negative-signal Weeks": st.column_config.NumberColumn(
                        format="%d",
                        help=(
                            "Consecutive weeks where Edge signals tripped + "
                            "sample was CONCLUSIVE. Alarm fires at 3 "
                            "(design §9)."
                        ),
                    ),
                },
            )

    # ── Latest report (tabbed: Weekly | Monthly) ────────────────────
    if latest_weekly is None and latest_monthly is None:
        if not health_state:
            st.info(
                "No health reports yet. The first weekly report will "
                "land after the next Monday cycle (or run "
                "`python scripts/strategy_health_review.py --window "
                "weekly` on demand). Monthlies land on the first of "
                "each month."
            )
    else:
        weekly_tab, monthly_tab = st.tabs(["🗓 Weekly", "📅 Monthly"])
        with weekly_tab:
            _render_report_card(
                latest_weekly,
                cadence="weekly",
                empty_hint=(
                    "No weekly report yet — runs every Monday post-cycle, "
                    "or on demand via `scripts/strategy_health_review.py "
                    "--window weekly`."
                ),
            )
        with monthly_tab:
            _render_report_card(
                latest_monthly,
                cadence="monthly",
                empty_hint=(
                    "No monthly report yet — runs on the first of each "
                    "month, or on demand via `scripts/strategy_health_review.py "
                    "--window monthly`."
                ),
            )


def _load_health_state() -> dict | None:
    """Read data/health_state.json or return None if missing/malformed.
    Streamlit refresh tolerates missing — never crashes."""
    path = (
        Path(__file__).resolve().parent / "data" / "health_state.json"
    )
    if not path.exists():
        return None
    try:
        with path.open() as fh:
            data = json.load(fh)
        # Drop the schema_version key so iteration only sees strategies.
        return {k: v for k, v in data.items() if k != "schema_version"}
    except (json.JSONDecodeError, OSError):
        return None


def _find_latest_report(glob_pattern: str) -> Path | None:
    """Return the most recently modified report matching `glob_pattern`,
    or None if the reports directory is missing or empty for that pattern.

    Patterns the dashboard cares about:
      - ``weekly_*.md`` for the weekly Strategy Health & Edge cadence
      - ``monthly_*.md`` for the monthly cadence
    """
    reports_dir = (
        Path(__file__).resolve().parent / "data" / "health_reports"
    )
    if not reports_dir.exists():
        return None
    # Backup files (e.g. weekly_*.md.bak.<utc>) also match `weekly_*.md`
    # via fnmatch — exclude them so the operator-facing card always shows
    # the canonical report, not whatever was preserved before a rewrite.
    candidates = [
        p for p in reports_dir.glob(glob_pattern)
        if ".bak." not in p.name
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


_REPORT_FRONT_MATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL,
)


def _parse_report_metadata(text: str) -> tuple[dict[str, str], str]:
    """Pull the YAML front-matter block out of a report.

    Returns ``(metadata, body_without_front_matter)``. The dashboard
    strips the front matter from the rendered body so the report doesn't
    open with a wall of `---` / key-value lines; the meaningful fields
    (period, generated_at) move into a compact caption above the body.
    """
    match = _REPORT_FRONT_MATTER_RE.match(text)
    if match is None:
        return {}, text
    raw = match.group(1)
    metadata: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip()
    body = text[match.end():]
    return metadata, body


def _format_generated_at(value: str) -> str:
    """Render an ISO8601 timestamp as a compact local-time string for the caption.

    Falls back to the raw value if parsing fails — the report's
    front-matter is normally well-formed but we don't want a malformed
    field to break the panel.
    """
    if not value:
        return "—"
    return format_local_timestamp(value)


def _render_report_card(
    report_path: Path | None, *, cadence: str, empty_hint: str,
) -> None:
    """Render one cadence's latest report inside a tab.

    Layout: a small metadata caption (period · generated · filename) on
    top, then a collapsed expander with the report body. Mirrors the
    Streamlit "card" pattern without an extra container border so it
    sits cleanly next to the other tab.
    """
    if report_path is None:
        st.info(empty_hint)
        return
    try:
        text = report_path.read_text()
    except OSError as exc:
        st.warning(f"could not read {report_path.name}: {exc}")
        return
    metadata, body = _parse_report_metadata(text)
    period_start = metadata.get("period_start", "—")
    period_end = metadata.get("period_end", "—")
    generated = _format_generated_at(metadata.get("generated_at", ""))
    # Compact one-line caption: period · generated · filename. Streamlit's
    # caption colour is muted so the report body remains visually primary.
    st.caption(
        f"**Period:** {period_start} → {period_end} &nbsp;·&nbsp; "
        f"**Generated:** {generated} &nbsp;·&nbsp; "
        f"**File:** `{report_path.name}`"
    )
    with st.expander(f"Show full {cadence} report", expanded=False):
        st.markdown(body)


@dataclass(frozen=True)
class DashboardContext:
    """Normalized, read-only inputs shared by dashboard pages."""

    state: dict[str, Any]
    display_state: dict[str, Any]
    trades_df: pd.DataFrame
    live_trading: bool
    db_path: str
    account_metrics: dict[str, Any]
    account_warning: str | None
    trades_load_error: str | None

    @property
    def equity(self) -> float:
        return float(self.account_metrics["equity"])

    @property
    def regime(self) -> str | None:
        return self.state.get("regime")

    @property
    def is_running(self) -> bool:
        return bool(self.state.get("running", False))


def _load_dashboard_context() -> DashboardContext:
    """Load the current snapshot and enrich it with read-only broker state."""
    state = load_engine_state(settings.STATE_SNAPSHOT_PATH)
    live_trading = bool(state.get("live_trading", settings.LIVE_TRADING))
    db_path = (
        settings.TRADE_LOG_DB_LIVE
        if live_trading
        else settings.TRADE_LOG_DB_PAPER
    )
    trades_df = load_trades(db_path)
    lifecycle_identities = load_open_lifecycle_identities(db_path)
    broker_metrics = load_broker_account_metrics(
        live_trading=live_trading,
        session_start_equity=state.get("session_start_equity"),
    )
    account_metrics, account_warning = resolve_account_metrics(
        state,
        broker_metrics=broker_metrics,
    )

    display_state = dict(state)
    broker_positions_detail = (
        broker_metrics.get("positions_detail")
        if broker_metrics and not broker_metrics.get("error")
        else None
    )
    if broker_positions_detail:
        display_state["positions_detail"] = merge_display_positions_detail(
            state,
            broker_positions_detail,
        )
    display_state["positions_detail"] = attach_open_position_lifecycle_ids(
        display_state.get("positions_detail") or {},
        display_state.get("open_positions") or {},
        lifecycle_identities,
    )
    display_state["multi_leg_positions"] = refresh_multi_leg_positions(
        state,
        broker_positions_detail=broker_positions_detail,
    )
    return DashboardContext(
        state=state,
        display_state=display_state,
        trades_df=trades_df,
        live_trading=live_trading,
        db_path=str(db_path),
        account_metrics=account_metrics,
        account_warning=account_warning,
        trades_load_error=trades_df.attrs.get("load_error"),
    )


def _render_page_title(title: str, subtitle: str) -> None:
    st.markdown(
        f"""
        <div class="dashboard-title">{title}</div>
        <div class="dashboard-subtitle">{subtitle}</div>
        """,
        unsafe_allow_html=True,
    )


def _snapshot_age_seconds(state: dict[str, Any]) -> float | None:
    value = state.get("timestamp")
    if not value:
        return None
    try:
        observed = datetime.fromisoformat(str(value))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError):
        return None


def _render_status_strip(ctx: DashboardContext) -> None:
    env_label = "🔴 LIVE" if ctx.live_trading else "📄 PAPER"
    status_label = "🟢 Running" if ctx.is_running else "⚫ Offline"
    metrics = ctx.account_metrics
    daily_pnl = float(metrics["daily_pnl"])
    columns = st.columns(6)
    columns[0].metric("Status", status_label)
    columns[1].metric("Mode", env_label)
    columns[2].metric(
        "Equity",
        f"${ctx.equity:,.2f}",
        delta=format_delta_currency(metrics["equity_delta"]),
    )
    columns[3].metric(
        "Daily P&L",
        f"${daily_pnl:+,.2f}",
        delta=format_delta_currency(daily_pnl) if daily_pnl else None,
        delta_color="normal",
    )
    columns[4].metric(
        "Regime",
        f"{_regime_color(ctx.regime)} {ctx.regime or '—'}",
    )
    columns[5].metric("Cycles", int(ctx.state.get("cycle_count", 0)))

    timestamp = ctx.state.get("timestamp")
    age = _snapshot_age_seconds(ctx.state)
    if timestamp:
        age_text = f" ({age:.0f}s ago)" if age is not None else ""
        st.caption(
            f"Last engine cycle: {format_local_timestamp(timestamp)}{age_text} · "
            "Account source: "
            + (
                "broker refresh"
                if metrics.get("source") == "broker"
                else "engine snapshot fallback"
            )
        )
    elif not ctx.is_running:
        st.info("Engine is offline. Historical trade data remains available.")

    if metrics.get("previous_close_equity") is None:
        st.caption(
            "Daily P&L fallback: previous close unavailable; showing session change."
        )
    if ctx.account_warning:
        st.caption(ctx.account_warning)


def _open_position_rows(ctx: DashboardContext) -> list[dict[str, Any]]:
    positions_detail = ctx.display_state.get("positions_detail") or {}
    open_positions = ctx.display_state.get("open_positions") or {}
    rows: list[dict[str, Any]] = []
    for symbol, strategy in open_positions.items():
        if strategy == "credit_spread":
            continue
        detail = positions_detail.get(symbol, {})
        entry = detail.get("avg_entry_price")
        qty = detail.get("qty")
        cost_basis = detail.get("cost_basis")
        if cost_basis is None and qty is not None and entry is not None:
            cost_basis = float(qty) * float(entry)
        unrealized_pnl = detail.get("unrealized_pnl")
        unrealized_pct = (
            float(unrealized_pnl) / float(cost_basis) * 100.0
            if unrealized_pnl is not None and cost_basis not in (None, 0)
            else None
        )
        rows.append({
            "Symbol": symbol_url(symbol),
            "Strategy": strategy,
            "Lifecycle ID": detail.get("position_uid") or "—",
            "Qty": qty,
            "Entry": entry,
            "Cost Basis": cost_basis,
            "Unrealized P&L": unrealized_pnl,
            "Unrealized %": unrealized_pct,
        })
    return rows


def _render_position_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.info("No equity or single-leg positions open.")
        return
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "Symbol": st.column_config.LinkColumn(
                "Symbol",
                display_text=r"https://finance\.yahoo\.com/quote/([^/]+)/",
            ),
            "Lifecycle ID": st.column_config.TextColumn(
                help="Durable read-only position identity used by operator tooling."
            ),
            "Qty": st.column_config.NumberColumn(format="%.2f"),
            "Entry": st.column_config.NumberColumn(format="$%.2f"),
            "Cost Basis": st.column_config.NumberColumn(format="$%.2f"),
            "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
            "Unrealized %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )


def _render_broker_curve(ctx: DashboardContext, *, compact: bool = False) -> None:
    period = st.segmented_control(
        "Broker curve window",
        options=["1W", "1M", "3M"],
        default="1M",
        key="overview_broker_curve_period" if compact else "broker_curve_period",
    )
    curve = load_broker_account_curve(ctx.live_trading, period or "1M")
    error = curve.attrs.get("load_error")
    if curve.empty:
        st.info("Broker account curve unavailable right now.")
        if error:
            st.caption(error)
        return
    start_equity = float(curve["equity"].iloc[0])
    end_equity = float(curve["equity"].iloc[-1])
    positive = end_equity >= start_equity
    line_color = "#00b09b" if positive else "#ff4b4b"
    fill_color = "rgba(0,176,155,0.15)" if positive else "rgba(255,75,75,0.15)"
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=curve["timestamp"],
        y=curve["equity"],
        mode="lines",
        line=dict(color=line_color, width=2),
        fill="tozeroy",
        fillcolor=fill_color,
        hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Equity: $%{y:,.2f}<extra></extra>",
    ))
    figure.update_layout(
        height=230 if compact else 300,
        margin=dict(l=0, r=0, t=10, b=0),
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        yaxis=dict(title="Equity ($)", gridcolor="rgba(255,255,255,0.05)"),
    )
    st.plotly_chart(figure, width="stretch")
    if error:
        st.caption(error)


def _attention_items(ctx: DashboardContext) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if not ctx.is_running:
        items.append(("warning", "Engine is offline."))
    age = _snapshot_age_seconds(ctx.state)
    stale_after = max(60.0, float(settings.ENGINE_CYCLE_INTERVAL_SECONDS) * 3.0)
    if age is None:
        items.append(("warning", "No valid engine snapshot timestamp is available."))
    elif age > stale_after:
        items.append(("error", f"Engine snapshot is stale ({age / 60:.1f} minutes old)."))
    if ctx.trades_load_error:
        items.append(("error", ctx.trades_load_error))
    if ctx.account_warning:
        items.append(("warning", ctx.account_warning))
    health_state = _load_health_state() or {}
    for strategy, state in sorted(health_state.items()):
        if (
            isinstance(state, dict)
            and int(state.get("negative_weeks", 0))
            >= NEGATIVE_PERSISTENCE_WEEKS_REQUIRED
        ):
            items.append((
                "error",
                f"{strategy}: {state.get('negative_weeks')} consecutive negative-signal weeks.",
            ))
    return items


def _render_attention(ctx: DashboardContext) -> None:
    render_section_header(
        "Needs Attention",
        "Operational and evidence conditions that may require operator review.",
        kicker="Now",
    )
    items = _attention_items(ctx)
    if not items:
        st.success("No dashboard-visible critical conditions.")
        return
    for level, message in items:
        if level == "error":
            st.error(message)
        else:
            st.warning(message)


def _render_recent_trades(ctx: DashboardContext, *, limit: int = 20) -> None:
    if ctx.trades_df.empty:
        st.info("No trades in the selected database yet.")
        return
    recent_rows = ctx.trades_df.tail(limit).copy()
    recent_rows["Measurement Note"] = recent_rows.apply(
        slippage_measurement_note,
        axis=1,
    )
    recent_rows["P&L"] = recent_rows.apply(recent_trade_realized_pnl, axis=1)
    columns = [
        "timestamp", "symbol", "side", "qty", "avg_fill_price", "P&L",
        "strategy", "reason", "slippage_signed_bps",
        "slippage_benchmark_kind", "slippage_measurement_quality",
        "Measurement Note",
    ]
    recent = recent_rows[[c for c in columns if c in recent_rows.columns]].copy()
    if "timestamp" in recent:
        recent["timestamp"] = recent["timestamp"].map(format_local_timestamp)
    if "avg_fill_price" in recent:
        recent["avg_fill_price"] = recent["avg_fill_price"].map(
            lambda value: f"${value:,.2f}" if pd.notna(value) else "—"
        )
    if "P&L" in recent:
        recent["P&L"] = recent["P&L"].map(
            lambda value: f"${value:+,.2f}" if pd.notna(value) else "—"
        )
    if "slippage_signed_bps" in recent:
        recent["slippage_signed_bps"] = recent["slippage_signed_bps"].map(
            lambda value: f"{value:,.1f}" if pd.notna(value) else "—"
        )
    for column in ("slippage_benchmark_kind", "slippage_measurement_quality"):
        if column in recent:
            recent[column] = recent[column].map(
                lambda value: value if value is not None and pd.notna(value) else "—"
            )
    recent = recent.rename(columns={
        "timestamp": "Timestamp",
        "symbol": "Symbol",
        "side": "Side",
        "qty": "Qty",
        "avg_fill_price": "Avg Fill Price",
        "strategy": "Strategy",
        "reason": "Reason",
        "slippage_signed_bps": "Slippage Bps",
        "slippage_benchmark_kind": "Benchmark Kind",
        "slippage_measurement_quality": "Quality",
    })
    if "Symbol" in recent:
        recent["Symbol"] = recent["Symbol"].map(symbol_url)
    st.dataframe(
        recent[::-1],
        width="stretch",
        hide_index=True,
        column_config={
            "Symbol": st.column_config.LinkColumn(
                "Symbol",
                display_text=r"https://finance\.yahoo\.com/quote/([^/]+)/",
            ),
        },
    )


def _summarize_unrealized_pnl(
    single_leg_rows: list[dict[str, Any]],
    multi_leg_rows: list[dict[str, Any]],
) -> tuple[float | None, int]:
    """Return the known unrealized total and count of unavailable marks."""
    values = [row.get("Unrealized P&L") for row in single_leg_rows]
    values.extend(row.get("Net Spread P&L") for row in multi_leg_rows)
    known = [float(value) for value in values if value is not None and pd.notna(value)]
    missing = len(values) - len(known)
    if not values:
        return 0.0, 0
    return (sum(known) if known else None), missing


@st.fragment(run_every="30s")
def _render_overview_page() -> None:
    ctx = _load_dashboard_context()
    _render_page_title(
        "Trading Bot Overview",
        "Immediate operational state, exposure, market context, and items requiring attention.",
    )
    _render_status_strip(ctx)
    st.divider()
    left, right = st.columns([1, 1])
    with left:
        _render_attention(ctx)
    with right:
        render_section_header(
            "Current Exposure",
            "Open-position count and current unrealized result.",
            kicker="Now",
        )
        position_rows = _open_position_rows(ctx)
        multi_leg_rows = multi_leg_display_rows(
            ctx.display_state.get("multi_leg_positions") or []
        )
        unrealized, missing_marks = _summarize_unrealized_pnl(
            position_rows,
            multi_leg_rows,
        )
        c1, c2, c3 = st.columns(3)
        c1.metric("Single-leg", len(position_rows))
        c2.metric("Multi-leg", len(multi_leg_rows))
        c3.metric(
            "Unrealized P&L",
            "—" if unrealized is None else f"${unrealized:+,.2f}",
        )
        if missing_marks:
            st.caption(
                f"Unrealized total is partial: {missing_marks} open position "
                f"mark{'s are' if missing_marks != 1 else ' is'} unavailable."
            )
        sector_counts = {
            key: len(value) if isinstance(value, list) else int(value)
            for key, value in (ctx.state.get("sector_exposure") or {}).items()
        }
        if sector_counts:
            largest_sector, count = max(sector_counts.items(), key=lambda item: item[1])
            st.caption(
                f"Largest held sector: {largest_sector.replace('_', ' ').title()} "
                f"({count} position{'s' if count != 1 else ''})."
            )

    st.divider()
    curve_col, positions_col = st.columns([3, 2])
    with curve_col:
        render_section_header(
            "Account Equity",
            "Broker-reported equity including unrealized P&L.",
            kicker="Performance",
        )
        _render_broker_curve(ctx, compact=True)
    with positions_col:
        render_section_header(
            "Largest Open Positions",
            "Highest absolute unrealized P&L among current single-leg positions.",
            kicker="Exposure",
        )
        rows = sorted(
            _open_position_rows(ctx),
            key=lambda row: abs(float(row["Unrealized P&L"] or 0.0)),
            reverse=True,
        )[:5]
        _render_position_table(rows)

    st.divider()
    market_col, activity_col = st.columns(2)
    with market_col:
        render_section_header(
            "Market Context",
            "Existing observational state; detailed tools live in Market Observatory.",
            kicker="Observe",
        )
        sector_heat = ctx.state.get("sector_heat") or {}
        if not sector_heat:
            st.info("Sector-heat snapshot unavailable.")
        else:
            counts = sector_heat.get("counts") or {}
            c1, c2, c3 = st.columns(3)
            c1.metric("Hot", int(counts.get("hot", 0)))
            c2.metric("Neutral", int(counts.get("neutral", 0)))
            c3.metric("Cold", int(counts.get("cold", 0)))
        st.caption(
            "Pre-open SPY/QQQ/XLK/SOXX observation is intentionally not active yet; "
            "the evidence contract will be implemented separately."
        )
    with activity_col:
        render_section_header(
            "Latest Activity",
            "Five most recent trade-log records.",
            kicker="Audit Trail",
        )
        _render_recent_trades(ctx, limit=5)


@st.fragment(run_every="30s")
def _render_positions_page() -> None:
    ctx = _load_dashboard_context()
    _render_page_title(
        "Positions & Risk",
        "Current economic exposure, sleeve capacity, capital pools, and concentration.",
    )
    _render_status_strip(ctx)
    if ctx.trades_load_error:
        st.error(ctx.trades_load_error)
    st.divider()
    pos_col, sleeve_col = st.columns(2)
    with pos_col:
        render_section_header(
            "Open Positions",
            "Current equity and single-leg positions from the latest snapshot.",
            kicker="Exposure",
        )
        _render_position_table(_open_position_rows(ctx))
    with sleeve_col:
        render_section_header(
            "Sleeve Usage",
            "Configured budgets against current and pending notional.",
            kicker="Risk",
        )
        sleeve_df = compute_sleeve_usage(
            ctx.display_state,
            equity=ctx.equity,
            allocations=settings.STRATEGY_ALLOCATIONS,
            total_gross_pct=settings.MAX_GROSS_EXPOSURE_PCT,
        )
        if sleeve_df.empty:
            st.info("Sleeve allocation snapshot unavailable.")
        else:
            display = sleeve_df.copy()
            display["Utilization"] *= 100.0
            display["Open Positions"] = display.apply(
                lambda row: f"{row['Open Positions']}/{row['Hard Max Positions']}",
                axis=1,
            )
            display = display.drop(columns=["Hard Max Positions"])
            st.dataframe(
                display,
                width="stretch",
                hide_index=True,
                column_config={
                    "Target Budget": st.column_config.NumberColumn(format="$%.2f"),
                    "Effective Budget": st.column_config.NumberColumn(format="$%.2f"),
                    "Stretch Headroom": st.column_config.NumberColumn(format="$%.2f"),
                    "Used Notional": st.column_config.NumberColumn(format="$%.2f"),
                    "Remaining": st.column_config.NumberColumn(format="$%.2f"),
                    "Max Position Notional": st.column_config.NumberColumn(format="$%.2f"),
                    "Utilization": st.column_config.ProgressColumn(
                        format="%.0f%%", min_value=0.0, max_value=100.0
                    ),
                },
            )

    multi_leg_positions = ctx.display_state.get("multi_leg_positions") or []
    if multi_leg_positions:
        st.divider()
        render_section_header(
            "Open Multi-Leg Positions",
            "Primary economic view for option structures, keyed by durable position identity.",
            kicker="Options",
        )
        st.dataframe(
            pd.DataFrame(multi_leg_display_rows(multi_leg_positions)),
            width="stretch",
            hide_index=True,
            column_config={
                "DTE": st.column_config.NumberColumn(format="%d"),
                "Entry Credit": st.column_config.NumberColumn(format="$%.2f"),
                "Mark Debit": st.column_config.NumberColumn(format="$%.2f"),
                "Net Spread P&L": st.column_config.NumberColumn(format="$%.2f"),
                "Short Leg P&L": st.column_config.NumberColumn(format="$%.2f"),
                "Long Leg P&L": st.column_config.NumberColumn(format="$%.2f"),
                "Max Profit": st.column_config.NumberColumn(format="$%.2f"),
                "Max Loss": st.column_config.NumberColumn(format="$%.2f"),
                "Underlying Price": st.column_config.NumberColumn(format="$%.2f"),
                "Distance": st.column_config.NumberColumn(format="$%.2f"),
                "Distance %": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )

    st.divider()
    drawdown_col, pool_col = st.columns(2)
    with drawdown_col:
        render_section_header(
            "Observed Sleeve Drawdown",
            "HWM-threshold observations, including non-blocking paper breaches.",
            kicker="Risk",
        )
        drawdown_df = compute_sleeve_drawdown_state(ctx.state)
        if drawdown_df.empty:
            st.info("No sleeve drawdown observations available.")
        else:
            st.dataframe(
                drawdown_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "Drawdown": st.column_config.NumberColumn(format="$%.2f"),
                    "Threshold": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )
    with pool_col:
        render_section_header(
            "Capital Pools",
            "Pool-level target, use, availability, and pending exposure.",
            kicker="Risk",
        )
        pool_df = pd.DataFrame.from_dict(
            ctx.display_state.get("capital_pools") or {},
            orient="index",
        )
        if pool_df.empty:
            st.info("Capital-pool snapshot unavailable.")
        else:
            pool_df = pool_df.reset_index().rename(columns={"index": "Pool"})
            pool_df["Utilization"] = pool_df["utilization"] * 100.0
            pool_df = pool_df.rename(columns={
                "target_budget": "Target Budget",
                "used": "Used",
                "available": "Available",
                "pending_entry_notional": "Pending Entry Notional",
            })
            st.dataframe(
                pool_df[[
                    "Pool", "Target Budget", "Used", "Available",
                    "Pending Entry Notional", "Utilization",
                ]],
                width="stretch",
                hide_index=True,
                column_config={
                    "Target Budget": st.column_config.NumberColumn(format="$%.2f"),
                    "Used": st.column_config.NumberColumn(format="$%.2f"),
                    "Available": st.column_config.NumberColumn(format="$%.2f"),
                    "Pending Entry Notional": st.column_config.NumberColumn(format="$%.2f"),
                    "Utilization": st.column_config.ProgressColumn(
                        format="%.0f%%", min_value=0.0, max_value=100.0
                    ),
                },
            )

    sector_exposure = ctx.state.get("sector_exposure") or {}
    if sector_exposure:
        st.divider()
        render_section_header(
            "Open Position Sector Exposure",
            "Held positions grouped by resolved sector; observability only.",
            kicker="Concentration",
        )
        rows = []
        for sector, items in sorted(
            sector_exposure.items(),
            key=lambda item: (-(len(item[1]) if isinstance(item[1], list) else int(item[1])), item[0]),
        ):
            if isinstance(items, int):
                rows.append({"Sector": sector.replace("_", " ").title(), "Positions": items, "Symbols": "", "Strategies": ""})
                continue
            rows.append({
                "Sector": sector.replace("_", " ").title(),
                "Positions": len(items),
                "Symbols": ", ".join(sorted({item["symbol"] for item in items})),
                "Strategies": ", ".join(sorted({item["strategy"] for item in items})),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_strategy_stats(ctx: DashboardContext) -> None:
    stats = compute_strategy_stats(ctx.trades_df)
    if stats.empty:
        st.info("No trades recorded yet.")
        return
    display = stats.rename(columns={
        "strategy": "Strategy",
        "trades": "Trades",
        "wins": "Wins",
        "win_rate": "Win Rate",
        "total_pnl": "Total P&L",
        "avg_adverse_slippage_bps": "Avg Adverse Slippage Bps",
        "stop_gap_dollars": "Stop-Gap $",
    }).copy()
    display["Win Rate"] *= 100.0
    st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        column_config={
            "Trades": st.column_config.NumberColumn(format="%d"),
            "Wins": st.column_config.NumberColumn(format="%d"),
            "Win Rate": st.column_config.NumberColumn(format="%.1f%%"),
            "Total P&L": st.column_config.NumberColumn(format="$%.2f"),
            "Avg Adverse Slippage Bps": st.column_config.NumberColumn(format="%.1f bps"),
            "Stop-Gap $": st.column_config.NumberColumn(format="$%.0f"),
        },
    )
    if display["Avg Adverse Slippage Bps"].isna().any():
        missing = display.loc[
            display["Avg Adverse Slippage Bps"].isna(), "Strategy"
        ].tolist()
        st.caption(
            "Blank slippage means no compatible execution-quality measurement, "
            f"not zero slippage: {', '.join(missing)}."
        )
    st.caption(
        "**Stop-Gap $** is loss beyond the planned protective-stop level when "
        "the market gaps through it; a large value is not automatically poor "
        "execution. Blank means the strategy does not place protective stops, "
        "as with defined-risk credit spreads. It is intentionally separate "
        "from execution-quality slippage."
    )


def _render_realized_performance(ctx: DashboardContext) -> None:
    curve = compute_equity_curve(ctx.trades_df)
    period = st.segmented_control(
        "Realized curve window",
        options=["1W", "1M", "All"],
        default="All",
        key="strategy_realized_curve_period",
    )
    displayed = filter_realized_curve_window(curve, period or "All")
    left, right = st.columns([2, 1])
    with left:
        render_section_header(
            "Equity Curve (Realized)",
            "Cumulative realized P&L from closed economic outcomes.",
            kicker="Performance",
        )
        if displayed.empty:
            st.info("No closed trades yet.")
        else:
            final_pnl = float(displayed["cumulative_pnl"].iloc[-1])
            positive = final_pnl >= 0
            figure = go.Figure()
            figure.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
            figure.add_trace(go.Scatter(
                x=displayed["timestamp"],
                y=displayed["cumulative_pnl"],
                mode="lines",
                line=dict(color="#00b09b" if positive else "#ff4b4b", width=2),
                fill="tozeroy",
                fillcolor="rgba(0,176,155,0.15)" if positive else "rgba(255,75,75,0.15)",
                hovertemplate="<b>%{x|%Y-%m-%d}</b><br>P&L: $%{y:,.2f}<extra></extra>",
            ))
            figure.update_layout(
                height=300,
                margin=dict(l=0, r=0, t=10, b=0),
                showlegend=False,
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
                yaxis=dict(title="P&L ($)", gridcolor="rgba(255,255,255,0.05)"),
            )
            st.plotly_chart(figure, width="stretch")
    with right:
        render_section_header(
            "Realized Diagnostics",
            "Generic diagnostics; not a strategy-graduation verdict.",
            kicker="Performance",
        )
        pnl = [event["pnl"] for event in realized_trade_events(ctx.trades_df)]
        if len(pnl) < 5:
            st.warning(f"Insufficient realized sample ({len(pnl)} outcomes).")
        else:
            from reporting.metrics import compute_metrics
            metrics = compute_metrics(pnl)
            st.metric("Sharpe (annualized)", f"{metrics.sharpe_ratio:.2f}")
            st.metric("Max Drawdown", f"{metrics.max_drawdown_pct:.1%}")
            st.metric("Profit Factor", f"{metrics.profit_factor:.2f}")
            st.metric("Win Rate", f"{metrics.win_rate:.1%}")
            st.metric("Avg W/L Ratio", f"{metrics.avg_win_loss_ratio:.2f}")


def _render_watchlists(ctx: DashboardContext) -> None:
    strategy_watchlists = settings.STRATEGY_WATCHLISTS
    if not strategy_watchlists:
        st.info("No strategy watchlists configured.")
        return
    tabs = st.tabs([name.replace("_", " ").title() for name in strategy_watchlists])
    open_positions = ctx.state.get("open_positions") or {}
    statuses = ctx.state.get("watchlist_statuses") or {}
    reasons_map = ctx.state.get("watchlist_reasons") or {}
    for tab, (strategy, symbols) in zip(tabs, strategy_watchlists.items()):
        with tab:
            allowed = settings.STRATEGY_ALLOWED_REGIMES.get(strategy)
            regime_ok = regime_gate_allows(ctx.regime, allowed)
            gate = "✅ Entries allowed" if regime_ok else (
                "🚫 Entries blocked" if regime_ok is False else "⚪ Regime unavailable"
            )
            st.caption(
                f"Regime: {_regime_color(ctx.regime)} {ctx.regime or '—'} · "
                f"Gate: {gate} · {len(symbols)} symbols"
            )
            strategy_trades = (
                ctx.trades_df[ctx.trades_df["strategy"] == strategy]
                if not ctx.trades_df.empty and "strategy" in ctx.trades_df
                else pd.DataFrame()
            )
            last_trade: dict[str, dict[str, Any]] = {}
            if not strategy_trades.empty:
                for symbol, group in strategy_trades.groupby("symbol"):
                    last = group.sort_values("timestamp").iloc[-1]
                    last_trade[str(symbol)] = {
                        "date": last["timestamp"].strftime("%Y-%m-%d") if pd.notna(last["timestamp"]) else "—",
                        "side": last.get("side", ""),
                        "price": last.get("avg_fill_price"),
                    }
            rows = []
            for symbol in symbols:
                status = watchlist_symbol_state(statuses, strategy, symbol)
                if status is None:
                    status = "Long" if open_positions.get(symbol) == strategy else "No Signal"
                reasons = watchlist_symbol_state(reasons_map, strategy, symbol) or []
                latest = last_trade.get(symbol, {})
                price = latest.get("price")
                rows.append({
                    "Symbol": symbol_url(symbol),
                    "Status": status,
                    "Reason": "; ".join(reasons) if reasons else "—",
                    "Last Trade": latest.get("date", "—"),
                    "Last Side": str(latest.get("side") or "—").upper(),
                    "Last Price": f"${float(price):,.2f}" if price else "—",
                })
            st.dataframe(
                pd.DataFrame(rows),
                width="stretch",
                hide_index=True,
                column_config={
                    "Symbol": st.column_config.LinkColumn(
                        display_text=r"https://finance\.yahoo\.com/quote/([^/]+)/"
                    )
                },
            )


@st.fragment(run_every="60s")
def _render_strategies_page() -> None:
    ctx = _load_dashboard_context()
    _render_page_title(
        "Strategies & Performance",
        "Performance diagnostics, strategy evidence, health reports, and active universes.",
    )
    _render_status_strip(ctx)
    if ctx.trades_load_error:
        st.error(ctx.trades_load_error)
    st.divider()
    render_section_header(
        "Broker Account Curve",
        "Account equity including unrealized P&L.",
        kicker="Performance",
    )
    _render_broker_curve(ctx)
    st.divider()
    _render_realized_performance(ctx)
    st.divider()
    render_section_header(
        "Strategy Realized P&L",
        "Closed-outcome summary with execution-quality and stop-gap diagnostics.",
        kicker="Strategies",
    )
    _render_strategy_stats(ctx)
    st.divider()
    _render_health_and_edge_panel()
    st.divider()
    render_section_header(
        "Active Watchlists",
        "Per-strategy universes with regime gating and latest-trade context.",
        kicker="Universe",
    )
    _render_watchlists(ctx)


def _render_sector_heat(ctx: DashboardContext) -> None:
    sector_heat = ctx.state.get("sector_heat") or {}
    if not sector_heat:
        st.info("Sector-heat snapshot unavailable.")
        return
    counts = sector_heat.get("counts") or {}
    c1, c2, c3 = st.columns(3)
    c1.metric("HOT Sectors", int(counts.get("hot", 0)))
    c2.metric("Neutral Sectors", int(counts.get("neutral", 0)))
    c3.metric("COLD Sectors", int(counts.get("cold", 0)))
    sectors = sector_heat.get("sectors") or {}
    rows = []
    for sector, detail in sectors.items():
        rows.append({
            "Sector": sector.replace("_", " ").title(),
            "ETF": detail.get("etf_ticker"),
            "Score": detail.get("score"),
            "Status": _sector_class_badge(detail.get("classification")),
            "Last Close": detail.get("last_close"),
            "> SMA200": detail.get("above_sma200"),
            "> SMA50": detail.get("above_sma50"),
            "Golden Cross": detail.get("golden_cross"),
            "Dist SMA50": (
                float(detail["dist_sma50_pct"]) * 100.0
                if detail.get("dist_sma50_pct") is not None
                else None
            ),
            "Vol Confirm": detail.get("vol_confirm"),
        })
    if rows:
        st.dataframe(
            pd.DataFrame(rows).sort_values(["Score", "Sector"], ascending=[False, True]),
            width="stretch",
            hide_index=True,
            column_config={
                "Score": st.column_config.NumberColumn(format="%.1f"),
                "Last Close": st.column_config.NumberColumn(format="$%.2f"),
                "> SMA200": st.column_config.CheckboxColumn(),
                "> SMA50": st.column_config.CheckboxColumn(),
                "Golden Cross": st.column_config.CheckboxColumn(),
                "Dist SMA50": st.column_config.NumberColumn(format="%.1f%%"),
                "Vol Confirm": st.column_config.CheckboxColumn(),
            },
        )
    if sector_heat.get("generated_at"):
        st.caption(
            "Session-stable snapshot generated: "
            f"{format_local_timestamp(sector_heat['generated_at'])}"
        )

    symbol_map = sector_heat.get("symbol_map") or {}
    unmapped = sector_heat.get("unmapped") or []
    if symbol_map or unmapped:
        render_section_header(
            "Watched Symbols by Sector",
            "Configured universes grouped by resolved sector and current heat state.",
            kicker="Coverage",
        )
    if symbol_map:
        exposure_rows = []
        for sector, items in sorted(symbol_map.items()):
            detail = (sector_heat.get("sectors") or {}).get(sector, {})
            by_symbol: dict[str, set[str]] = {}
            for item in items:
                by_symbol.setdefault(str(item["symbol"]), set()).add(
                    str(item["strategy"])
                )
            exposure_rows.append({
                "Sector": sector.replace("_", " ").title(),
                "Status": _sector_class_badge(detail.get("classification")),
                "Symbols": ", ".join(sorted(by_symbol)),
                "Strategies": ", ".join(sorted({
                    strategy
                    for strategy_set in by_symbol.values()
                    for strategy in strategy_set
                })),
            })
        st.dataframe(pd.DataFrame(exposure_rows), width="stretch", hide_index=True)
    if unmapped:
        st.caption(
            "Unmapped symbols: "
            + ", ".join(sorted({str(item["symbol"]) for item in unmapped}))
        )


@st.fragment(run_every="30s")
def _render_market_page() -> None:
    ctx = _load_dashboard_context()
    _render_page_title(
        "Market Observatory",
        "Read-only market context. Observations are not strategy permissions or order controls.",
    )
    _render_status_strip(ctx)
    st.divider()
    render_section_header(
        "Pre-Open Pulse",
        "Planned observation set: SPY, QQQ, XLK, and SOXX.",
        kicker="Research Foundation",
    )
    st.info(
        "The pre-open evidence contract and append-only observation ledger are "
        "the next workstream. No provisional value is displayed as a signal."
    )
    st.divider()
    _render_leveraged_trend_panel()
    st.divider()
    render_section_header(
        "Sector Heat",
        "Session-level sector momentum context from the engine snapshot.",
        kicker="Market Context",
    )
    _render_sector_heat(ctx)
    st.divider()
    render_section_header(
        "Earnings Clusters",
        "Parked observation-first research; no score or candidate ranking is active.",
        kicker="Research Foundation",
    )
    st.info(
        "The chronological event-ledger contract will be reviewed separately "
        "before any dashboard calculation or backfill is implemented."
    )


@st.fragment(run_every="30s")
def _render_activity_page() -> None:
    ctx = _load_dashboard_context()
    _render_page_title(
        "Activity & Execution",
        "Recent fills, realized outcomes, slippage provenance, and execution-quality context.",
    )
    _render_status_strip(ctx)
    if ctx.trades_load_error:
        st.error(ctx.trades_load_error)
    st.divider()
    render_section_header(
        "Recent Trades",
        "Most recent records from the selected paper or live trade database.",
        kicker="Audit Trail",
    )
    _render_recent_trades(ctx, limit=50)
    st.caption(
        "Slippage values retain their benchmark kind and measurement quality; "
        "blank values are unavailable rather than zero."
    )


def render_dashboard() -> None:
    """Render the multipage, read-only operator dashboard."""
    st.set_page_config(
        page_title="Trading Bot Dashboard",
        page_icon="📈",
        layout="wide",
    )
    inject_styles()
    page = st.navigation(
        [
            st.Page(
                _render_overview_page,
                title="Overview",
                icon="🏠",
                url_path="overview",
                default=True,
            ),
            st.Page(
                _render_positions_page,
                title="Positions & Risk",
                icon="🛡️",
                url_path="positions-risk",
            ),
            st.Page(
                _render_strategies_page,
                title="Strategies & Performance",
                icon="📈",
                url_path="strategies-performance",
            ),
            st.Page(
                _render_market_page,
                title="Market Observatory",
                icon="🔭",
                url_path="market-observatory",
            ),
            st.Page(
                _render_activity_page,
                title="Activity & Execution",
                icon="🧾",
                url_path="activity-execution",
            ),
        ],
        position="top",
    )
    page.run()


if __name__ == "__main__":
    render_dashboard()
