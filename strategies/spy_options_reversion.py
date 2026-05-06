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
from utils.options_lookup import find_best_call

_ET = ZoneInfo("America/New_York")
_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


class SPYOptionsReversionStrategy(BaseStrategy):
    name = "spy_options_reversion"
    preferred_order_type = OrderType.LIMIT

    def __init__(self, rsi_length: int = 14, rsi_threshold: float = 30, *, edge_filter=None):
        super().__init__(edge_filter=edge_filter)
        self.rsi_length = rsi_length
        self.rsi_threshold = rsi_threshold
        # VIX cache: refreshed once per calendar day to avoid hot-loop HTTP calls.
        self._vix_date: date | None = None
        self._vix_sigma: float = 0.15  # fallback: ~VIX 15

    @property
    def required_bars(self) -> int:
        return self.rsi_length + 5

    # ── Signal generation ────────────────────────────────────────────────────

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        false_series = pd.Series(False, index=df.index)
        if len(df) < self.required_bars:
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
        trigger an immediate market exit.  Two guards run in order:

        1. Time stop  — exit by Wednesday 3:30 PM ET of the contract's expiry
                        week.  Prevents holding through the Theta cliff.
        2. Delta floor — exit if approximated Delta < 0.30.  Signals the
                         option has moved too far OTM to be a viable play.
        """
        match = _OCC_RE.match(position.symbol)
        if not match or match.group(3) != "C":
            return False

        expiry_date = datetime.strptime(match.group(2), "%y%m%d").date()
        strike = float(match.group(4)) / 1000.0

        # ── Guard 1: time stop ───────────────────────────────────────────────
        # Exit on or after the Wednesday of expiry week at 3:30 PM ET.
        # "Wednesday of expiry week" = expiry_date (Friday) - 2 days.
        now_et = datetime.now(_ET)
        expiry_wednesday = expiry_date - timedelta(days=2)
        at_or_past_330 = now_et.hour * 60 + now_et.minute >= 15 * 60 + 30
        if now_et.date() >= expiry_wednesday and at_or_past_330:
            logger.warning(
                f"[{self.name}] Time stop: {position.symbol} — "
                f"expiry week Wednesday reached ({now_et.strftime('%a %Y-%m-%d %H:%M ET')})"
            )
            return True

        # ── Guard 2: Delta floor ─────────────────────────────────────────────
        # T = time to options market close (4 PM ET on expiry date) in years.
        expiry_close_et = datetime.combine(expiry_date, time(16, 0), tzinfo=_ET)
        t_days = (expiry_close_et - datetime.now(timezone.utc)).total_seconds() / 86400.0
        T = max(t_days / 365.0, 0.001)

        sigma = self._fetch_vix()

        try:
            from blackscholes import BlackScholesCall
            call = BlackScholesCall(S=latest_close, K=strike, T=T, r=0.05, sigma=sigma)
            delta = call.delta()
            logger.debug(
                f"[{self.name}] {position.symbol} Delta={delta:.3f} "
                f"(S={latest_close:.2f}, K={strike:.2f}, T={T:.4f}y, σ={sigma:.2f})"
            )
            if delta < 0.30:
                logger.warning(
                    f"[{self.name}] Delta floor: {position.symbol} — "
                    f"Delta={delta:.3f} < 0.30, exiting"
                )
                return True
        except Exception as e:
            logger.error(f"[{self.name}] Delta calculation failed for {position.symbol}: {e}")

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
        self, symbol: str, underlying_price: float
    ) -> Tuple[str, float, float, float]:
        """
        Resolve the best OCC contract and price it at the midpoint.

        Spread guard: rejects if (ask - bid) / midpoint > 5%.
        No-quote guard: rejects if bid <= 0 or OPRA data unavailable.

        Returns (occ_symbol, limit_price, take_profit, stop_loss).
        Raises ValueError on any rejection so the engine skips the trade.
        """
        occ_symbol = find_best_call(
            symbol, underlying_price, min_dte=10, max_dte=21, target_delta=0.55
        )
        if not occ_symbol:
            raise ValueError(f"No valid option contract found for {symbol}")

        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionSnapshotRequest
        from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY

        data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = OptionSnapshotRequest(symbol_or_symbols=occ_symbol)

        try:
            snapshot = data_client.get_option_snapshots(req)
        except Exception as e:
            raise ValueError(
                f"OPRA snapshot unavailable for {occ_symbol}: {e}. "
                "Cannot verify spread — skipping trade."
            )

        entry = snapshot.get(occ_symbol)
        if entry is None:
            raise ValueError(
                f"No snapshot data returned for {occ_symbol}. "
                "Cannot verify spread — skipping trade."
            )

        # Prefer quote; fall back to last trade.
        quote = entry.latest_quote
        if quote is not None:
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            if bid <= 0:
                raise ValueError(
                    f"{occ_symbol}: bid=${bid:.2f} — no valid quote. "
                    "Cannot verify spread — skipping trade."
                )
            midpoint = (bid + ask) / 2.0
            spread_pct = (ask - bid) / midpoint
            if spread_pct > 0.05:
                raise ValueError(
                    f"{occ_symbol}: spread {spread_pct:.1%} > 5% "
                    f"(bid={bid:.2f} ask={ask:.2f}) — skipping trade."
                )
            premium = midpoint
        elif entry.latest_trade is not None:
            # No quote available; use last trade price but cannot check spread.
            raise ValueError(
                f"{occ_symbol}: no live quote (only last trade). "
                "Cannot verify spread — skipping trade."
            )
        else:
            raise ValueError(
                f"{occ_symbol}: no quote or trade data available — skipping trade."
            )

        if premium <= 0:
            raise ValueError(f"{occ_symbol}: computed premium={premium:.2f} <= 0 — skipping trade.")

        take_profit = round(premium * 1.20, 2)
        stop_loss = round(premium * 0.70, 2)

        logger.info(
            f"[{self.name}] {occ_symbol}: premium=${premium:.2f} "
            f"spread={spread_pct:.1%} TP=${take_profit:.2f} SL=${stop_loss:.2f}"
        )
        return occ_symbol, premium, take_profit, stop_loss
