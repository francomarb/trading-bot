"""
SPY Options RSI Reversion Strategy.
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from loguru import logger

from indicators.technicals import add_rsi
from strategies.base import BaseStrategy, SignalFrame, OrderType
from utils.options_lookup import ContractPick, find_best_call
from utils.options_ranker import Quote

_ET = ZoneInfo("America/New_York")
_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


class OptionTradeRejected(ValueError):
    """Expected option-entry veto such as wide spreads or missing quotes."""


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
    ):
        super().__init__(edge_filter=edge_filter)
        self.rsi_length = rsi_length
        self.rsi_threshold = rsi_threshold
        self.trail_activation_pct = trail_activation_pct
        self.trail_pct = trail_pct
        # VIX cache: refreshed once per calendar day to avoid hot-loop HTTP calls.
        self._vix_date: date | None = None
        self._vix_sigma: float = 0.15  # fallback: ~VIX 15
        # Trailing stop state keyed by OCC symbol.
        self._position_hwm: dict[str, float] = {}   # OCC → highest B-S value observed
        self._position_base: dict[str, float] = {}  # OCC → first B-S value observed

    def required_bars(self) -> int:
        return self.rsi_length + 5

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

    def inspect_open_positions(self, position, latest_close: float) -> bool:
        """
        Called every engine cycle for each open position.  Returns True to
        trigger an immediate market exit.  Three guards run in order:

        1. Time stop      — exit by Wednesday 3:30 PM ET of the contract's
                            expiry week.  Prevents holding through Theta cliff.
        2. Delta floor    — exit if B-S Delta < 0.30.  Signals the option has
                            moved too far OTM to be a viable play.
        3. Trailing stop  — activate once the B-S value rises ≥ trail_activation_pct
                            above the entry value; then exit if the current value
                            drops ≥ trail_pct below the highest observed value.
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
        expiry_wednesday = expiry_date - timedelta(days=2)
        at_or_past_330 = now_et.hour * 60 + now_et.minute >= 15 * 60 + 30
        if now_et.date() >= expiry_wednesday and at_or_past_330:
            logger.warning(
                f"[{self.name}] Time stop: {occ} — "
                f"expiry week Wednesday reached ({now_et.strftime('%a %Y-%m-%d %H:%M ET')})"
            )
            self._position_hwm.pop(occ, None)
            self._position_base.pop(occ, None)
            return True

        # ── Guards 2 + 3: B-S valuation ─────────────────────────────────────
        # T = time to options market close (4 PM ET on expiry date) in years.
        expiry_close_et = datetime.combine(expiry_date, time(16, 0), tzinfo=_ET)
        t_days = (expiry_close_et - datetime.now(timezone.utc)).total_seconds() / 86400.0
        T = max(t_days / 365.0, 0.001)

        sigma = self._fetch_vix()

        try:
            from blackscholes import BlackScholesCall
            call = BlackScholesCall(S=latest_close, K=strike, T=T, r=0.05, sigma=sigma)
            delta = call.delta()
            opt_val = float(call.price)

            logger.debug(
                f"[{self.name}] {occ} Delta={delta:.3f} price={opt_val:.2f} "
                f"(S={latest_close:.2f}, K={strike:.2f}, T={T:.4f}y, σ={sigma:.2f})"
            )

            # ── Guard 2: Delta floor ─────────────────────────────────────────
            if delta < 0.30:
                logger.warning(
                    f"[{self.name}] Delta floor: {occ} — Delta={delta:.3f} < 0.30, exiting"
                )
                self._position_hwm.pop(occ, None)
                self._position_base.pop(occ, None)
                return True

            # ── Guard 3: trailing stop ───────────────────────────────────────
            if occ not in self._position_base:
                self._position_base[occ] = opt_val
            self._position_hwm[occ] = max(self._position_hwm.get(occ, opt_val), opt_val)

            base = self._position_base[occ]
            hwm = self._position_hwm[occ]

            if hwm >= base * (1.0 + self.trail_activation_pct):
                trail_floor = hwm * (1.0 - self.trail_pct)
                logger.debug(
                    f"[{self.name}] {occ} trailing stop active — "
                    f"val={opt_val:.2f} hwm={hwm:.2f} floor={trail_floor:.2f}"
                )
                if opt_val < trail_floor:
                    logger.warning(
                        f"[{self.name}] Trailing stop: {occ} — "
                        f"value={opt_val:.2f} < floor={trail_floor:.2f} "
                        f"(hwm={hwm:.2f}, activation={self.trail_activation_pct:.0%}, "
                        f"trail={self.trail_pct:.0%})"
                    )
                    self._position_hwm.pop(occ, None)
                    self._position_base.pop(occ, None)
                    return True

        except Exception as e:
            logger.error(f"[{self.name}] B-S valuation failed for {occ}: {e}")

        return False

    def _fetch_vix(self) -> float:
        """Return today's VIX as a decimal (e.g. 0.18 for VIX=18).
        Fetches once per calendar day; reuses the cached value intraday."""
        today = date.today()
        if self._vix_date == today:
            return self._vix_sigma
        try:
            import yfinance as yf
            hist = yf.Ticker("^VIX").history(period="1d")
            if not hist.empty:
                self._vix_sigma = float(hist["Close"].iloc[-1]) / 100.0
                self._vix_date = today
                logger.debug(f"[{self.name}] VIX refreshed: {self._vix_sigma:.4f}")
        except Exception as e:
            logger.debug(f"[{self.name}] VIX fetch failed, using {self._vix_sigma:.4f}: {e}")
        return self._vix_sigma

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

        ``notional_cap`` is the per-position sleeve budget (from the allocator).
        It is converted to a per-contract cap by dividing by 100 (the standard
        equity option multiplier), so the picker never returns a contract the
        sleeve cannot afford.

        Returns (occ_symbol, limit_price, take_profit, stop_loss).
        Raises ``OptionTradeRejected`` if no contract survives.
        """
        if notional_cap is None or notional_cap <= 0:
            raise OptionTradeRejected(
                f"{symbol}: notional_cap=${notional_cap} — sleeve has no room."
            )

        max_premium_per_contract = notional_cap  # already $-per-contract scale
        quote_lookup = _build_quote_lookup()

        pick: ContractPick | None = find_best_call(
            symbol,
            underlying_price,
            min_dte=14,
            max_dte=28,
            target_delta=0.55,
            max_premium_per_contract=max_premium_per_contract,
            quote_lookup=quote_lookup,
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

        # Hard SL at -25% (backtest validated). TP is a +200% safety valve —
        # the trailing stop in inspect_open_positions handles real profit-taking.
        take_profit = round(premium * 3.00, 2)
        stop_loss = round(premium * 0.75, 2)

        logger.info(
            f"[{self.name}] {pick.occ_symbol}: premium=${premium:.2f} "
            f"spread={pick.spread_pct:.1%} score={pick.score:.2f} "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f} (safety valve)"
        )
        return pick.occ_symbol, premium, take_profit, stop_loss


def _build_quote_lookup():
    """
    Construct a quote-lookup callable that resolves a batch of OCC symbols
    via Alpaca's option snapshot endpoint. Returns ``None`` for any symbol
    without a valid live quote so the ranker drops it cleanly.
    """
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionSnapshotRequest
    from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY

    data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    def _lookup(occ_symbols: list[str]) -> dict[str, "Quote | None"]:
        if not occ_symbols:
            return {}
        try:
            snapshot = data_client.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=occ_symbols)
            )
        except Exception as e:
            logger.warning(f"OPRA snapshot batch failed: {e}")
            return {occ: None for occ in occ_symbols}

        out: dict[str, "Quote | None"] = {}
        for occ in occ_symbols:
            entry = snapshot.get(occ)
            if entry is None or entry.latest_quote is None:
                out[occ] = None
                continue
            q = entry.latest_quote
            bid = float(q.bid_price)
            ask = float(q.ask_price)
            if bid <= 0 or ask <= 0:
                out[occ] = None
                continue
            out[occ] = Quote(bid=bid, ask=ask)
        return out

    return _lookup
