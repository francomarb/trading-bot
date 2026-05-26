"""
Read-only analytics dashboard (Phase 11.14).

Connects to trades.db and the engine state snapshot to visualize:
  - Equity curve + rolling Sharpe
  - Per-strategy performance (win rate, P&L, slippage)
  - Active positions and sleeve allocation
  - Recent trades table

Run with:
    bash start_dashboard.sh
    # or directly:
    streamlit run dashboard.py

The dashboard is read-only — it never touches the engine or broker.
It auto-refreshes every 30 seconds while the page is open.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings
from engine.positions import build_credit_spread_snapshot


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


def load_engine_state(path: str) -> dict:
    """Read engine_state.json. Returns {} if missing or malformed."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


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

    rows = _realized_pnl_events(trades_df, key_columns=("symbol",))
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


def compute_strategy_stats(trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute per-strategy summary: trades, wins, win_rate, total_pnl,
    avg_realized_slippage_bps.

    Single-leg trades are fully closed positions aggregated across exit rows
    that share strategy / symbol / entry_timestamp. Multi-leg trades are
    realized-P&L close events keyed by position_id, so spreads do not need to
    mimic single-leg buy/sell shape to show up in the dashboard.
    """
    if trades_df.empty or "strategy" not in trades_df.columns:
        return pd.DataFrame(columns=[
            "strategy", "trades", "wins", "win_rate",
            "total_pnl", "avg_slippage_bps",
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
            exits["realized_slippage_bps"] = pd.to_numeric(
                exits["realized_slippage_bps"], errors="coerce"
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
                slippage_numer=(
                    "realized_slippage_bps",
                    lambda s: float((s.fillna(0.0) * exits.loc[s.index, "filled_qty_num"]).sum()),
                ),
                slippage_denom=("filled_qty_num", "sum"),
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
            mleg_exits["realized_slippage_bps"] = pd.to_numeric(
                mleg_exits["realized_slippage_bps"], errors="coerce"
            ).fillna(0.0)
            mleg_exits["filled_qty_num"] = pd.to_numeric(
                mleg_exits["filled_qty"], errors="coerce"
            ).fillna(0.0)
            mleg_grouped = mleg_exits.groupby(
                "position_id", dropna=True, sort=False
            ).agg(
                realized_pnl=("realized_pnl", "sum"),
                slippage_numer=(
                    "realized_slippage_bps",
                    lambda s: float(
                        (s.fillna(0.0) * mleg_exits.loc[s.index, "filled_qty_num"]).sum()
                    ),
                ),
                slippage_denom=("filled_qty_num", "sum"),
            )
            mleg_grouped = mleg_grouped[mleg_grouped["realized_pnl"].notna()]
            pnls.extend(mleg_grouped["realized_pnl"].fillna(0.0).tolist())
            slippage_numer += float(mleg_grouped["slippage_numer"].sum())
            slippage_denom += float(mleg_grouped["slippage_denom"].sum())

        wins = sum(1 for p in pnls if p > 0)
        trade_count = len(pnls)
        total_pnl = sum(pnls)
        win_rate = wins / trade_count if trade_count > 0 else 0.0
        avg_slip = slippage_numer / slippage_denom if slippage_denom > 0 else 0.0

        results.append({
            "strategy": strategy,
            "trades": trade_count,
            "wins": wins,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_slippage_bps": avg_slip,
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
    when any strategy has `negative_persistence_weeks >= 3` in the
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
    latest_report = _find_latest_weekly_report()

    # ── Silent-killer banner ────────────────────────────────────────
    killers = [
        (strategy_name, state)
        for strategy_name, state in (health_state or {}).items()
        if isinstance(state, dict)
        and state.get("negative_weeks", 0) >= 3
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

    # ── Latest report (expander) ────────────────────────────────────
    if latest_report is not None:
        with st.expander(
            f"📄 Latest weekly report — `{latest_report.name}`",
            expanded=False,
        ):
            try:
                st.markdown(latest_report.read_text())
            except Exception as exc:  # noqa: BLE001
                st.warning(f"could not read report: {exc}")
    else:
        if not health_state:
            st.info(
                "No weekly health reports yet. The first report will "
                "land after the next Monday cycle (or run "
                "`python scripts/strategy_health_review.py --window "
                "weekly` on demand)."
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


def _find_latest_weekly_report() -> Path | None:
    """Return the most recently modified weekly_*.md, or None if absent."""
    reports_dir = (
        Path(__file__).resolve().parent / "data" / "health_reports"
    )
    if not reports_dir.exists():
        return None
    candidates = sorted(
        reports_dir.glob("weekly_*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def render_dashboard() -> None:
    st.set_page_config(
        page_title="Trading Bot Dashboard",
        page_icon="📈",
        layout="wide",
    )
    inject_styles()
    st.markdown(
        """
        <div class="dashboard-title">Trading Bot Dashboard</div>
        <div class="dashboard-subtitle">
            Live operational view of the paper/live engine, strategy sleeves,
            and realized trade history.
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Load data ────────────────────────────────────────────────────────
    state = load_engine_state(settings.STATE_SNAPSHOT_PATH)
    live_trading = state.get("live_trading", settings.LIVE_TRADING)
    db_path = settings.TRADE_LOG_DB_LIVE if live_trading else settings.TRADE_LOG_DB_PAPER
    trades_df = load_trades(db_path)
    trades_load_error = trades_df.attrs.get("load_error")

    if trades_load_error:
        st.error(trades_load_error)

    # ── Header row ───────────────────────────────────────────────────────
    env_label = "🔴 LIVE" if live_trading else "📄 PAPER"
    is_running = state.get("running", False)
    status_label = "🟢 Running" if is_running else "⚫ Offline"
    regime = state.get("regime")
    cycle_count = state.get("cycle_count", 0)
    ts = state.get("timestamp", "—")
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
        positions_detail = {}
        for sym, detail in broker_positions_detail.items():
            enriched = dict(detail)
            snapshot_detail = (state.get("positions_detail") or {}).get(sym, {})
            if "strategy" not in enriched and snapshot_detail.get("strategy") is not None:
                enriched["strategy"] = snapshot_detail["strategy"]
            positions_detail[sym] = enriched
        display_state["positions_detail"] = positions_detail
    display_state["multi_leg_positions"] = refresh_multi_leg_positions(
        state,
        broker_positions_detail=broker_positions_detail,
    )
    equity = account_metrics["equity"]
    daily_pnl = account_metrics["daily_pnl"]
    session_pnl = account_metrics["session_pnl"]
    previous_close = account_metrics["previous_close_equity"]

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Status", status_label)
    col2.metric("Mode", env_label)
    col3.metric("Equity", f"${equity:,.2f}",
                delta=format_delta_currency(account_metrics["equity_delta"]))
    col4.metric("Daily P&L", f"${daily_pnl:+,.2f}",
                delta=format_delta_currency(daily_pnl) if daily_pnl != 0 else None,
                delta_color="normal")
    col5.metric("Regime", f"{_regime_color(regime)} {regime or '—'}")
    col6.metric("Cycles", cycle_count)

    if ts != "—":
        try:
            last_update = datetime.fromisoformat(ts)
            age_s = (datetime.now(timezone.utc) - last_update).total_seconds()
            st.caption(f"Last engine cycle: {ts} ({age_s:.0f}s ago)")
        except Exception:
            st.caption(f"Last engine cycle: {ts}")
    elif not is_running:
        st.info("Engine is offline. Showing historical data from trade database.")
    st.caption(
        "Account metrics source: "
        + (
            "live broker refresh"
            if account_metrics["source"] == "broker"
            else "engine snapshot fallback"
        )
    )
    if previous_close is None:
        st.caption("Daily P&L fallback: previous close unavailable, showing session-based change.")
    if account_warning:
        st.caption(account_warning)

    st.divider()

    # ── Broker account curve ─────────────────────────────────────────────
    broker_curve_period = st.segmented_control(
        "Broker curve window",
        options=["1W", "1M", "3M"],
        default="1M",
        key="broker_account_curve_period",
    )
    broker_curve = load_broker_account_curve(
        live_trading,
        broker_curve_period or "1M",
    )
    broker_curve_error = broker_curve.attrs.get("load_error")
    render_section_header(
        "Broker Account Curve",
        "Broker-reported account equity, including unrealized P&L.",
        kicker="Performance",
    )
    if broker_curve.empty:
        st.info("Broker account curve unavailable right now.")
        if broker_curve_error:
            st.caption(broker_curve_error)
    else:
        start_equity = float(broker_curve["equity"].iloc[0])
        end_equity = float(broker_curve["equity"].iloc[-1])
        line_color = "#00b09b" if end_equity >= start_equity else "#ff4b4b"
        fill_color = (
            "rgba(0,176,155,0.15)"
            if end_equity >= start_equity else "rgba(255,75,75,0.15)"
        )
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=broker_curve["timestamp"],
            y=broker_curve["equity"],
            mode="lines",
            name="Account Equity",
            line=dict(color=line_color, width=2),
            fill="tozeroy",
            fillcolor=fill_color,
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>Equity: $%{y:,.2f}<extra></extra>",
        ))
        fig.update_layout(
            xaxis_title=None,
            yaxis_title="Equity ($)",
            height=300,
            margin=dict(l=0, r=0, t=10, b=0),
            showlegend=False,
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
            yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        )
        st.plotly_chart(fig, width="stretch")
        if broker_curve_error:
            st.caption(broker_curve_error)

    st.divider()

    # ── Equity curve + rolling Sharpe ────────────────────────────────────
    equity_curve = compute_equity_curve(trades_df)
    realized_curve_period = st.segmented_control(
        "Realized curve window",
        options=["1W", "1M", "All"],
        default="All",
        key="realized_curve_period",
    )
    displayed_equity_curve = filter_realized_curve_window(
        equity_curve,
        realized_curve_period or "All",
    )

    left, right = st.columns([2, 1])

    with left:
        render_section_header(
            "Equity Curve (Realized)",
            "Realized cumulative P&L from closed trades only.",
            kicker="Performance",
        )
        if displayed_equity_curve.empty:
            st.info("No closed trades yet.")
        else:
            final_pnl = displayed_equity_curve["cumulative_pnl"].iloc[-1]
            line_color = "#00b09b" if final_pnl >= 0 else "#ff4b4b"
            fill_color = "rgba(0,176,155,0.15)" if final_pnl >= 0 else "rgba(255,75,75,0.15)"
            fig = go.Figure()
            # Zero reference line
            fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
            fig.add_trace(go.Scatter(
                x=displayed_equity_curve["timestamp"],
                y=displayed_equity_curve["cumulative_pnl"],
                mode="lines",
                name="Cumulative P&L",
                line=dict(color=line_color, width=2),
                fill="tozeroy",
                fillcolor=fill_color,
                hovertemplate="<b>%{x|%Y-%m-%d}</b><br>P&L: $%{y:,.2f}<extra></extra>",
            ))
            fig.update_layout(
                xaxis_title=None,
                yaxis_title="P&L ($)",
                height=300,
                margin=dict(l=0, r=0, t=10, b=0),
                showlegend=False,
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
                yaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
            )
            st.plotly_chart(fig, width="stretch")

    with right:
        render_section_header(
            "Performance Metrics",
            "Computed from realized trade outcomes in the trade log.",
            kicker="Performance",
        )
        pnl_events = _realized_pnl_events(trades_df, key_columns=("symbol",))
        pnl_list = [event["pnl"] for event in pnl_events]
        sample_size = len(pnl_list)
        if not pnl_list:
            st.info("No realized close events yet.")
        elif sample_size < 5:
            st.warning(
                f"Insufficient realized sample for meaningful metrics "
                f"({sample_size} event{'s' if sample_size != 1 else ''})."
            )
        else:
            from reporting.metrics import compute_metrics
            m = compute_metrics(pnl_list)
            st.metric("Sharpe (annualized)", f"{m.sharpe_ratio:.2f}")
            st.metric("Max Drawdown", f"{m.max_drawdown_pct:.1%}")
            st.metric("Profit Factor", f"{m.profit_factor:.2f}")
            st.metric("Win Rate", f"{m.win_rate:.1%}")
            st.metric("Avg W/L Ratio", f"{m.avg_win_loss_ratio:.2f}")

    st.divider()

    # ── Strategy health ───────────────────────────────────────────────────
    render_section_header(
        "Strategy Realized P&L",
        "Closed-trade summary by strategy using realized P&L and slippage.",
        kicker="Strategy Realized P&L",
    )
    strategy_stats = compute_strategy_stats(trades_df)
    if strategy_stats.empty:
        st.info("No trades recorded yet.")
    else:
        display = strategy_stats.copy()
        display = display.rename(columns={
            "strategy": "Strategy",
            "trades": "Trades",
            "wins": "Wins",
            "win_rate": "Win Rate",
            "total_pnl": "Total P&L",
            "avg_slippage_bps": "Avg Slippage Bps",
        })
        st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            column_config={
                "Trades": st.column_config.NumberColumn(format="%d"),
                "Wins": st.column_config.NumberColumn(format="%d"),
                "Win Rate": st.column_config.NumberColumn(format="%.1f%%"),
                "Total P&L": st.column_config.NumberColumn(format="$%.2f"),
                "Avg Slippage Bps": st.column_config.NumberColumn(format="%.1f bps"),
            },
        )

    st.divider()

    # ── Strategy Health & Edge Monitor (PLAN 11.10g) ─────────────────────
    _render_health_and_edge_panel()

    st.divider()

    # ── Active positions + sleeve allocation ────────────────────────────
    pos_col, sleeve_col = st.columns(2)

    with pos_col:
        render_section_header(
            "Open Positions",
            "Current owned positions from the latest engine snapshot.",
            kicker="Exposure",
        )
        positions_detail = display_state.get("positions_detail") or {}
        open_positions = display_state.get("open_positions") or {}
        if not open_positions:
            st.info("No open positions." if is_running else "Engine offline.")
        else:
            pos_data = []
            for sym, strat in open_positions.items():
                # Credit spreads are keyed by UUID, not a tradable symbol —
                # they render in their own "Open Credit Spreads" section.
                if strat == "credit_spread":
                    continue
                detail = positions_detail.get(sym, {})
                entry = detail.get("avg_entry_price")
                upnl = detail.get("unrealized_pnl")
                qty = detail.get("qty")
                cost_basis = (qty * entry) if (qty is not None and entry is not None) else None
                unrealized_pct = (
                    (upnl / cost_basis) if (upnl is not None and cost_basis not in (None, 0))
                    else None
                )
                pos_data.append({
                    "Symbol": symbol_url(sym),
                    "Strategy": strat,
                    "Qty": qty,
                    "Entry": entry,
                    "Cost Basis": cost_basis,
                    "Unrealized P&L": upnl,
                    "Unrealized %": (
                        unrealized_pct * 100.0
                        if unrealized_pct is not None else None
                    ),
                })
            if not pos_data:
                # Every open position is a credit spread — shown below.
                st.info("No equity / single-leg positions open.")
            else:
                st.dataframe(
                    pd.DataFrame(pos_data),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Symbol": st.column_config.LinkColumn(
                            "Symbol",
                            display_text=r"https://finance\.yahoo\.com/quote/([^/]+)/",
                        ),
                        "Qty": st.column_config.NumberColumn(format="%.2f"),
                        "Entry": st.column_config.NumberColumn(format="$%.2f"),
                        "Cost Basis": st.column_config.NumberColumn(format="$%.2f"),
                        "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
                        "Unrealized %": st.column_config.NumberColumn(format="%.2f%%"),
                    },
                )

    with sleeve_col:
        render_section_header(
            "Sleeve Usage",
            "Configured sleeve budgets against current open-position notional.",
            kicker="Exposure",
        )
        sleeve_df = compute_sleeve_usage(
            display_state,
            equity=equity,
            allocations=settings.STRATEGY_ALLOCATIONS,
            total_gross_pct=settings.MAX_GROSS_EXPOSURE_PCT,
        )
        if not sleeve_df.empty:
            display = sleeve_df.copy()
            display["Utilization"] = display["Utilization"] * 100.0
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
                    "Stretch Headroom": st.column_config.NumberColumn(
                        format="$%.2f",
                        help="Extra sleeve capacity temporarily granted by stretch logic.",
                    ),
                    "Used Notional": st.column_config.NumberColumn(format="$%.2f"),
                    "Remaining": st.column_config.NumberColumn(format="$%.2f"),
                    "Max Position Notional": st.column_config.NumberColumn(format="$%.2f"),
                    "Utilization": st.column_config.ProgressColumn(
                        "Utilization",
                        help="Current sleeve notional usage vs configured budget.",
                        format="%.0f%%",
                        min_value=0.0,
                        max_value=100.0,
                    ),
                },
            )
            st.caption(
                "Uses allocator snapshot data when available, including stretched "
                "budget and pending-order-aware usage."
            )

        pool_df = pd.DataFrame.from_dict(
            display_state.get("capital_pools") or {}, orient="index"
        )
        if not pool_df.empty:
            pool_df = pool_df.reset_index().rename(columns={"index": "Pool"})
            pool_df["Utilization"] = pool_df["utilization"] * 100.0
            pool_df = pool_df.rename(columns={
                "target_budget": "Target Budget",
                "used": "Used",
                "available": "Available",
                "pending_entry_notional": "Pending Entry Notional",
            })
            st.caption("Capital Pools")
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
                        "Utilization",
                        format="%.0f%%",
                        min_value=0.0,
                        max_value=100.0,
                    ),
                },
            )

    st.divider()

    # ── Open Multi-Leg Positions (11.39) ────────────────────────────────
    multi_leg_positions = display_state.get("multi_leg_positions") or []
    if multi_leg_positions:
        render_section_header(
            "Open Multi-Leg Positions",
            "Reusable live P/L and risk view for option structures.",
            kicker="Options",
        )
        rows = multi_leg_display_rows(multi_leg_positions)
        st.dataframe(
            pd.DataFrame(rows),
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
        st.caption(
            "Uses dashboard broker refresh when available; otherwise falls back "
            "to the engine's latest multi-leg snapshot."
        )
        st.divider()

    # ── Open Credit Spreads (11.29) ─────────────────────────────────────
    # Multi-leg positions are keyed by position_id, not a tradable symbol,
    # so they get their own table sourced from the engine's `credit_spreads`
    # snapshot field rather than the equity Open Positions table above.
    credit_spreads = state.get("credit_spreads") or []
    if credit_spreads:
        render_section_header(
            "Open Credit Spreads",
            "Bull put credit spreads currently open, from the latest engine snapshot.",
            kicker="Options",
        )
        spread_rows = []
        for sp in credit_spreads:
            net_credit = sp.get("net_credit") or 0.0
            width = sp.get("width") or 0.0
            qty = sp.get("qty") or 1
            max_loss = max(0.0, (width - net_credit) * 100.0 * qty)
            spread_rows.append({
                "Underlying": sp.get("underlying", ""),
                "Strikes": f"{sp.get('short_strike', 0):.0f} / {sp.get('long_strike', 0):.0f}",
                "Expiration": sp.get("expiration", ""),
                "Width": width,
                "Qty": qty,
                "Net Credit": net_credit * 100.0 * qty,
                "Max Loss": max_loss,
                "Status": "Closing" if sp.get("pending_close") else "Open",
            })
        st.dataframe(
            pd.DataFrame(spread_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Width": st.column_config.NumberColumn(format="$%.0f"),
                "Qty": st.column_config.NumberColumn(format="%d"),
                "Net Credit": st.column_config.NumberColumn(
                    format="$%.2f", help="Total credit collected at open."
                ),
                "Max Loss": st.column_config.NumberColumn(
                    format="$%.2f", help="Defined-risk max loss = (width − credit) × 100 × qty."
                ),
            },
        )
        st.divider()

    # ── Open Position Sector Exposure (11.7 Part B) ─────────────────────
    # Live observability — open positions per resolved GICS sector with
    # symbol + owning-strategy detail. No auto-block; operator-facing only.
    sector_exposure = state.get("sector_exposure") or {}
    if sector_exposure:
        render_section_header(
            "Open Position Sector Exposure",
            "Live held positions grouped by resolved sector — symbols and owning strategies. Observability only.",
            kicker="Concentration",
        )
        exposure_rows = []
        for sector, items in sorted(
            sector_exposure.items(), key=lambda kv: (-len(kv[1]), kv[0])
        ):
            # items may be a list[dict{symbol, strategy}] (current shape) or
            # an int (legacy). Defend against both so a stale snapshot does
            # not break the dashboard during a rolling deploy.
            if isinstance(items, int):
                exposure_rows.append({
                    "Sector": sector.replace("_", " ").title(),
                    "Positions": int(items),
                    "Symbols": "",
                    "Strategies": "",
                })
                continue
            symbols = sorted({i["symbol"] for i in items})
            strategies = sorted({i["strategy"] for i in items})
            exposure_rows.append({
                "Sector": sector.replace("_", " ").title(),
                "Positions": len(items),
                "Symbols": ", ".join(symbols),
                "Strategies": ", ".join(strategies),
            })
        st.dataframe(
            pd.DataFrame(exposure_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Positions": st.column_config.NumberColumn(format="%d"),
            },
        )
        st.divider()

    # ── Sector heat ─────────────────────────────────────────────────────
    sector_heat = state.get("sector_heat") or {}
    if sector_heat:
        left_col, right_col = st.columns([2, 1])
        with left_col:
            render_section_header(
                "Sector Heat",
                "Session-level sector momentum snapshot from the bot. Not recomputed on dashboard refresh.",
                kicker="Context",
            )
            counts = sector_heat.get("counts") or {}
            c1, c2, c3 = st.columns(3)
            c1.metric("HOT Sectors", int(counts.get("hot", 0)))
            c2.metric("Neutral Sectors", int(counts.get("neutral", 0)))
            c3.metric("Cold Sectors", int(counts.get("cold", 0)))

            sectors = sector_heat.get("sectors") or {}
            if sectors:
                sector_rows = []
                for sector, detail in sectors.items():
                    sector_rows.append({
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
                            if detail.get("dist_sma50_pct") is not None else None
                        ),
                        "Vol Confirm": detail.get("vol_confirm"),
                    })
                st.dataframe(
                    pd.DataFrame(sector_rows).sort_values(["Score", "Sector"], ascending=[False, True]),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Score": st.column_config.NumberColumn(format="%d"),
                        "Last Close": st.column_config.NumberColumn(format="$%.2f"),
                        "> SMA200": st.column_config.CheckboxColumn(),
                        "> SMA50": st.column_config.CheckboxColumn(),
                        "Golden Cross": st.column_config.CheckboxColumn(),
                        "Dist SMA50": st.column_config.NumberColumn(format="%.1f%%"),
                        "Vol Confirm": st.column_config.CheckboxColumn(),
                    },
                )
            generated_at = sector_heat.get("generated_at")
            if generated_at:
                st.caption(f"Sector heat snapshot generated: {generated_at}")

        with right_col:
            render_section_header(
                "Sector Exposure",
                "Watched symbols grouped by resolved sector. Useful for spotting cold-cluster exposure.",
                kicker="Context",
            )
            symbol_map = sector_heat.get("symbol_map") or {}
            if symbol_map:
                exposure_rows = []
                for sector, items in sorted(symbol_map.items()):
                    detail = (sector_heat.get("sectors") or {}).get(sector, {})
                    by_symbol: dict[str, set[str]] = {}
                    for item in items:
                        by_symbol.setdefault(str(item["symbol"]), set()).add(str(item["strategy"]))
                    symbols = ", ".join(sorted(by_symbol))
                    strategies = ", ".join(
                        sorted({strategy for strategy_set in by_symbol.values() for strategy in strategy_set})
                    )
                    exposure_rows.append({
                        "Sector": sector.replace("_", " ").title(),
                        "Status": _sector_class_badge(detail.get("classification")),
                        "Symbols": symbols,
                        "Strategies": strategies,
                    })
                st.dataframe(
                    pd.DataFrame(exposure_rows),
                    width="stretch",
                    hide_index=True,
                )
            unmapped = sector_heat.get("unmapped") or []
            if unmapped:
                st.caption(
                    "Unmapped symbols: "
                    + ", ".join(sorted({item["symbol"] for item in unmapped}))
                )

        st.divider()

    # ── Watchlists ───────────────────────────────────────────────────────
    render_section_header(
        "Active Watchlists",
        "Per-strategy universes with live regime gating and last-trade context.",
        kicker="Universe",
    )
    strategy_watchlists = settings.STRATEGY_WATCHLISTS
    strategy_allowed_regimes = settings.STRATEGY_ALLOWED_REGIMES
    open_positions = state.get("open_positions") or {}
    watchlist_statuses = state.get("watchlist_statuses") or {}
    watchlist_reasons = state.get("watchlist_reasons") or {}

    if not strategy_watchlists:
        st.info("No strategy watchlists configured.")
    else:
        tabs = st.tabs([s.replace("_", " ").title() for s in strategy_watchlists])
        for tab, (strat_name, symbols) in zip(tabs, strategy_watchlists.items()):
            with tab:
                allowed = strategy_allowed_regimes.get(strat_name, set())
                regime_ok = (
                    regime in allowed if regime else None
                )
                gate_label = (
                    "✅ Entries allowed" if regime_ok
                    else ("🚫 Entries blocked" if regime_ok is False else "⚪ Market closed")
                )
                st.caption(
                    f"Regime: {_regime_color(regime)} {regime or '—'}  |  Gate: {gate_label}  |  "
                    f"{len(symbols)} symbols"
                )

                # Last trade per symbol from the DB
                strat_trades = (
                    trades_df[trades_df["strategy"] == strat_name]
                    if not trades_df.empty and "strategy" in trades_df.columns
                    else pd.DataFrame()
                )
                last_trade: dict[str, dict] = {}
                if not strat_trades.empty:
                    for sym, grp in strat_trades.groupby("symbol"):
                        last = grp.sort_values("timestamp").iloc[-1]
                        last_trade[sym] = {
                            "date": last["timestamp"].strftime("%Y-%m-%d") if pd.notna(last["timestamp"]) else "—",
                            "side": last.get("side", ""),
                            "price": last.get("avg_fill_price"),
                        }

                rows = []
                strategy_status_map = watchlist_statuses.get(strat_name, {})
                strategy_reason_map = watchlist_reasons.get(strat_name, {})
                for sym in symbols:
                    status = strategy_status_map.get(sym)
                    if status is None:
                        is_open = sym in open_positions and open_positions[sym] == strat_name
                        status = "Long" if is_open else "No Signal"
                    reasons = strategy_reason_map.get(sym) or []
                    lt = last_trade.get(sym, {})
                    price = lt.get("price")
                    rows.append({
                        "Symbol": symbol_url(sym),
                        "Status": status,
                        "Reason": "; ".join(reasons) if reasons else "—",
                        "Last Trade": lt.get("date", "—"),
                        "Last Side": lt.get("side", "—").upper() if lt.get("side") else "—",
                        "Last Price": f"${float(price):,.2f}" if price else "—",
                    })

                st.dataframe(
                    pd.DataFrame(rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Symbol": st.column_config.LinkColumn(
                            "Symbol",
                            display_text=r"https://finance\.yahoo\.com/quote/([^/]+)/",
                        ),
                    },
                )

    st.divider()

    # ── Recent trades ────────────────────────────────────────────────────
    render_section_header(
        "Recent Trades",
        "Most recent fills from the selected trade database.",
        kicker="Audit Trail",
    )
    if trades_df.empty:
        st.info("No trades in the database yet.")
    else:
        display_cols = [
            "timestamp", "symbol", "side", "qty", "avg_fill_price",
            "strategy", "reason", "realized_slippage_bps",
        ]
        available = [c for c in display_cols if c in trades_df.columns]
        recent = trades_df[available].tail(20).copy()
        if "timestamp" in recent.columns:
            recent["timestamp"] = recent["timestamp"].dt.strftime("%Y-%m-%d %H:%M")
        if "avg_fill_price" in recent.columns:
            recent["avg_fill_price"] = recent["avg_fill_price"].map(
                lambda x: f"${x:,.2f}" if pd.notna(x) else "—"
            )
        recent = recent.rename(columns={
            "timestamp": "Timestamp",
            "symbol": "Symbol",
            "side": "Side",
            "qty": "Qty",
            "avg_fill_price": "Avg Fill Price",
            "strategy": "Strategy",
            "reason": "Reason",
            "realized_slippage_bps": "Realized Slippage Bps",
        })
        if "Symbol" in recent.columns:
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

    # ── Auto-refresh ──────────────────────────────────────────────────────
    st.caption("Dashboard auto-refreshes every 30 seconds.")
    time.sleep(30)
    st.rerun()


if __name__ == "__main__":
    render_dashboard()
