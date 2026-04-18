"""
Technical indicator wrappers (Phase 3).

Scope: the minimum set needed for the MVP strategy (SMA crossover, Phase 4) and
the ATR-based stop-loss in Phase 6 Risk. Indicators are deferred until a
strategy actually needs them (YAGNI).

Currently provided:
  - add_sma(df, length)   → df with 'sma_{length}'    column appended
  - add_ema(df, length)   → df with 'ema_{length}'    column appended
  - add_atr(df, length)   → df with 'atr_{length}'    column appended (Wilder/RMA)

Design notes:
  - Every function is pure: it returns a new DataFrame. Inputs are not mutated.
  - Column names are predictable and include the window length so multiple
    windows of the same indicator can coexist on one frame (e.g. sma_20 +
    sma_50 for a crossover).
  - Implementations are hand-rolled, not pandas-ta. Reason: pandas-ta is on an
    abandoned/lightly-maintained fork path and has had pandas 2.x breakage in
    the past. SMA/EMA/ATR are ~5 lines each; hand-rolling eliminates a
    dependency risk on a path that will eventually manage real money. When a
    future indicator warrants pandas-ta, we can reintroduce it selectively.

References:
  - SMA: arithmetic mean over the last N closes.
  - EMA: recursive with alpha = 2/(N+1). First value seeded with the SMA of
    the first N closes (Wilder-style) so the series starts at index N-1 and
    matches pandas-ta output.
  - ATR: Wilder's smoothing (RMA) of True Range, where
    TR = max(H-L, |H - C_prev|, |L - C_prev|).
    First ATR = simple mean of first N TR values; subsequent values use
    ATR_t = (ATR_{t-1} * (N-1) + TR_t) / N.
"""

from __future__ import annotations

import pandas as pd


OHLC_REQUIRED = ["high", "low", "close"]


# ── Validation helpers ───────────────────────────────────────────────────────


def _require_columns(df: pd.DataFrame, cols: list[str], indicator: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"{indicator}: input DataFrame is missing required columns {missing}"
        )


def _require_length(length: int, indicator: str) -> None:
    if not isinstance(length, int) or length < 1:
        raise ValueError(f"{indicator}: length must be a positive int, got {length!r}")


# ── SMA ──────────────────────────────────────────────────────────────────────


def add_sma(
    df: pd.DataFrame, length: int, *, source: str = "close"
) -> pd.DataFrame:
    """
    Append Simple Moving Average of `source` (default: close) over `length` bars.

    New column: `sma_{length}`. The first `length - 1` values are NaN.
    """
    _require_length(length, "SMA")
    _require_columns(df, [source], "SMA")

    out = df.copy()
    out[f"sma_{length}"] = out[source].rolling(window=length, min_periods=length).mean()
    return out


# ── EMA ──────────────────────────────────────────────────────────────────────


def add_ema(
    df: pd.DataFrame, length: int, *, source: str = "close"
) -> pd.DataFrame:
    """
    Append Exponential Moving Average of `source` over `length` bars.

    New column: `ema_{length}`. Seeded with the SMA of the first `length`
    values, so the first `length - 1` values are NaN and ema[length-1] equals
    sma[length-1]. Uses alpha = 2 / (length + 1).
    """
    _require_length(length, "EMA")
    _require_columns(df, [source], "EMA")

    out = df.copy()
    s = out[source]
    col = f"ema_{length}"

    if len(s) < length:
        out[col] = pd.Series([float("nan")] * len(s), index=s.index)
        return out

    alpha = 2.0 / (length + 1)
    seed = s.iloc[:length].mean()

    result = [float("nan")] * (length - 1)
    result.append(float(seed))
    prev = seed
    for x in s.iloc[length:]:
        cur = alpha * x + (1 - alpha) * prev
        result.append(cur)
        prev = cur

    out[col] = pd.Series(result, index=s.index)
    return out


# ── ATR ──────────────────────────────────────────────────────────────────────


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """TR_t = max(H-L, |H - C_prev|, |L - C_prev|). TR_0 = H_0 - L_0."""
    prev_close = close.shift(1)
    hl = high - low
    hc = (high - prev_close).abs()
    lc = (low - prev_close).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    # At index 0 prev_close is NaN → hc/lc are NaN → max is NaN. Fall back to H-L.
    tr.iloc[0] = (high.iloc[0] - low.iloc[0])
    return tr


def add_atr(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """
    Append Wilder's Average True Range over `length` bars.

    New column: `atr_{length}`. First `length - 1` values are NaN.
    First ATR is the simple mean of the first `length` TR values.
    Subsequent: ATR_t = (ATR_{t-1} * (length - 1) + TR_t) / length.
    """
    _require_length(length, "ATR")
    _require_columns(df, OHLC_REQUIRED, "ATR")

    out = df.copy()
    tr = _true_range(out["high"], out["low"], out["close"])
    col = f"atr_{length}"

    n = len(tr)
    if n < length:
        out[col] = pd.Series([float("nan")] * n, index=tr.index)
        return out

    atr_values: list[float] = [float("nan")] * (length - 1)
    first_atr = tr.iloc[:length].mean()
    atr_values.append(float(first_atr))
    prev = first_atr
    for t in tr.iloc[length:]:
        cur = (prev * (length - 1) + t) / length
        atr_values.append(cur)
        prev = cur

    out[col] = pd.Series(atr_values, index=tr.index)
    return out
