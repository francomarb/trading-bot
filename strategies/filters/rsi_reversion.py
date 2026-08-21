"""
RSI Reversion edge filter.

The active RSI experiment is intentionally simple: buy short-term RSI3 dips
only when the individual stock remains in a long-term uptrend and is liquid
enough for limit-order execution. Broader market regime, SPY50, sector,
earnings, and breakdown gates are deliberately excluded so this strategy can
prove or disprove its own signal without being starved by stacked protection.
"""

from __future__ import annotations

import pandas as pd

from strategies.base import EdgeFilterDecision


_VOL_MIN_WINDOW = 20
_NOTIONAL_MIN_AVG = 10_000_000
_STOCK_TREND_SMA = 200


class RSIEdgeFilter:
    """
    Basic entry gate for the RSI3 quick-exit experiment.

    Gates:
      1. Stock close > 200-day SMA.
      2. 20-day average dollar volume >= notional_min_avg.

    Liquidity fails open when volume is unavailable or history is short. The
    stock trend gate fails closed until enough history exists for SMA200.
    """

    def __init__(
        self,
        *,
        stock_sma_window: int = _STOCK_TREND_SMA,
        vol_min_window: int = _VOL_MIN_WINDOW,
        notional_min_avg: int = _NOTIONAL_MIN_AVG,
    ) -> None:
        if stock_sma_window < 1:
            raise ValueError("stock_sma_window must be positive")
        if vol_min_window < 1:
            raise ValueError("vol_min_window must be positive")
        if notional_min_avg < 0:
            raise ValueError("notional_min_avg must be non-negative")
        self._stock_sma_window = int(stock_sma_window)
        self._vol_min_window = int(vol_min_window)
        self._notional_min_avg = int(notional_min_avg)
        self._symbol = ""
        self._last_metrics: dict[str, float | bool | None] = {}

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol

    @property
    def last_metrics(self) -> dict[str, float | bool | None]:
        """Latest-bar diagnostics for compact candidate logging."""
        return dict(self._last_metrics)

    def _stock_above_sma(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        close = df["close"].astype(float)
        sma = close.rolling(self._stock_sma_window).mean()
        allowed = (close > sma).where(sma.notna(), False).astype(bool)
        return allowed, sma

    def _volume_liquid(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series | None]:
        if "volume" not in df.columns or "close" not in df.columns:
            return pd.Series(True, index=df.index, dtype=bool), None
        dollar_vol = df["close"].astype(float) * df["volume"].astype(float)
        avg = dollar_vol.rolling(self._vol_min_window).mean()
        liquid = avg >= self._notional_min_avg
        liquid = liquid.where(avg.notna(), other=True)
        return liquid.astype(bool), avg

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        if "close" not in df.columns:
            raise ValueError("RSIEdgeFilter requires a 'close' column")

        stock_gate, stock_sma = self._stock_above_sma(df)
        liquid_gate, avg_dollar_vol = self._volume_liquid(df)
        combined = (stock_gate & liquid_gate).astype(bool)

        reasons_by_bar: list[list[str]] = []
        for i, (stock_ok, liquid_ok) in enumerate(
            zip(stock_gate.tolist(), liquid_gate.tolist(), strict=False)
        ):
            row_reasons: list[str] = []
            if not stock_ok:
                close = float(df["close"].astype(float).iloc[i])
                sma = stock_sma.iloc[i]
                if pd.isna(sma):
                    row_reasons.append(
                        f"stock trend unavailable: need {self._stock_sma_window} bars "
                        f"for SMA{self._stock_sma_window}"
                    )
                else:
                    row_reasons.append(
                        f"stock below SMA{self._stock_sma_window}: "
                        f"close ${close:.2f} <= SMA ${float(sma):.2f}"
                    )
            if not liquid_ok:
                avg = None if avg_dollar_vol is None else avg_dollar_vol.iloc[i]
                avg_str = f"${float(avg):,.0f}" if avg is not None and pd.notna(avg) else "NaN"
                row_reasons.append(
                    f"liquidity too low (avg_dollar_vol{self._vol_min_window}={avg_str} "
                    f"< ${self._notional_min_avg:,})"
                )
            reasons_by_bar.append(row_reasons)

        if df.empty:
            self._last_metrics = {}
        else:
            latest_avg = None if avg_dollar_vol is None else avg_dollar_vol.iloc[-1]
            latest_sma = stock_sma.iloc[-1]
            self._last_metrics = {
                "stock_above_sma": bool(stock_gate.iloc[-1]),
                "liquid": bool(liquid_gate.iloc[-1]),
                "stock_sma": (
                    float(latest_sma) if pd.notna(latest_sma) else None
                ),
                "avg_dollar_vol": (
                    float(latest_avg) if latest_avg is not None and pd.notna(latest_avg) else None
                ),
            }

        return EdgeFilterDecision(
            allowed=combined,
            reasons=pd.Series(reasons_by_bar, index=df.index, dtype=object),
        )
