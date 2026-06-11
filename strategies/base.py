"""
Strategy framework (Phase 4).

Defines the abstract `BaseStrategy` contract that every concrete strategy must
implement, plus the shared `SignalFrame` return type and `OrderType` enum.

Design principles (from CLAUDE.md + PLAN.md):

  1. **Pure functions of data.** A strategy is a deterministic transform from
     a bars DataFrame to a pair of boolean Series (entries / exits). No
     network calls, no broker state, no hidden randomness, no reading the
     wall clock.

  2. **vectorbt-native convention.** Signals are *separate* `entries` and
     `exits` boolean Series, not a conflated {-1, 0, 1} column. This scales
     to strategies with variable conviction, multiple positions, and
     directly consumes by the Phase 5 vectorbt backtester.

  3. **No look-ahead.** A signal at bar t is computed from data available at
     t's close only (pandas `rolling` / `shift` constructs naturally respect
     this). The Phase 5 backtester is responsible for shifting execution to
     the *next* bar's open — strategies emit signals aligned to the bar
     whose close triggered them.

  4. **Composable edge filters.** A strategy can be constructed with an
     optional callable edge filter that gates entries. The preferred contract
     is `EdgeFilterDecision`; plain `pd.Series[bool]` remains supported only
     as a compatibility path while legacy filters are phased out. This is the
     minimal "regime awareness" hook: e.g. only emit long entries when
     SPY > 200-day MA. Full regime detection is Phase 11.

  5. **Strategy declares its preferred order type.** Trend/breakout
     strategies prefer marketable orders; mean-reversion prefers limit. The
     Phase 7 execution layer reads this attribute and routes accordingly.
     Hard-risk exits (stop-outs) always override with market orders.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Union

import pandas as pd

if TYPE_CHECKING:
    from data.watchlists import WatchlistSource


# ── Shared types ─────────────────────────────────────────────────────────────


class OptionTradeRejected(ValueError):
    """Expected option-entry veto raised by any options-buying strategy.

    Lives here (not inside a specific strategy module) because the engine
    catches it across strategies and a second options-buying strategy must
    be able to raise the same type without importing from a sibling module.
    """


class MultiLegTradeRejected(ValueError):
    """Expected multi-leg option-entry veto raised by spread-style strategies."""


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    # Stop-limit: broker triggers a limit at `entry_trigger_price`, with the
    # limit capped at `entry_max_price`. Used by price-level breakout
    # strategies (Donchian) per PLAN 11.47 to delegate the intraday trigger
    # to the broker while keeping the chase cap structurally enforced.
    STOP_LIMIT = "stop_limit"


@dataclass(frozen=True)
class SignalFrame:
    """
    The output of `BaseStrategy.generate_signals`.

    - `entries[t] == True` means: at bar t's close, open a new long position.
      Execution happens on bar t+1's open (responsibility of the engine /
      backtester, not the strategy).
    - `exits[t] == True` means: at bar t's close, close any open position.

    Both Series share the same DatetimeIndex as the input bars.
    """

    entries: pd.Series
    exits: pd.Series

    def __post_init__(self) -> None:
        if not self.entries.index.equals(self.exits.index):
            raise ValueError("entries and exits must share the same index")
        if self.entries.dtype != bool or self.exits.dtype != bool:
            raise ValueError("entries and exits must be boolean Series")


EdgeFilter = Callable[[pd.DataFrame], Union[pd.Series, "EdgeFilterDecision"]]
"""Callable edge-filter contract.

Preferred return type: ``EdgeFilterDecision``.
Compatibility return type: ``pd.Series[bool]``.

Deprecation note:
    Returning a plain boolean Series is the legacy authoring style and is
    being phased out for first-party filters. It remains supported during the
    migration so existing filters and simple external filters continue to work.
