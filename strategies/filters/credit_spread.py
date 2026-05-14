"""
Credit spread edge filter (Phase 11.29).

``CreditSpreadEdgeFilter`` enforces the entry gates from
docs/credit_spread_strategy.md §3 that are not position-state or
chain-availability concerns:

  Gate 2 — Underlying trend:
    underlying close > its own 50-day SMA. Don't sell puts into a
    downtrend on this specific instrument. Per-bar.

  Gate 3 — Volatility floor:
    the instrument's IV proxy ≥ ``min_iv_proxy`` (index points). Only
    sell premium when premium is rich. The proxy source (VIX / RVX) is
    per-instrument. yfinance only exposes *today's* index level cheaply,
    so this gate is evaluated once and broadcast across the frame — the
    engine acts on the latest bar only, and this strategy is live-only
    (not vectorbt-backtested), so a per-bar IV history is unnecessary.

  Gate 9 — Earnings blackout (single names only):
    no entry within ``earnings_blackout_days`` of a known earnings date.
    A no-op for ETFs (``earnings_blackout_days == 0``), which is all of
    v1 (SPY, QQQ).

The remaining §3 gates live elsewhere: the regime gate is the slot's
``allowed_regimes``; DTE/spread availability and the position-count caps
are the strategy's own concern (they need the chain and per-instance
position state).

Fails OPEN on an IV-proxy fetch failure — ``IVProxyResolver`` already
falls back to a neutral level and logs, so the gate degrades to "allow"
rather than locking the strategy out on a transient yfinance hiccup.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from strategies.base import EdgeFilterDecision
from strategies.filters.common import EarningsBlackout
from utils.iv_proxy import IVProxyResolver, is_valid_source


_DEFAULT_SMA_WINDOW = 50


class CreditSpreadEdgeFilter:
    """
    Entry gate for the credit-spread strategy, per underlying instance.

    Args:
        iv_proxy_source:       IV proxy source name ("vix" / "rvx").
        min_iv_proxy:          Minimum IV proxy in index points to allow entry.
        sma_window:            Trend-gate SMA window (default 50).
        earnings_blackout_days: Calendar days around earnings to block. 0 for
                               ETFs disables the earnings gate entirely.
        iv_resolver:           Injected IVProxyResolver (tests pass a stub;
                               production shares one resolver across instances).
    """

    def __init__(
        self,
        *,
        iv_proxy_source: str,
        min_iv_proxy: float,
        sma_window: int = _DEFAULT_SMA_WINDOW,
        earnings_blackout_days: int = 0,
        iv_resolver: IVProxyResolver | None = None,
    ) -> None:
        if not is_valid_source(iv_proxy_source):
            raise ValueError(
                f"unknown iv_proxy_source {iv_proxy_source!r} — "
                "expected 'vix' or 'rvx'"
            )
        self._iv_source = iv_proxy_source
        self._min_iv_proxy = float(min_iv_proxy)
        self._sma_window = int(sma_window)
        self._iv_resolver = iv_resolver or IVProxyResolver()
        self._symbol: str = ""
        # Earnings gate only matters for single names; ETFs pass 0 and the
        # gate is skipped entirely (no yfinance lookups).
        self._earnings: EarningsBlackout | None = (
            EarningsBlackout(
                days_before=earnings_blackout_days,
                days_after=earnings_blackout_days,
            )
            if earnings_blackout_days > 0
            else None
        )

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol
        if self._earnings is not None:
            self._earnings.set_symbol(symbol)

    def _trend_gate(self, df: pd.DataFrame) -> pd.Series:
        """True where the underlying close is above its own N-day SMA.

        Fails open on insufficient history (< N bars) — the strategy's
        chain/availability checks still gate those edge cases."""
        close = df["close"].astype(float)
        sma = close.rolling(self._sma_window).mean()
        gate = close > sma
        return gate.where(sma.notna(), other=True).astype(bool)

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        if df.empty:
            return EdgeFilterDecision.allow_all(df.index)

        trend_gate = self._trend_gate(df)

        # IV proxy is a "right now" scalar — resolve once, broadcast.
        iv_value = self._iv_resolver.resolve(self._iv_source)
        iv_ok = iv_value >= self._min_iv_proxy
        iv_gate = pd.Series(iv_ok, index=df.index, dtype=bool)

        if self._earnings is not None:
            earnings_gate = self._earnings(df).astype(bool)
        else:
            earnings_gate = pd.Series(True, index=df.index, dtype=bool)

        combined = trend_gate & iv_gate & earnings_gate

        reasons_by_bar: list[list[str]] = []
        for trend_ok, earn_ok in zip(
            trend_gate.tolist(), earnings_gate.tolist(), strict=False
        ):
            row: list[str] = []
            if not trend_ok:
                row.append(f"underlying below {self._sma_window} SMA (downtrend)")
            if not iv_ok:
                row.append(
                    f"IV proxy {iv_value:.1f} < min {self._min_iv_proxy:.1f} "
                    f"({self._iv_source.upper()})"
                )
            if not earn_ok:
                row.append("earnings blackout")
            reasons_by_bar.append(row)

        decision = EdgeFilterDecision(
            allowed=combined.astype(bool),
            reasons=pd.Series(reasons_by_bar, index=df.index, dtype=object),
        )

        # Operator-facing log on the latest bar.
        if decision.latest_allowed:
            logger.info(
                f"CREDIT_SPREAD_FILTER_ALLOWED {self._symbol} — "
                f"trend={bool(trend_gate.iloc[-1])} "
                f"iv={iv_value:.1f}≥{self._min_iv_proxy:.1f} "
                f"earnings={bool(earnings_gate.iloc[-1])}"
            )
        else:
            logger.info(
                f"CREDIT_SPREAD_FILTER_BLOCKED {self._symbol} — "
                + ", ".join(decision.latest_reasons)
            )

        return decision
