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

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings


# ── Data loading helpers (pure functions — tested independently) ─────────────


def load_trades(db_path: str) -> pd.DataFrame:
    """Load all rows from the trades table. Returns empty DataFrame if missing."""
    path = Path(db_path)
    if not path.exists():
        return pd.DataFrame(columns=[
            "id", "timestamp", "symbol", "side", "qty", "avg_fill_price",
            "order_id", "strategy", "reason", "stop_price",
            "entry_reference_price", "modeled_slippage_bps",
            "realized_slippage_bps", "order_type", "status",
            "requested_qty", "filled_qty",
        ])
    try:
        conn = sqlite3.connect(str(path))
        df = pd.read_sql_query("SELECT * FROM trades ORDER BY timestamp ASC", conn)
        conn.close()
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    except Exception:
        return pd.DataFrame()


def load_engine_state(path: str) -> dict:
    """Read engine_state.json. Returns {} if missing or malformed."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


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

    rows = []
    open_buys: dict[str, list[tuple]] = {}  # symbol → [(qty, price)]

    for _, row in trades_df.iterrows():
        symbol = row.get("symbol", "")
        side = (row.get("side") or "").lower()
        qty = float(row.get("filled_qty") or row.get("qty") or 0)
        price = float(row.get("avg_fill_price") or 0)
        ts = row.get("timestamp")

        if side == "buy" and qty > 0 and price > 0:
            open_buys.setdefault(symbol, []).append((qty, price))
        elif side == "sell" and qty > 0 and price > 0:
            buys = open_buys.get(symbol, [])
            if buys:
                buy_qty, buy_price = buys.pop(0)
                matched_qty = min(qty, buy_qty)
                pnl = (price - buy_price) * matched_qty
                rows.append({"timestamp": ts, "pnl": pnl})

    if not rows:
        return pd.DataFrame(columns=["timestamp", "cumulative_pnl"])

    df = pd.DataFrame(rows).sort_values("timestamp")
    df["cumulative_pnl"] = df["pnl"].cumsum()
    return df[["timestamp", "cumulative_pnl"]]


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

    A "win" is defined as a sell row where realized P&L > 0.
    """
    if trades_df.empty or "strategy" not in trades_df.columns:
        return pd.DataFrame(columns=[
            "strategy", "trades", "wins", "win_rate",
            "total_pnl", "avg_slippage_bps",
        ])

    results = []
    for strategy, group in trades_df.groupby("strategy"):
        sells = group[group["side"].str.lower() == "sell"]
        buys = group[group["side"].str.lower() == "buy"]

        # Match buys to sells by symbol to compute P&L
        pnls: list[float] = []
        open_buys: dict[str, list[tuple]] = {}
        for _, row in group.sort_values("timestamp").iterrows():
            sym = row.get("symbol", "")
            side = (row.get("side") or "").lower()
            qty = float(row.get("filled_qty") or row.get("qty") or 0)
            price = float(row.get("avg_fill_price") or 0)
            if side == "buy" and qty > 0 and price > 0:
                open_buys.setdefault(sym, []).append((qty, price))
            elif side == "sell" and qty > 0 and price > 0:
                _buys = open_buys.get(sym, [])
                if _buys:
                    bqty, bprice = _buys.pop(0)
                    pnls.append((price - bprice) * min(qty, bqty))

        wins = sum(1 for p in pnls if p > 0)
        trade_count = len(pnls)
        total_pnl = sum(pnls)
        win_rate = wins / trade_count if trade_count > 0 else 0.0
        avg_slip = float(
            pd.to_numeric(group["realized_slippage_bps"], errors="coerce").dropna().mean()
        ) if "realized_slippage_bps" in group.columns else 0.0

        results.append({
            "strategy": strategy,
            "trades": trade_count,
            "wins": wins,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_slippage_bps": avg_slip,
        })

    return pd.DataFrame(results)


# ── Dashboard layout ─────────────────────────────────────────────────────────


