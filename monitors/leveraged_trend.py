"""
Leveraged-ETF trend monitor — 200-day SMA phase state on the UNDERLYING index.

Purpose
-------
Provide the read-only dashboard phase view for leveraged funds (TQQQ, TECL,
...). The phase-in / phase-out decision is driven by the *unleveraged*
underlying's position relative to its 200-day SMA, filtered for noise:

    * phase-OUT signal — underlying closes strictly BELOW its 200-day SMA on
      ``exit_days`` consecutive completed sessions.
    * phase-IN signal  — underlying closes strictly ABOVE its 200-day SMA on
      ``entry_days`` consecutive completed sessions.

Both halves are strict, so a close landing exactly ON the SMA satisfies
neither. It breaks both confirmation runs without changing the phase, and it
is not a crossing — the price reached the line but did not pass through it.

Each underlying is an INDEPENDENT state machine. QQQ and XLK track different
indices and routinely disagree (verified 2026-08-24: QQQ phased in 2026-04-14,
XLK not until 2026-08-10, a four-month divergence).

Scope — this module NEVER trades
--------------------------------
This operator-facing monitor is consumed by ``dashboard.py``. It places no
orders and touches no engine state. Paper execution lives separately in
``strategies/leveraged_trend.py`` and the normal engine/risk/lifecycle path;
keeping this module read-only prevents a dashboard refresh from ever creating
broker side effects.

Design notes
------------
1. **Stateless replay, not a persisted counter.** Phase is recomputed by
   replaying the rule across the whole bar series on every call. A counter
   persisted to disk desynchronises on restarts, missed sessions and cache
   backfills; a replay is a pure function of the bars and cannot drift.

2. **The in-progress daily bar is dropped.** During market hours Alpaca emits
   the current session as a live-updating daily bar. Counting it would let the
   streak flap intraday and fire a phase change that the actual close does not
   support. Same hazard the engine solves in ``TradingEngine._decision_frame``.

3. **SIP feed, not IEX.** The rule keys off the official consolidated close.
   An IEX daily close is one venue's last trade and can differ by a few cents —
   enough to flip a marginal day on a threshold rule. Delayed SIP is free on
   the basic tier and the 15-minute delay is irrelevant to a daily bar read
   after the close.

4. **Equality is a third state, not a tie broken toward one side.** Folding
   "on the line" into "above" would let a run of on-the-line closes confirm a
   phase-IN that the strict rule never granted; folding it into "below" would
   do the mirror image for phase-OUT. Neither direction is defensible, so
   equality resets both runs and holds the phase where it is.

5. **``UNKNOWN`` fails toward staying out.** Insufficient history, a NaN SMA or
   a fetch failure yields ``Phase.UNKNOWN``, which is rendered as "no signal"
   and must never be presented as a phase-IN. There is no automation behind
   this monitor, so the failure mode is a blank cell rather than a bad trade —
   but the asymmetry is deliberate and must survive any future reuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

import pandas as pd
from loguru import logger

from config import settings
from data import fetcher
from indicators.technicals import add_sma

_NEW_YORK_TZ = ZoneInfo("America/New_York")


class Phase(Enum):
    """Confirmed phase state for one underlying."""

    IN = "IN"
    OUT = "OUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PhaseTransition:
    """A confirmed phase change — the session the streak requirement was met."""

    transition_date: date
    phase: Phase
    close: float
    sma: float


@dataclass(frozen=True)
class TrendMonitorState:
    """
    Everything the dashboard renders for one underlying.

    ``below_streak`` / ``above_streak`` are counts of trailing consecutive
    COMPLETED sessions strictly on that side of the SMA. At most one is
    non-zero; a close exactly on the SMA zeroes both, because the rule is
    strict in both directions.

    ``phase_since`` is the date of an OBSERVED transition and is ``None``
    when the phase was seeded from the window rather than confirmed by a
    signal inside it.

    ``observed_from`` is the first session on which a phase could be
    determined at all — the first close on either side of the SMA, which is
    not necessarily the first evaluable bar, since a window can open with
    closes sitting exactly on the line. Callers may present it as a lower
    bound on how long the phase has held. Anchoring that bound at the start
    of the data instead would claim the phase held across bars where no
    phase existed yet. Never render ``observed_from`` as if it were
    ``phase_since``: it is the earliest the phase could be seen, not where
    the signal fired.
    """

    underlying: str
    leveraged: str | None
    last_bar_date: date | None
    close: float | None
    sma: float | None
    dist_pct: float | None
    below_streak: int
    above_streak: int
    sma_length: int
    exit_days: int
    entry_days: int
    phase: Phase
    phase_since: date | None
    sessions_in_phase: int | None
    observed_from: date | None
    last_cross_date: date | None
    sessions_since_cross: int | None
    transitions: tuple[PhaseTransition, ...]
    error: str | None = None

    @property
    def sma_length_label(self) -> str:
        """Display label for the moving average, e.g. 'SMA 200'."""
        return f"SMA {self.sma_length}"

    @property
    def sessions_to_signal(self) -> int | None:
        """
        Completed sessions still required before the phase would flip.

        ``None`` when there is no data, or when the current streak runs in the
        same direction as the phase already held (nothing pending).
        """
        if self.phase is Phase.UNKNOWN:
            return None
        if self.phase is Phase.IN:
            if self.below_streak <= 0:
                return None
            return max(self.exit_days - self.below_streak, 0)
        if self.above_streak <= 0:
            return None
        return max(self.entry_days - self.above_streak, 0)

    @property
    def phase_is_seeded(self) -> bool:
        """
        True when the phase was read off the window's first bar rather than
        confirmed by a signal inside it.

        Callers must not present a seeded phase's age as a confirmed one —
        the phase may have held since long before the window opened.
        """
        return self.phase is not Phase.UNKNOWN and self.phase_since is None

    @property
    def days_since_phase_change(self) -> int | None:
        """
        Calendar days since the confirmed phase change.

        ``None`` for a seeded phase; use :attr:`days_observed` as a lower
        bound in that case.
        """
        if self.phase_since is None or self.last_bar_date is None:
            return None
        return (self.last_bar_date - self.phase_since).days

    @property
    def days_observed(self) -> int | None:
        """Calendar days since the phase first became determinable."""
        if self.observed_from is None or self.last_bar_date is None:
            return None
        return (self.last_bar_date - self.observed_from).days

    @property
    def days_since_cross(self) -> int | None:
        """Calendar days since the underlying last crossed its SMA line."""
        if self.last_cross_date is None or self.last_bar_date is None:
            return None
        return (self.last_bar_date - self.last_cross_date).days


def drop_in_progress_bar(
    df: pd.DataFrame,
    *,
    now: datetime | None = None,
    complete_after_et: str = "16:15",
) -> pd.DataFrame:
    """
    Drop the trailing bar when it represents the current, unfinished session.

    Alpaca emits the in-progress session as a daily bar that updates all day.
    The bar is treated as complete once the New York wall clock passes
    ``complete_after_et`` (default 16:15 — the 16:00 close plus the 15-minute
    delayed-SIP lag), which is why the cutoff is configurable rather than 16:00.

    Weekends, holidays and pre-market are handled implicitly: the trailing bar's
    date only equals the current New York date while a session is under way.

    Known conservatism: on an early-close session (13:00 ET) the bar is complete
    from 13:15 but is withheld until 16:15, so the monitor shows the prior
    session for a few hours. Showing a stale-but-real close beats showing a
    close that has not happened.
    """
    if df.empty:
        return df

    now = now or datetime.now(timezone.utc)
    now_ny = pd.Timestamp(now).tz_convert(_NEW_YORK_TZ)
    latest_ny = pd.Timestamp(df.index[-1]).tz_convert(_NEW_YORK_TZ)

    if latest_ny.date() != now_ny.date():
        return df

    hour, _, minute = complete_after_et.partition(":")
    cutoff = now_ny.replace(
        hour=int(hour), minute=int(minute), second=0, microsecond=0
    )
    if now_ny >= cutoff:
        return df
    return df.iloc[:-1]


def evaluate_series(
    df: pd.DataFrame,
    *,
    underlying: str,
    leveraged: str | None = None,
    sma_length: int = 200,
    exit_days: int = 3,
    entry_days: int = 5,
) -> TrendMonitorState:
    """
    Replay the phase rule over a frame of COMPLETED daily bars.

    Pure — no I/O, no clock. ``df`` must already have the in-progress bar
    removed (see :func:`drop_in_progress_bar`).

    The seed phase is taken from which side of the SMA the first evaluable bar
    sits on, rather than assumed, so a window that opens mid-downtrend does not
    have to manufacture a phase-OUT it already missed. The seed is not reported
    as a transition.
    """
    empty = TrendMonitorState(
        underlying=underlying,
        leveraged=leveraged,
        last_bar_date=None,
        close=None,
        sma=None,
        dist_pct=None,
        below_streak=0,
        above_streak=0,
        sma_length=sma_length,
        exit_days=exit_days,
        entry_days=entry_days,
        phase=Phase.UNKNOWN,
        phase_since=None,
        sessions_in_phase=None,
        observed_from=None,
        last_cross_date=None,
        sessions_since_cross=None,
        transitions=(),
    )

    if df.empty or "close" not in df.columns:
        return TrendMonitorState(**{**empty.__dict__, "error": "no bars"})

    if len(df) < sma_length:
        return TrendMonitorState(
            **{
                **empty.__dict__,
                "last_bar_date": pd.Timestamp(df.index[-1]).date(),
                "close": float(df["close"].iloc[-1]),
                "error": (
                    f"insufficient history: {len(df)} bars, "
                    f"{sma_length} required for SMA{sma_length}"
                ),
            }
        )

    frame = add_sma(df, sma_length)
    sma_col = f"sma_{sma_length}"
    evaluable = frame.dropna(subset=[sma_col])
    if evaluable.empty:
        return TrendMonitorState(
            **{**empty.__dict__, "error": f"SMA{sma_length} is all-NaN"}
        )

    closes = evaluable["close"].astype(float).to_numpy()
    smas = evaluable[sma_col].astype(float).to_numpy()
    dates = [pd.Timestamp(ts).date() for ts in evaluable.index]

    # Three states, not two. Both halves of the rule are strict, so a close
    # exactly on the SMA is neither a breach nor a confirmation: it resets
    # both runs and leaves the phase alone. Folding it into "above" would let
    # on-the-line closes confirm a phase-IN the rule never granted.
    is_below = closes < smas
    is_above = closes > smas

    # The seed is taken from the first bar that sits definitively on one side.
    first_definite: int | None = None
    for idx in range(len(evaluable)):
        if bool(is_below[idx]) or bool(is_above[idx]):
            first_definite = idx
            break

    last = len(evaluable) - 1
    if first_definite is None:
        # Every evaluable close sits exactly on its own SMA — degenerate, but
        # reachable on a synthetic or halted series. There is no side to be on,
        # so there is no phase to report.
        return TrendMonitorState(
            **{
                **empty.__dict__,
                "last_bar_date": dates[last],
                "close": float(closes[last]),
                "sma": float(smas[last]),
                "dist_pct": 0.0,
                # No bar picks a side, so no phase is ever established and
                # there is nothing for a lower bound to anchor to.
                "observed_from": None,
                "error": "every close sits exactly on the SMA",
            }
        )

    phase = Phase.OUT if bool(is_below[first_definite]) else Phase.IN
    # None until the replay OBSERVES a transition. A seeded phase has no
    # confirmed start date; reporting the window's first bar as one would
    # dress the edge of the data up as a signal.
    phase_since_idx: int | None = None
    last_cross_idx: int | None = None
    last_definite_below: bool | None = None
    below_streak = 0
    above_streak = 0
    transitions: list[PhaseTransition] = []

    for idx in range(len(evaluable)):
        below = bool(is_below[idx])
        above = bool(is_above[idx])

        if below:
            below_streak += 1
            above_streak = 0
        elif above:
            above_streak += 1
            below_streak = 0
        else:
            # On the line: breaks both runs, confirms neither.
            below_streak = 0
            above_streak = 0

        # Only definite sides can cross. Touching the line and returning to
        # the same side is not a crossing, so on-the-line bars are skipped
        # rather than counted as a side change.
        if below or above:
            if last_definite_below is not None and below != last_definite_below:
                last_cross_idx = idx
            last_definite_below = below

        if phase is Phase.IN and below_streak >= exit_days:
            phase = Phase.OUT
            phase_since_idx = idx
            transitions.append(
                PhaseTransition(dates[idx], Phase.OUT, closes[idx], smas[idx])
            )
        elif phase is Phase.OUT and above_streak >= entry_days:
            phase = Phase.IN
            phase_since_idx = idx
            transitions.append(
                PhaseTransition(dates[idx], Phase.IN, closes[idx], smas[idx])
            )

    return TrendMonitorState(
        underlying=underlying,
        leveraged=leveraged,
        last_bar_date=dates[last],
        close=float(closes[last]),
        sma=float(smas[last]),
        dist_pct=float(closes[last] / smas[last] - 1.0) * 100.0,
        below_streak=below_streak,
        above_streak=above_streak,
        sma_length=sma_length,
        exit_days=exit_days,
        entry_days=entry_days,
        phase=phase,
        phase_since=(
            dates[phase_since_idx] if phase_since_idx is not None else None
        ),
        sessions_in_phase=(
            last - phase_since_idx if phase_since_idx is not None else None
        ),
        # The first bar that establishes a phase, NOT the first evaluable
        # bar. A window opening with on-the-line closes has no phase until
        # some close picks a side; anchoring the lower bound earlier would
        # claim the phase held while it did not yet exist.
        observed_from=dates[first_definite],
        last_cross_date=dates[last_cross_idx] if last_cross_idx is not None else None,
        sessions_since_cross=(
            last - last_cross_idx if last_cross_idx is not None else None
        ),
        transitions=tuple(transitions),
    )


def load_monitor_state(
    underlying: str,
    leveraged: str | None = None,
    *,
    now: datetime | None = None,
    history_years: int | None = None,
    sma_length: int | None = None,
    exit_days: int | None = None,
    entry_days: int | None = None,
    feed: str | None = None,
) -> TrendMonitorState:
    """
    Fetch bars for one underlying and return its phase state.

    Never raises — a fetch failure returns a ``Phase.UNKNOWN`` state carrying
    the error text, so one dead symbol cannot blank the dashboard panel.
    """
    sma_length = sma_length or settings.LEVERAGED_TREND_SMA_LENGTH
    exit_days = exit_days or settings.LEVERAGED_TREND_EXIT_DAYS
    entry_days = entry_days or settings.LEVERAGED_TREND_ENTRY_DAYS
    history_years = history_years or settings.LEVERAGED_TREND_HISTORY_YEARS
    feed = feed or settings.LEVERAGED_TREND_FEED
    now = now or datetime.now(timezone.utc)

    try:
        df, _stats = fetcher.fetch_symbol(
            underlying,
            start=now - timedelta(days=int(history_years * 365.25)),
            end=now,
            timeframe="1Day",
            feed=feed,
        )
    except Exception as exc:  # noqa: BLE001 — a monitor must never break the page
        logger.warning(
            f"leveraged trend monitor: {underlying} fetch failed: "
            f"{type(exc).__name__}: {exc}"
        )
        return TrendMonitorState(
            underlying=underlying,
            leveraged=leveraged,
            last_bar_date=None,
            close=None,
            sma=None,
            dist_pct=None,
            below_streak=0,
            above_streak=0,
            sma_length=sma_length,
            exit_days=exit_days,
            entry_days=entry_days,
            phase=Phase.UNKNOWN,
            phase_since=None,
            sessions_in_phase=None,
            observed_from=None,
            last_cross_date=None,
            sessions_since_cross=None,
            transitions=(),
            error=f"{type(exc).__name__}: {exc}",
        )

    completed = drop_in_progress_bar(
        df,
        now=now,
        complete_after_et=settings.LEVERAGED_TREND_SESSION_COMPLETE_ET,
    )
    return evaluate_series(
        completed,
        underlying=underlying,
        leveraged=leveraged,
        sma_length=sma_length,
        exit_days=exit_days,
        entry_days=entry_days,
    )


def load_all_monitor_states(
    *,
    now: datetime | None = None,
) -> list[TrendMonitorState]:
    """Phase state for every configured underlying, in configured order."""
    return [
        load_monitor_state(underlying, leveraged, now=now)
        for underlying, leveraged in settings.LEVERAGED_TREND_PAIRS.items()
    ]