"""


@dataclass(frozen=True)
class EdgeFilterDecision:
    """
    Normalized edge-filter result.

    New filters that have meaningful operator-facing diagnostics should return
    this type directly. Legacy filters may still return ``pd.Series[bool]`` and
    are normalized centrally by ``BaseStrategy`` as a compatibility path.

    Deprecation note:
        ``pd.Series[bool]`` remains supported for older filters, but it is no
        longer the preferred contract for first-party filters because it cannot
        express structured block reasons on its own.
    """

    allowed: pd.Series
    reasons: pd.Series

    def __post_init__(self) -> None:
        if not self.allowed.index.equals(self.reasons.index):
            raise ValueError("allowed and reasons must share the same index")
        if self.allowed.dtype != bool:
            raise ValueError("allowed must be a boolean Series")

        normalized = []
        for allowed, raw_reasons in zip(
            self.allowed.tolist(), self.reasons.tolist(), strict=False
        ):
            if raw_reasons is None:
                reason_list: list[str] = []
            elif isinstance(raw_reasons, str):
                reason_list = [raw_reasons]
            else:
                reason_list = [str(reason) for reason in list(raw_reasons)]
            normalized.append([] if allowed else reason_list)
        object.__setattr__(
            self,
            "reasons",
            pd.Series(normalized, index=self.reasons.index, dtype=object),
        )

    @classmethod
    def allow_all(cls, index: pd.Index) -> "EdgeFilterDecision":
        """Return an all-pass decision over ``index``."""
        return cls(
            allowed=pd.Series(True, index=index, dtype=bool),
            reasons=pd.Series([[] for _ in range(len(index))], index=index, dtype=object),
        )

    @classmethod
    def from_bool_series(
        cls,
        allowed: pd.Series,
        *,
        blocked_reasons: list[str] | None = None,
    ) -> "EdgeFilterDecision":
        """
        Build a normalized decision from a boolean gate.

        ``blocked_reasons`` is a compatibility helper for simple filters whose
        latest block reason is known but which do not yet emit structured
        reasons per bar.

        Deprecation note:
            This constructor exists to support the legacy boolean-only filter
            path during migration. New filters should return
            ``EdgeFilterDecision`` directly instead of relying on this adapter.
        """
        blocked = list(blocked_reasons or [])
        reasons = [
            [] if bool(ok) else list(blocked)
            for ok in allowed.tolist()
        ]
        return cls(
            allowed=allowed.astype(bool),
            reasons=pd.Series(reasons, index=allowed.index, dtype=object),
        )

    def reindex(self, index: pd.Index) -> "EdgeFilterDecision":
        """Align the decision to ``index`` and fail closed on missing bars."""
        reindexed_allowed = self.allowed.reindex(index)
        allowed_values = [bool(v) if pd.notna(v) else False for v in reindexed_allowed]

        reindexed_reasons = self.reasons.reindex(index)
        reasons_values = []
        for allowed, raw_reasons in zip(
            allowed_values, reindexed_reasons.tolist(), strict=False
        ):
            if raw_reasons is None:
                normalized = []
            elif isinstance(raw_reasons, str):
                normalized = [raw_reasons]
            else:
                normalized = [str(reason) for reason in list(raw_reasons)]
            reasons_values.append([] if allowed else normalized)
        return EdgeFilterDecision(
            allowed=pd.Series(allowed_values, index=index, dtype=bool),
            reasons=pd.Series(reasons_values, index=index, dtype=object),
        )

    @property
    def latest_allowed(self) -> bool:
        """Latest-bar allow/block state."""
        if self.allowed.empty:
            return False
        return bool(self.allowed.iloc[-1])

    @property
    def latest_reasons(self) -> list[str]:
        """Latest-bar block reasons."""
        if self.reasons.empty:
            return []
        return list(self.reasons.iloc[-1] or [])

    def and_with(self, other: "EdgeFilterDecision") -> "EdgeFilterDecision":
        """Logical AND composition that preserves all blocking reasons."""
        if not self.allowed.index.equals(other.allowed.index):
            raise ValueError("cannot compose EdgeFilterDecision with different indexes")

        allowed = self.allowed & other.allowed
        reasons = []
        for left_ok, left_reasons, right_ok, right_reasons in zip(
            self.allowed.tolist(),
            self.reasons.tolist(),
            other.allowed.tolist(),
            other.reasons.tolist(),
            strict=False,
        ):
            merged: list[str] = []
            if not left_ok:
                merged.extend(list(left_reasons or []))
            if not right_ok:
                merged.extend(list(right_reasons or []))
            reasons.append(merged)
        return EdgeFilterDecision(
            allowed=allowed.astype(bool),
            reasons=pd.Series(reasons, index=self.allowed.index, dtype=object),
        )


def normalize_edge_filter_result(
    result: pd.Series | EdgeFilterDecision,
    index: pd.Index,
    *,
    legacy_blocked_reasons: list[str] | None = None,
) -> EdgeFilterDecision:
    """
    Normalize edge-filter output to ``EdgeFilterDecision``.

    This keeps ``pd.Series`` support as a compatibility path while making the
    richer structured contract the repo standard for new filters.

    Deprecation note:
        Plain boolean ``pd.Series`` output is the old filter contract. It is
        still accepted here so existing filters keep working during rollout,
        but new first-party filters should return ``EdgeFilterDecision``.
    """
    if isinstance(result, EdgeFilterDecision):
        return result.reindex(index)
    if not isinstance(result, pd.Series):
        raise TypeError(
            f"edge_filter must return a pd.Series or EdgeFilterDecision, got {type(result).__name__}"
        )
    reindexed = result.reindex(index)
    normalized = pd.Series(
        [bool(v) if pd.notna(v) else False for v in reindexed],
        index=index,
        dtype=bool,
    )
    return EdgeFilterDecision.from_bool_series(
        normalized,
        blocked_reasons=legacy_blocked_reasons,
    )


# ── Base class ───────────────────────────────────────────────────────────────


class BaseStrategy(ABC):
    """
    Abstract strategy.

    Subclasses must set `name` and `preferred_order_type` as class attributes
    and implement `_raw_signals(df)`. The public `generate_signals(df)` applies
    any configured edge filter on top.
    """

    name: str  # concrete subclasses must override
    preferred_order_type: OrderType = OrderType.MARKET

    def __init__(self, *, edge_filter: EdgeFilter | None = None) -> None:
        self._edge_filter = edge_filter

    def required_bars(self) -> int:
        """Minimum number of bars needed to generate a valid signal.

        Subclasses should override this if they need more than 50 bars
        (e.g. a strategy using a 200-day SMA needs at least 200).  The
        engine uses this to determine how much history to fetch for each
        strategy slot.
        """
        return 50

    def inspect_open_positions(self, position, latest_close: float) -> bool:
        """
        Hook called by the engine during the cycle loop to allow the strategy
        to evaluate an emergency exit condition mid-trade.
        Returns True to trigger an immediate market exit.
        """
        return False

    def latest_trigger_price(self, df: pd.DataFrame) -> float | None:
        """
        For STOP_LIMIT strategies: the broker-side stop trigger price at the
        latest bar. Default is None; subclasses that use STOP_LIMIT must
        override (Donchian: prior-N-day high). Engine fails the entry if
        STOP_LIMIT is declared but no trigger price is returned.
        """
        return None

    def trigger_prices(self, df: pd.DataFrame) -> pd.Series | None:
        """
        For STOP_LIMIT strategies: the per-bar trigger price series, indexed
        identically to df. Default is None; subclasses that use STOP_LIMIT
        and need backtest parity must override (Donchian: the prior-N-day
        high series). Used by the backtest runner to simulate stop-limit
        entry semantics — without it, a backtest of a STOP_LIMIT strategy
        falls back to next-bar-open fills and silently diverges from
        production. PLAN 11.47 backtest parity.
        """
        return None

    # Concrete strategies implement this.
    @abstractmethod
    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        """Compute entries/exits before edge-filter gating."""

    def _aligned_edge_gate(
        self, df: pd.DataFrame, *, symbol: str = ""
    ) -> EdgeFilterDecision | None:
        """Return the normalized edge-filter decision for ``df``, if configured."""
        if self._edge_filter is None:
            return None
        # Inject symbol into filters that declare set_symbol().
        if symbol and hasattr(self._edge_filter, "set_symbol"):
            self._edge_filter.set_symbol(symbol)

        result = self._edge_filter(df)
        getter = getattr(self._edge_filter, "get_last_block_reasons", None)
        legacy_blocked_reasons = list(getter() or []) if callable(getter) else None
        return normalize_edge_filter_result(
            result,
            df.index,
            legacy_blocked_reasons=legacy_blocked_reasons,
        )

    def inspect_signals(
        self, df: pd.DataFrame, *, symbol: str = ""
    ) -> tuple[SignalFrame, SignalFrame, bool | None, list[str]]:
        """
        Return raw signals, filtered signals, and the current edge-filter state.

        The final value is:
          - `True` / `False` when an edge filter exists
          - `None` when the strategy has no edge filter configured
        """
        raw = self._raw_signals(df)
        edge_decision = self._aligned_edge_gate(df, symbol=symbol)
        if edge_decision is None:
            return raw, raw, None, []
        filtered = SignalFrame(
            entries=(raw.entries & edge_decision.allowed),
            exits=raw.exits,
        )
        return (
            raw,
            filtered,
            edge_decision.latest_allowed,
            edge_decision.latest_reasons,
        )

    def generate_signals(self, df: pd.DataFrame, *, symbol: str = "") -> SignalFrame:
        """
        Public entry point. Computes raw signals, then AND-gates entries
        (but not exits — we always want to be able to exit) with the
        edge filter if one is configured.

        `symbol` is passed through to symbol-aware filters (those that
        implement a `set_symbol` method, e.g. EarningsBlackout). Filters
        that don't need the symbol ignore it — backwards compatible.
        """
        _raw, filtered, _edge_allowed, _reasons = self.inspect_signals(df, symbol=symbol)
        return filtered



# ── Scanner ─────────────────────────────────────────────────────────────────


class Scanner(ABC):
    """
    Discovers symbols dynamically at runtime.

    A StrategySlot can optionally hold a Scanner. When present, the engine
    calls `scan()` at the start of each cycle (or on a slower cadence) to
    refresh the slot's symbol list. This allows strategies to trade a
    universe that changes over time — e.g. stocks near a crossover, unusual
    volume, or sector rotation signals.
    """

    @abstractmethod
    def scan(self) -> list[str]:
        """Return the current list of symbols to trade."""


# ── Strategy Slot ───────────────────────────────────────────────────────────


@dataclass
class StrategySlot:
    """
    Binds a strategy to its trading universe.

    The engine holds a list of slots. Each cycle iterates over every slot,
    running the slot's strategy against its symbols. Risk and broker are
    shared across all slots (one account, one equity pool).

    Fields:
        strategy:  A BaseStrategy instance (e.g. SMACrossover, MeanReversion).
        symbols:   Static list of symbols to trade. Ignored if `watchlist_source`
                   or `scanner` is set.
        timeframe: Bar timeframe for this slot (default "1Day").
        watchlist_source: Optional WatchlistSource that supplies the symbol list.
                   Takes precedence over `symbols` and `scanner`. Use
                   StaticWatchlistSource for Phase 10; DynamicWatchlistSource
                   is Phase 11.
        scanner:   Optional Scanner that refreshes `symbols` dynamically.
                   Superseded by `watchlist_source` when both are set.
        scan_interval_seconds: Minimum seconds between scanner invocations.
            Expensive scanners (e.g. screening the entire market) should use
            a longer interval so they don't run every engine cycle.  Defaults
            to 0 (scan every cycle).
        allowed_regimes: frozenset of MarketRegime values (from regime.detector)
            that permit new entries for this slot. None = all regimes allowed
            (legacy / regime-unaware default — entries never blocked by regime).
            Exits are NEVER blocked regardless of this setting.
            Typical values:
              frozenset({MarketRegime.TRENDING, MarketRegime.RANGING})
              — blocks new entries in BEAR and VOLATILE, allows in bull markets.
    """

    strategy: BaseStrategy
    symbols: list[str] = field(default_factory=list)
    timeframe: str = "1Day"
    watchlist_source: "WatchlistSource | None" = None
    scanner: Scanner | None = None
    scan_interval_seconds: float = 0
    allowed_regimes: "frozenset | None" = None  # frozenset[MarketRegime] | None

    # Internal: monotonic timestamp of the last scanner invocation.
    _last_scan_time: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.symbols and self.scanner is None and self.watchlist_source is None:
            raise ValueError(
                "StrategySlot must have symbols, a watchlist_source, or a scanner"
            )

    def active_symbols(self) -> list[str]:
        """Return the current symbol list.

        Precedence: watchlist_source → scanner → symbols.
        """
        if self.watchlist_source is not None:
            return self.watchlist_source.symbols()
        if self.scanner is not None:
            now = time.monotonic()
            if now - self._last_scan_time >= self.scan_interval_seconds:
                self.symbols = self.scanner.scan()
                self._last_scan_time = now
        return self.symbols