def _regime_color(regime: str | None) -> str:
    colors = {
        "TRENDING": "🟢",
        "RANGING": "🟡",
        "VOLATILE": "🟠",
        "BEAR": "🔴",
    }
    return colors.get(regime or "", "⚪")


def render_dashboard() -> None:
    st.set_page_config(
        page_title="Trading Bot Dashboard",
        page_icon="📈",
        layout="wide",
    )
    st.title("📈 Trading Bot — Analytics Dashboard")

    # ── Load data ────────────────────────────────────────────────────────
    state = load_engine_state(settings.STATE_SNAPSHOT_PATH)
    live_trading = state.get("live_trading", settings.LIVE_TRADING)
    db_path = settings.TRADE_LOG_DB_LIVE if live_trading else settings.TRADE_LOG_DB_PAPER
    trades_df = load_trades(db_path)

    # ── Header row ───────────────────────────────────────────────────────
    env_label = "🔴 LIVE" if live_trading else "📄 PAPER"
    is_running = state.get("running", False)
    status_label = "🟢 Running" if is_running else "⚫ Offline"
    regime = state.get("regime")
    equity = state.get("equity", 0.0)
    daily_pnl = state.get("daily_pnl", 0.0)
    cycle_count = state.get("cycle_count", 0)
    ts = state.get("timestamp", "—")

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Status", status_label)
    col2.metric("Mode", env_label)
    session_start = state.get("session_start_equity", equity)
    equity_delta = equity - session_start if session_start else None
    col3.metric("Equity", f"${equity:,.2f}",
                delta=f"${equity_delta:+,.2f}" if equity_delta is not None else None)
    col4.metric("Daily P&L", f"${daily_pnl:+,.2f}",
                delta=f"${daily_pnl:+,.2f}" if daily_pnl != 0 else None,
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

    st.divider()

    # ── Equity curve + rolling Sharpe ────────────────────────────────────
    equity_curve = compute_equity_curve(trades_df)

    left, right = st.columns([2, 1])

    with left:
        st.subheader("Equity Curve (Cumulative P&L)")
        if equity_curve.empty:
            st.info("No closed trades yet.")
        else:
            final_pnl = equity_curve["cumulative_pnl"].iloc[-1]
            line_color = "#00b09b" if final_pnl >= 0 else "#ff4b4b"
            fill_color = "rgba(0,176,155,0.15)" if final_pnl >= 0 else "rgba(255,75,75,0.15)"
            fig = go.Figure()
            # Zero reference line
            fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
            fig.add_trace(go.Scatter(
                x=equity_curve["timestamp"],
                y=equity_curve["cumulative_pnl"],
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
            st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Performance Metrics")
        if equity_curve.empty or len(equity_curve) < 3:
            st.info("Need more trades for metrics.")
        else:
            from reporting.metrics import compute_metrics
            sells = trades_df[trades_df["side"].str.lower() == "sell"]
            if not sells.empty and "avg_fill_price" in sells.columns:
                # Approximate per-trade P&L for metric computation
                open_buys_m: dict[str, list[tuple]] = {}
                pnl_list: list[float] = []
                for _, row in trades_df.sort_values("timestamp").iterrows():
                    sym = row.get("symbol", "")
                    side = (row.get("side") or "").lower()
                    qty = float(row.get("filled_qty") or row.get("qty") or 0)
                    price = float(row.get("avg_fill_price") or 0)
                    if side == "buy" and qty > 0 and price > 0:
                        open_buys_m.setdefault(sym, []).append((qty, price))
                    elif side == "sell" and qty > 0 and price > 0:
                        _bs = open_buys_m.get(sym, [])
                        if _bs:
                            bq, bp = _bs.pop(0)
                            pnl_list.append((price - bp) * min(qty, bq))
                if pnl_list:
                    m = compute_metrics(pnl_list)
                    st.metric("Sharpe (annualized)", f"{m.sharpe_ratio:.2f}")
                    st.metric("Max Drawdown", f"{m.max_drawdown_pct:.1%}")
                    st.metric("Profit Factor", f"{m.profit_factor:.2f}")
                    st.metric("Win Rate", f"{m.win_rate:.1%}")
                    st.metric("Avg W/L Ratio", f"{m.avg_win_loss_ratio:.2f}")
            else:
                st.info("No closed trades yet.")

    st.divider()

    # ── Strategy health ───────────────────────────────────────────────────
    st.subheader("Strategy Health")
    strategy_stats = compute_strategy_stats(trades_df)
    if strategy_stats.empty:
        st.info("No trades recorded yet.")
    else:
        display = strategy_stats.copy()
        display["win_rate"] = display["win_rate"].map("{:.1%}".format)
        display["total_pnl"] = display["total_pnl"].map("${:,.2f}".format)
        display["avg_slippage_bps"] = display["avg_slippage_bps"].map("{:.1f} bps".format)
        st.dataframe(display, use_container_width=True, hide_index=True)

    st.divider()

    # ── Active positions + sleeve allocation ────────────────────────────
    pos_col, sleeve_col = st.columns(2)

    with pos_col:
        st.subheader("Open Positions")
        positions_detail = state.get("positions_detail") or {}
        open_positions = state.get("open_positions") or {}
        if not open_positions:
            st.info("No open positions." if is_running else "Engine offline.")
        else:
            pos_data = []
            for sym, strat in open_positions.items():
                detail = positions_detail.get(sym, {})
                entry = detail.get("avg_entry_price")
                upnl = detail.get("unrealized_pnl")
                qty = detail.get("qty")
                cost_basis = (qty * entry) if (qty is not None and entry is not None) else None
                pos_data.append({
                    "symbol": sym,
                    "strategy": strat,
                    "qty": qty,
                    "entry $": f"${entry:,.2f}" if entry is not None else "—",
                    "cost basis": f"${cost_basis:,.2f}" if cost_basis is not None else "—",
                    "unreal. P&L": f"${upnl:+,.2f}" if upnl is not None else "—",
                })
            st.dataframe(pd.DataFrame(pos_data), use_container_width=True, hide_index=True)

    with sleeve_col:
        st.subheader("Sleeve Allocation")
        # Derive sleeve allocation from open positions count and settings
        open_pos = state.get("open_positions") or {}
        alloc_rows = []
        for name, cfg in settings.STRATEGY_ALLOCATIONS.items():
            used_count = sum(1 for strat in open_pos.values() if strat == name)
            max_pos = cfg.get("max_positions", "?")
            weight = cfg.get("weight", 0)
            alloc_rows.append({
                "strategy": name,
                "weight": f"{weight:.0%}",
                "open positions": f"{used_count}/{max_pos}",
            })
        if alloc_rows:
            st.dataframe(pd.DataFrame(alloc_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Watchlists ───────────────────────────────────────────────────────
    st.subheader("Active Watchlists")
    strategy_watchlists = settings.STRATEGY_WATCHLISTS
    strategy_allowed_regimes = settings.STRATEGY_ALLOWED_REGIMES
    open_positions = state.get("open_positions") or {}

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
                for sym in symbols:
                    is_open = sym in open_positions and open_positions[sym] == strat_name
                    lt = last_trade.get(sym, {})
                    price = lt.get("price")
                    rows.append({
                        "symbol": sym,
                        "status": "🟢 Long" if is_open else "⚪ Flat",
                        "last trade": lt.get("date", "—"),
                        "last side": lt.get("side", "—").upper() if lt.get("side") else "—",
                        "last price": f"${float(price):,.2f}" if price else "—",
                    })

                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                )

    st.divider()

    # ── Recent trades ────────────────────────────────────────────────────
    st.subheader("Recent Trades (last 20)")
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
        st.dataframe(recent[::-1], use_container_width=True, hide_index=True)

    # ── Auto-refresh ──────────────────────────────────────────────────────
    st.caption("Dashboard auto-refreshes every 30 seconds.")
    time.sleep(30)
    st.rerun()


if __name__ == "__main__":
    render_dashboard()
