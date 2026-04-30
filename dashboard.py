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

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings


# ── Data loading helpers (pure functions — tested independently) ─────────────


def load_trades(db_path: str) -> pd.DataFrame:
    """Load all rows from the trades table. Returns empty DataFrame if missing."""
    def _empty(error: str | None = None) -> pd.DataFrame:
        df = pd.DataFrame(columns=[
            "id", "timestamp", "symbol", "side", "qty", "avg_fill_price",
            "order_id", "strategy", "reason", "stop_price",
            "entry_reference_price", "modeled_slippage_bps",
            "realized_slippage_bps", "order_type", "status",
            "requested_qty", "filled_qty",
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
        events = _realized_pnl_events(group, key_columns=("symbol",))
        pnls = [event["pnl"] for event in events]

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


def compute_sleeve_usage(
    state: dict,
    *,
    equity: float,
    allocations: dict[str, dict],
    total_gross_pct: float,
) -> pd.DataFrame:
    """Compute actual sleeve usage from the state snapshot's open positions."""
    positions_detail = state.get("positions_detail") or {}
    rows = []

    for strategy_name, cfg in allocations.items():
        weight = float(cfg.get("weight", 0.0) or 0.0)
        budget = equity * total_gross_pct * weight
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
            "Weight": weight,
            "Budget": budget,
            "Used Notional": used_notional,
            "Remaining": remaining,
            "Utilization": utilization,
            "Open Positions": len(open_positions),
            "Max Positions": cfg.get("max_positions", "?"),
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


def _regime_color(regime: str | None) -> str:
    colors = {
        "TRENDING": "🟢",
        "RANGING": "🟡",
        "VOLATILE": "🟠",
        "BEAR": "🔴",
    }
    key = (regime or "").upper()
    return colors.get(key, "⚪")


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
        render_section_header(
            "Equity Curve",
            "Realized cumulative P&L from closed trades only.",
            kicker="Performance",
        )
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
            st.plotly_chart(fig, width="stretch")

    with right:
        render_section_header(
            "Performance Metrics",
            "Computed from realized trade outcomes in the trade log.",
            kicker="Performance",
        )
        if equity_curve.empty or len(equity_curve) < 3:
            st.info("Need more trades for metrics.")
        else:
            from reporting.metrics import compute_metrics
            pnl_events = _realized_pnl_events(trades_df, key_columns=("symbol",))
            pnl_list = [event["pnl"] for event in pnl_events]
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
    render_section_header(
        "Strategy Health",
        "Closed-trade summary by strategy using realized P&L and slippage.",
        kicker="Attribution",
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

    # ── Active positions + sleeve allocation ────────────────────────────
    pos_col, sleeve_col = st.columns(2)

    with pos_col:
        render_section_header(
            "Open Positions",
            "Current owned positions from the latest engine snapshot.",
            kicker="Exposure",
        )
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
                    "Symbol": symbol_url(sym),
                    "Strategy": strat,
                    "Qty": qty,
                    "Entry": f"${entry:,.2f}" if entry is not None else "—",
                    "Cost Basis": f"${cost_basis:,.2f}" if cost_basis is not None else "—",
                    "Unrealized P&L": f"${upnl:+,.2f}" if upnl is not None else "—",
                })
            st.dataframe(
                pd.DataFrame(pos_data),
                width="stretch",
                hide_index=True,
                column_config={
                    "Symbol": st.column_config.LinkColumn(
                        "Symbol",
                        display_text=r"https://finance\.yahoo\.com/quote/([^/]+)/",
                    ),
                },
            )

    with sleeve_col:
        render_section_header(
            "Sleeve Usage",
            "Configured sleeve budgets against current open-position notional.",
            kicker="Exposure",
        )
        sleeve_df = compute_sleeve_usage(
            state,
            equity=equity,
            allocations=settings.STRATEGY_ALLOCATIONS,
            total_gross_pct=settings.MAX_GROSS_EXPOSURE_PCT,
        )
        if not sleeve_df.empty:
            display = sleeve_df.copy()
            display["Open Positions"] = display.apply(
                lambda row: f"{row['Open Positions']}/{row['Max Positions']}",
                axis=1,
            )
            display = display.drop(columns=["Max Positions"])
            st.dataframe(
                display,
                width="stretch",
                hide_index=True,
                column_config={
                    "Weight": st.column_config.NumberColumn(format="%.0f%%"),
                    "Budget": st.column_config.NumberColumn(format="$%.2f"),
                    "Used Notional": st.column_config.NumberColumn(format="$%.2f"),
                    "Remaining": st.column_config.NumberColumn(format="$%.2f"),
                    "Utilization": st.column_config.ProgressColumn(
                        "Utilization",
                        help="Current sleeve notional usage vs configured budget.",
                        format="%.1f%%",
                        min_value=0.0,
                        max_value=1.0,
                    ),
                },
            )
            st.caption(
                "Uses current open-position notional from the engine snapshot. "
                "Pending buy orders are not included here."
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
                for sym in symbols:
                    status = strategy_status_map.get(sym)
                    if status is None:
                        is_open = sym in open_positions and open_positions[sym] == strat_name
                        status = "Long" if is_open else "Flat"
                    lt = last_trade.get(sym, {})
                    price = lt.get("price")
                    rows.append({
                        "Symbol": symbol_url(sym),
                        "Status": status,
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
