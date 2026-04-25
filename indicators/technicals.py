"""
Technical indicator wrappers (Phase 3 / Phase 10.F2).

Scope: the minimum set needed for the MVP strategy (SMA crossover, Phase 4),
the ATR-based stop-loss in Phase 6 Risk, and the Regime Detector (Phase 10.F2).

Currently provided:
  - add_sma(df, length)   → df with 'sma_{length}'                 column appended
  - add_ema(df, length)   → df with 'ema_{length}'                 column appended
  - add_atr(df, length)   → df with 'atr_{length}'                 column appended (Wilder/RMA)
  - add_rsi(df, length)   → df with 'rsi_{length}'                 column appended (Wilder/RMA)
  - add_adx(df, length)   → df with 'adx_{length}', 'plus_di_{length}',
                                     'minus_di_{length}'           columns appended

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
  - RSI: Wilder's Relative Strength Index. Uses RMA (same as ATR) to smooth
    average gains and losses. First avg_gain/avg_loss = simple mean of first
    N changes; subsequent use RMA recurrence. RSI = 100 - 100/(1 + RS)
    where RS = avg_gain / avg_loss.
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


# ── RSI ──────────────────────────────────────────────────────────────────────


def add_rsi(
    df: pd.DataFrame, length: int = 14, *, source: str = "close"
) -> pd.DataFrame:
    """
    Append Wilder's Relative Strength Index over `length` bars.

    New column: `rsi_{length}`. The first `length` values are NaN (we need
    `length` price changes, which requires `length + 1` prices, so the first
    usable RSI is at index `length`).

    Smoothing uses Wilder's RMA (same as ATR):
      avg_gain_t = (avg_gain_{t-1} * (length - 1) + gain_t) / length
      avg_loss_t = (avg_loss_{t-1} * (length - 1) + loss_t) / length
      RS = avg_gain / avg_loss
      RSI = 100 - 100 / (1 + RS)

    When avg_loss == 0, RSI = 100 (all gains). When avg_gain == 0, RSI = 0.
    """
    _require_length(length, "RSI")
    _require_columns(df, [source], "RSI")

    out = df.copy()
    s = out[source]
    col = f"rsi_{length}"

    n = len(s)
    if n < length + 1:
        out[col] = pd.Series([float("nan")] * n, index=s.index)
        return out

    deltas = s.diff()  # deltas[0] is NaN

    gains = deltas.clip(lower=0)
    losses = (-deltas).clip(lower=0)

    # Seed: simple mean of first `length` changes (indices 1..length)
    avg_gain = float(gains.iloc[1 : length + 1].mean())
    avg_loss = float(losses.iloc[1 : length + 1].mean())

    rsi_values: list[float] = [float("nan")] * length

    # RSI at index `length`
    if avg_loss == 0:
        rsi_values.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_values.append(100.0 - 100.0 / (1.0 + rs))

    # Remaining bars
    for i in range(length + 1, n):
        avg_gain = (avg_gain * (length - 1) + float(gains.iloc[i])) / length
        avg_loss = (avg_loss * (length - 1) + float(losses.iloc[i])) / length
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - 100.0 / (1.0 + rs))

    out[col] = pd.Series(rsi_values, index=s.index)
    return out


# ── ADX ──────────────────────────────────────────────────────────────────────


def _wilder_rma(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's smoothed moving average (RMA).
    First value = simple mean of first `length` elements.
    Subsequent: RMA_t = (RMA_{t-1} * (length - 1) + x_t) / length.
    Identical to the smoothing used in add_atr().
    """
    n = len(series)
    if n < length:
        return pd.Series([float("nan")] * n, index=series.index)

    result: list[float] = [float("nan")] * (length - 1)
    seed = float(series.iloc[:length].mean())
    result.append(seed)
    prev = seed
    for x in series.iloc[length:]:
        cur = (prev * (length - 1) + float(x)) / length
        result.append(cur)
        prev = cur
    return pd.Series(result, index=series.index)


def add_adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """
    Append Wilder's Average Directional Index and directional indicators.

    New columns:
      adx_{length}       — trend strength (0–100); ≥25 → trending, <20 → ranging
      plus_di_{length}   — positive directional indicator
      minus_di_{length}  — negative directional indicator

    The first (2 * length - 1) ADX values are NaN (length bars needed for
    smoothed +DM/-DM/TR, then another length bars for the ADX smoothing).

    Algorithm (Wilder, 1978):
      +DM = max(high - prev_high, 0) when it exceeds -DM, else 0
      -DM = max(prev_low - low, 0)   when it exceeds +DM, else 0
      TR  = max(H-L, |H-C_prev|, |L-C_prev|)   (same as add_atr)
      Smooth +DM, -DM, TR with Wilder's RMA(length)
      +DI = 100 * smooth(+DM) / smooth(TR)
      -DI = 100 * smooth(-DM) / smooth(TR)
      DX  = 100 * |+DI - -DI| / (+DI + -DI)
      ADX = Wilder's RMA(length) applied to DX
    """
    _require_length(length, "ADX")
    _require_columns(df, OHLC_REQUIRED, "ADX")

    out = df.copy()
    high  = out["high"]
    low   = out["low"]
    close = out["close"]

    # Directional movement (+DM and -DM).
    up_move   = high.diff()    # high_t - high_{t-1}
    down_move = -low.diff()    # low_{t-1} - low_t

    plus_dm  = up_move.where((up_move > down_move) & (up_move > 0), 0.0).fillna(0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0).fillna(0.0)

    # True range (reuse existing helper).
    tr = _true_range(high, low, close)

    # Wilder smooth all three series.
    smooth_plus_dm  = _wilder_rma(plus_dm,  length)
    smooth_minus_dm = _wilder_rma(minus_dm, length)
    smooth_tr       = _wilder_rma(tr,       length)

    # Directional indicators.
    plus_di  = 100.0 * smooth_plus_dm  / smooth_tr
    minus_di = 100.0 * smooth_minus_dm / smooth_tr

    # DX — undefined when +DI + -DI = 0 (flat market); treat as 0.
    di_sum  = plus_di + minus_di
    di_diff = (plus_di - minus_di).abs()
    dx = (100.0 * di_diff / di_sum).where(di_sum > 0, other=0.0)

    # ADX = Wilder RMA of DX.
    adx = _wilder_rma(dx, length)

    out[f"adx_{length}"]      = adx
    out[f"plus_di_{length}"]  = plus_di
    out[f"minus_di_{length}"] = minus_di
    return out
