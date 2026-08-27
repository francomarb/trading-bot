"""
Confirmed SMA trend strategy for leveraged index ETFs.

The strategy deliberately separates the instrument that produces the signal
from the instrument that is traded.  Backtest frames carry the leveraged
fund's OHLC columns for execution plus a ``signal_close`` column containing
the adjusted daily close of the unleveraged benchmark ETF.

Signal semantics
----------------
* Historical replay starts OUT (cash). The first entry still requires a fully
  confirmed run; the first evaluable close alone never creates a transition.
* Enter after ``entry_days`` consecutive signal closes strictly above SMA.
* Exit after ``exit_days`` consecutive signal closes strictly below SMA.
* A close exactly on the SMA resets both streaks and changes no position.

Signals are emitted on the completed close that confirms the streak.  The
backtester owns the look-ahead-safe shift to the following session's open.
Separately, ``current_position_target`` exposes the ending confirmed LONG/FLAT
state so the live engine can reconcile a cold start without inventing a new
transition event. There is no stop-loss or secondary exit in this baseline.
"""

from __future__ import annotations

import pandas as pd

from risk.models import (
    PositionRiskProfile,
    ProtectionModel,
    SizingModel,
)
from strategies.base import BaseStrategy, OrderType, PositionTarget, SignalFrame


class LeveragedTrend(BaseStrategy):
    """Trade a leveraged fund from confirmed SMA state on its underlying."""

    name = "leveraged_trend"
    preferred_order_type = OrderType.MARKET

    def __init__(
        self,
        *,
        sma_length: int = 200,
        entry_days: int = 5,
        exit_days: int = 2,
        signal_column: str = "signal_close",
        target_notional_pct: float = 0.10,
        stated_leverage_multiplier: float = 3.0,
        stress_exposure_multiplier: float = 3.0,
    ) -> None:
        super().__init__()
        for name, value in (
            ("sma_length", sma_length),
            ("entry_days", entry_days),
            ("exit_days", exit_days),
        ):
            if not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if not isinstance(signal_column, str):
            raise TypeError("signal_column must be a string")
        if not signal_column:
            raise ValueError("signal_column must not be empty")
        if not 0 < target_notional_pct <= 1:
            raise ValueError("target_notional_pct must be in (0, 1]")
        if stated_leverage_multiplier <= 0:
            raise ValueError("stated_leverage_multiplier must be positive")
        if stress_exposure_multiplier < stated_leverage_multiplier:
            raise ValueError(
                "stress_exposure_multiplier must be >= stated leverage"
            )

        self.sma_length = sma_length
        self.entry_days = entry_days
        self.exit_days = exit_days
        self.signal_column = signal_column
        self.target_notional_pct = target_notional_pct
        self.stated_leverage_multiplier = stated_leverage_multiplier
        self.stress_exposure_multiplier = stress_exposure_multiplier

    def risk_profile(self, symbol: str) -> PositionRiskProfile:
        """Request stopless notional sizing; RiskManager applies final caps."""
        return PositionRiskProfile(
            sizing_model=SizingModel.NOTIONAL,
            protection_model=ProtectionModel.SIGNAL_EXIT_ONLY,
            target_notional_pct=self.target_notional_pct,
            stated_leverage_multiplier=self.stated_leverage_multiplier,
            stress_exposure_multiplier=self.stress_exposure_multiplier,
        )

    def required_bars(self) -> int:
        """Bars required to warm the SMA and complete either confirmation."""
        return self.sma_length + max(self.entry_days, self.exit_days) - 1

    def _replay(
        self, df: pd.DataFrame
    ) -> tuple[SignalFrame, PositionTarget | None]:
        """Replay transition events and return the confirmed ending target."""
        if self.signal_column not in df.columns:
            raise ValueError(
                "LeveragedTrend requires an explicit "
                f"{self.signal_column!r} column from the unleveraged signal asset"
            )
        if not df.index.is_unique:
            raise ValueError("LeveragedTrend requires a unique bar index")

        signal_close = pd.to_numeric(df[self.signal_column], errors="coerce")
        if signal_close.isna().any():
            raise ValueError(
                f"LeveragedTrend {self.signal_column!r} contains missing or "
                "non-numeric values"
            )

        sma = signal_close.rolling(
            window=self.sma_length, min_periods=self.sma_length
        ).mean()
        above = signal_close > sma
        below = signal_close < sma

        entries = pd.Series(False, index=df.index, dtype=bool)
        exits = pd.Series(False, index=df.index, dtype=bool)
        in_position = False
        target: PositionTarget | None = None
        above_streak = 0
        below_streak = 0

        for idx in range(len(df)):
            if pd.isna(sma.iloc[idx]):
                above_streak = 0
                below_streak = 0
                continue

            if bool(above.iloc[idx]):
                above_streak += 1
                below_streak = 0
            elif bool(below.iloc[idx]):
                below_streak += 1
                above_streak = 0
            else:
                # Strict comparisons: equality confirms neither direction.
                above_streak = 0
                below_streak = 0

            # The live fetch window is finite and may begin after the true
            # phase started. Infer a target only after this window contains a
            # complete confirmation in either direction; until then the prior
            # state is unknowable and reconciliation must do nothing.
            if above_streak >= self.entry_days:
                target = PositionTarget.LONG
            elif below_streak >= self.exit_days:
                target = PositionTarget.FLAT

            if not in_position and above_streak >= self.entry_days:
                entries.iloc[idx] = True
                in_position = True
            elif in_position and below_streak >= self.exit_days:
                exits.iloc[idx] = True
                in_position = False

        return SignalFrame(entries=entries, exits=exits), target

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        """Return confirmed transition events for backtest/live parity."""
        signals, _target = self._replay(df)
        return signals

    def current_position_target(self, df: pd.DataFrame) -> PositionTarget | None:
        """Return LONG/FLAT for the latest fully replayed confirmed phase."""
        _signals, target = self._replay(df)
        return target

    def __repr__(self) -> str:
        return (
            "LeveragedTrend("
            f"sma_length={self.sma_length}, entry_days={self.entry_days}, "
            f"exit_days={self.exit_days}, signal_column={self.signal_column!r})"
        )
