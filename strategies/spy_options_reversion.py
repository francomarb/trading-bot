"""
SPY Options RSI Reversion Strategy.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from loguru import logger

from indicators.technicals import add_rsi
from strategies.base import BaseStrategy, OptionTradeRejected, OrderType, SignalFrame
from utils.iv_proxy import IVProxyResolver, IVRankSnapshot
from utils.options_lookup import ContractPick, build_opra_quote_lookup, find_best_call
from utils.options_ranker import CallRankerConfig

_ET = ZoneInfo("America/New_York")
_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

__all__ = ["OptionTradeRejected", "SPYOptionsConfig", "SPYOptionsReversionStrategy"]


def _coerce_numeric(value_or_callable) -> float:
    """Return a float from a numeric attribute or a zero-arg pricing method."""
    value = value_or_callable() if callable(value_or_callable) else value_or_callable
    return float(value)


@dataclass(frozen=True)
class SPYOptionsConfig:
    """Tunables for the single-leg SPY call reversion strategy."""

    min_dte: int = 14
    max_dte: int = 28
    target_delta: float = 0.55
    target_strike_pct: float = 0.995
    take_profit_multiple: float = 3.00
    stop_loss_multiple: float = 0.75
    time_stop_expiry_week_weekday: int = 2  # Wednesday
    time_stop_hour: int = 15
    time_stop_minute: int = 30
    delta_floor: float = 0.30
    risk_free_rate: float = 0.05
    ranker_config: CallRankerConfig = field(default_factory=CallRankerConfig)


class SPYOptionsReversionStrategy(BaseStrategy):
    name = "spy_options_reversion"
    preferred_order_type = OrderType.LIMIT

    def __init__(
        self,
        rsi_length: int = 14,
        rsi_threshold: float = 30,
        *,
        trail_activation_pct: float = 0.10,
        trail_pct: float = 0.15,
        edge_filter=None,
        quote_lookup=None,
        config: SPYOptionsConfig | None = None,
        iv_resolver: IVProxyResolver | None = None,
    ):
        super().__init__(edge_filter=edge_filter)
        self.config = config or SPYOptionsConfig()
        self.rsi_length = rsi_length
        self.rsi_threshold = rsi_threshold
        self.trail_activation_pct = trail_activation_pct
        self.trail_pct = trail_pct
        # Shared IV proxy data layer — consolidated under PLAN 11.46 to remove
        # the private VIX fetch path. Production wiring (forward_test.py)
        # injects one resolver shared with the credit-spread filter; tests
        # pass a stub. The resolver's own daily cache + fallback handle the
        # VIX miss path that the deleted ``_vix_date`` / ``_vix_sigma`` /
        # ``vix_fallback_sigma`` machinery used to cover.
        self._iv_resolver = iv_resolver or IVProxyResolver()
        # Trailing stop state keyed by OCC symbol.
        self._position_hwm: dict[str, float] = {}   # OCC → highest premium observed
        self._position_base: dict[str, float] = {}  # OCC → actual entry premium
        # Quote lookup: a callable resolving OCC symbols to live quotes.
        # An explicit lookup may be injected (tests use this for stubs; a
        # future cross-strategy wiring could share a single
        # OptionHistoricalDataClient across options strategies). The default
        # builds a per-instance lookup lazily on first build_option_execution
        # call and caches it on `self`, so importing this module never
        # requires Alpaca credentials and subsequent entries reuse the same
        # client instead of churning one per signal bar.
        self._quote_lookup = quote_lookup

    def required_bars(self) -> int:
        return self.rsi_length + 5

    def register_fill(self, occ: str, fill_premium: float) -> None:
        """Anchor the trailing-stop activation base to the actual fill premium.

        Called by the engine on a confirmed option-buy fill. Without this
        hook, ``_position_base`` is seeded lazily from the first
        ``inspect_open_positions`` Black-Scholes valuation, which can drift
        from real cost basis whenever (a) the underlying has moved between
        fill and the first cycle, or (b) the daily-cached VIX sigma differs
        from intraday IV. The trailing-stop activation threshold reads off
        ``_position_base``, so an inaccurate base shifts when the trail
        engages relative to the position's true entry cost.

        Idempotent w.r.t. ``_position_hwm``: if a value has already been
        observed (race with the first cycle), the higher of fill premium or
        existing HWM is kept. Restored positions (engine restart) never
        receive this call; they fall back to the lazy seeding path.
        """
        if fill_premium is None or fill_premium <= 0:
            return
        self._position_base[occ] = fill_premium
        self._position_hwm[occ] = max(
            self._position_hwm.get(occ, fill_premium), fill_premium
        )

    def restore_trailing_state(
        self, occ: str, *, entry_premium: float, hwm_premium: float
    ) -> None:
        """Rehydrate trailing-stop state from durable storage after restart.

        Called by the engine when reconciling option positions whose
        in-memory high-water mark was lost. Never lowers an existing
        in-memory HWM.
        """
        if entry_premium is not None and entry_premium > 0:
            self._position_base[occ] = entry_premium
        if hwm_premium is not None and hwm_premium > 0:
            self._position_hwm[occ] = max(
                self._position_hwm.get(occ, hwm_premium), hwm_premium
            )

    # ── Signal generation ────────────────────────────────────────────────────

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        false_series = pd.Series(False, index=df.index)
        if len(df) < self.required_bars():
            return SignalFrame(entries=false_series, exits=false_series)

        df = add_rsi(df, self.rsi_length)
        rsi = df[f"rsi_{self.rsi_length}"]

        prev_rsi = rsi.shift(1)
        entries = (prev_rsi < self.rsi_threshold) & (rsi >= self.rsi_threshold)

        # Exits are handled entirely by inspect_open_positions (time stop +
        # Delta floor).  No time-based exit series here — _raw_signals has no
        # access to the specific contract's expiry date.
        return SignalFrame(entries=entries, exits=false_series)

    # ── Mid-trade exit guards ────────────────────────────────────────────────

    @staticmethod
    def _position_premium(position) -> float | None:
        """Read the current option premium from Alpaca's position snapshot."""
        current = getattr(position, "current_price", None)
        if current is not None:
            try:
                premium = float(current)
                if premium > 0:
                    return premium
            except (TypeError, ValueError):
                pass
        try:
            qty = abs(float(getattr(position, "qty", 0.0) or 0.0))
            market_value = abs(float(getattr(position, "market_value", 0.0) or 0.0))
        except (TypeError, ValueError):
            return None
        if qty <= 0 or market_value <= 0:
            return None
        return market_value / (qty * 100.0)

    def inspect_open_positions(self, position, latest_close: float) -> bool:
        """
        Called every engine cycle for each open position.  Returns True to
        trigger an immediate market exit.  Three guards run in order:

        1. Time stop      — exit by the configured day/time of the contract's
                            expiry week.  Prevents holding through Theta cliff.
        2. Delta floor    — exit if B-S Delta < configured floor.  Signals the option has
                            moved too far OTM to be a viable play.
        3. Trailing stop  — activate once the broker premium rises by
                            trail_activation_pct above entry; then exit if the
                            broker premium drops trail_pct below the durable HWM.
                            Lets winners run while protecting open profits.
        """
        match = _OCC_RE.match(position.symbol)
        if not match or match.group(3) != "C":
            return False

        occ = position.symbol
        expiry_date = datetime.strptime(match.group(2), "%y%m%d").date()
        strike = float(match.group(4)) / 1000.0

        # ── Guard 1: time stop ───────────────────────────────────────────────
        # Exit on or after the Wednesday of expiry week at 3:30 PM ET.
        now_et = datetime.now(_ET)
        cfg = self.config
        days_before_expiry = (expiry_date.weekday() - cfg.time_stop_expiry_week_weekday) % 7
        stop_date = expiry_date - timedelta(days=days_before_expiry)
        stop_minutes = cfg.time_stop_hour * 60 + cfg.time_stop_minute
        at_or_past_stop_time = now_et.hour * 60 + now_et.minute >= stop_minutes
        if now_et.date() >= stop_date and at_or_past_stop_time:
            logger.warning(
                f"[{self.name}] Time stop: {occ} — "
                f"expiry-week stop reached ({now_et.strftime('%a %Y-%m-%d %H:%M ET')})"
            )
            self._position_hwm.pop(occ, None)
            self._position_base.pop(occ, None)
            self._log_ivr_observation("exit_time_stop", occ)
            return True

        # ── Guards 2 + 3: B-S valuation ─────────────────────────────────────
        # T = time to options market close (4 PM ET on expiry date) in years.
        expiry_close_et = datetime.combine(expiry_date, time(16, 0), tzinfo=_ET)
        t_days = (expiry_close_et - datetime.now(timezone.utc)).total_seconds() / 86400.0
        T = max(t_days / 365.0, 0.001)

        sigma = self._fetch_vix()

        try:
            from blackscholes import BlackScholesCall
            call = BlackScholesCall(
                S=latest_close, K=strike, T=T, r=cfg.risk_free_rate, sigma=sigma
            )
            delta = call.delta()
            opt_val = _coerce_numeric(call.price)
            broker_premium = self._position_premium(position)

            logger.debug(
                f"[{self.name}] {occ} Delta={delta:.3f} "
                f"broker_premium={broker_premium!r} theoretical=${opt_val:.2f} "
                f"(S={latest_close:.2f}, K={strike:.2f}, T={T:.4f}y, σ={sigma:.2f})"
            )

            # ── Guard 2: Delta floor ─────────────────────────────────────────
            if delta < cfg.delta_floor:
                logger.warning(
                    f"[{self.name}] Delta floor: {occ} — "
                    f"Delta={delta:.3f} < {cfg.delta_floor:.2f}, exiting"
                )
                self._position_hwm.pop(occ, None)
                self._position_base.pop(occ, None)
                self._log_ivr_observation("exit_delta_floor", occ)
                return True

            # ── Guard 3: trailing stop ───────────────────────────────────────
            if broker_premium is None:
                logger.warning(
                    f"[{self.name}] {occ}: broker premium unavailable; "
                    "skipping software trailing-stop evaluation this cycle"
                )
                return False
            observed_premium = broker_premium
            if occ not in self._position_base:
                self._position_base[occ] = observed_premium
            self._position_hwm[occ] = max(
                self._position_hwm.get(occ, observed_premium),
                observed_premium,
            )

            base = self._position_base[occ]
            hwm = self._position_hwm[occ]

            if hwm >= base * (1.0 + self.trail_activation_pct):
                trail_floor = hwm * (1.0 - self.trail_pct)
                logger.debug(
                    f"[{self.name}] {occ} trailing stop active — "
                    f"premium={observed_premium:.2f} "
                    f"hwm={hwm:.2f} floor={trail_floor:.2f}"
                )
                if observed_premium < trail_floor:
                    logger.warning(
                        f"[{self.name}] Trailing stop: {occ} — "
                        f"premium={observed_premium:.2f} < floor={trail_floor:.2f} "
                        f"(hwm={hwm:.2f}, activation={self.trail_activation_pct:.0%}, "
                        f"trail={self.trail_pct:.0%})"
                    )
                    self._position_hwm.pop(occ, None)
                    self._position_base.pop(occ, None)
                    self._log_ivr_observation("exit_trailing_stop", occ)
                    return True

        except Exception as e:
            logger.error(f"[{self.name}] B-S valuation failed for {occ}: {e}")

        return False

    def _fetch_vix(self) -> float:
        """Return today's VIX as a decimal sigma (e.g. 0.18 for VIX=18).

        Thin wrapper over the shared ``IVProxyResolver``: the resolver does
        its own daily cache + stale-cache reuse + fallback (PLAN 11.29 +
        11.46), so this method just divides the index-points scalar by 100
        to land in the Black-Scholes sigma convention.
        """
        return self._iv_resolver.resolve("vix") / 100.0

    # ── IVR observation logging (11.46 — zero-behavior-change) ───────────────

    def _log_ivr_observation(self, event: str, symbol: str) -> None:
        """Emit a structured ``SPY_OPTIONS_IVR`` log line for paper-watch
        evidence accumulation (PLAN 11.46b decision input).

        Pure side-effect — never gates behavior, never raises, **and never
        blocks a critical decision path on the network**: the resolver is
        called with ``cache_only=True`` so a cold cache cannot stall an exit
        (e.g. time stop) on a synchronous yfinance fetch. On a cold cache
        the snapshot reports ``sufficient=False`` / ``lookback_days_used=0``
        and the observation log still lands with that shape — the audit can
        distinguish "no data yet" from a real signal. A failure in the
        resolver path is swallowed at warning level.
        """
        try:
            snap: IVRankSnapshot = self._iv_resolver.resolve_rank(
                "vix", cache_only=True
            )
            rank_str = f"{snap.rank:.4f}" if snap.rank is not None else "None"
            pct_str = (
                f"{snap.percentile:.4f}" if snap.percentile is not None else "None"
            )
            logger.info(
                f"SPY_OPTIONS_IVR {event} symbol={symbol} "
                f"rank={rank_str} percentile={pct_str} "
                f"current={snap.current:.2f} sufficient={snap.sufficient} "
                f"as_of={snap.as_of.isoformat()}"
            )
        except Exception as e:
            logger.warning(
                f"[{self.name}] SPY_OPTIONS_IVR {event} symbol={symbol} "
                f"— resolver failed: {e}"
            )

    # ── Option execution ─────────────────────────────────────────────────────

    def build_option_execution(
        self,
        symbol: str,
        underlying_price: float,
        *,
        notional_cap: float,
    ) -> Tuple[str, float, float, float]:
        """
        Pick the best-scoring affordable call contract and price it at midpoint.

        Selection uses ``utils.options_ranker``: quotes the top-5 strike-nearest
        candidates and scores each on (strike proximity, spread quality,
        premium efficiency). Hard filters drop unaffordable, broken-quote,
        outrageously-spread (>10%), or premium-outlier contracts.

        ``notional_cap`` is the per-position dollar budget from the allocator
        and is passed straight through to the ranker as ``max_premium_per_contract``.
        The ranker computes each candidate's per-contract cost as ``mid × 100``
        (standard equity option multiplier) and rejects candidates whose
        per-contract cost exceeds that dollar budget. No conversion is needed
        because the budget and the cost are both in the same dollars-per-contract
        units. Do NOT divide ``notional_cap`` by 100 — that would shrink the
        budget 100× and reject nearly every contract.

        Returns (occ_symbol, limit_price, take_profit, stop_loss).
        Raises ``OptionTradeRejected`` if no contract survives.
        """
        if notional_cap is None or notional_cap <= 0:
            raise OptionTradeRejected(
                f"{symbol}: notional_cap=${notional_cap} — sleeve has no room."
            )

        # Pass-through: notional_cap is the dollar budget for one position, and
        # the ranker compares mid*100 (per-contract cost) against this same
        # dollar figure. No /100 conversion — see docstring.
        max_premium_per_contract = notional_cap

        if self._quote_lookup is None:
            # Lazy production default — build once on first use and reuse
            # across subsequent entries (one OptionHistoricalDataClient
            # instead of one per signal bar).
            self._quote_lookup = build_opra_quote_lookup()

        pick: ContractPick | None = find_best_call(
            symbol,
            underlying_price,
            min_dte=self.config.min_dte,
            max_dte=self.config.max_dte,
            target_delta=self.config.target_delta,
            target_strike_pct=self.config.target_strike_pct,
            ranker_config=self.config.ranker_config,
            max_premium_per_contract=max_premium_per_contract,
            quote_lookup=self._quote_lookup,
        )
        if pick is None:
            raise OptionTradeRejected(
                f"No tradeable option contract for {symbol} "
                f"(budget ${max_premium_per_contract:,.0f}/contract)."
            )

        premium = pick.premium
        if premium <= 0:
            raise OptionTradeRejected(
                f"{pick.occ_symbol}: computed premium={premium:.2f} <= 0 — skipping trade."
            )

        # Hard SL and TP are configured from backtest-validated defaults.
        # the trailing stop in inspect_open_positions handles real profit-taking.
        take_profit = round(premium * self.config.take_profit_multiple, 2)
        stop_loss = round(premium * self.config.stop_loss_multiple, 2)

        logger.info(
            f"[{self.name}] {pick.occ_symbol}: premium=${premium:.2f} "
            f"spread={pick.spread_pct:.1%} score={pick.score:.2f} "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f} (safety valve)"
        )

        # 11.46 — observation-only IVR logging at entry. Pure evidence
        # accumulation for the 11.46b paper-watch verdict; no behavior gate.
        self._log_ivr_observation("entry", pick.occ_symbol)

        return pick.occ_symbol, premium, take_profit, stop_loss
