"""
Credit-spread short-delta audit (PLAN 11.57).

Answers one question: **does the short leg the picker sold carry the delta
the config asked for?**

Why it can be wrong
-------------------

``find_best_put_spread`` prices and ranks strikes against LIVE OPRA quotes
but labels them with a Black-Scholes delta built from two inputs that do
not describe that moment:

  (A) STALE SPOT. On a 1Day slot ``_decision_frame`` drops the in-progress
      bar, so ``underlying_price`` is the *prior completed session's*
      close. Delta and price therefore refer to different spots.

  (B) VOL INPUT. ``sigma = iv_proxy/100``, and ``iv_proxy`` is VIX for
      both SPY and QQQ.

Only the delta path carries either error — the net credit is quote-derived
and assumption-free. That asymmetry is the audit's main lever: when the
market's credit disagrees with the model's own credit at the same spot and
strikes, the disagreement is purely volatility.

The ranker's ``short_delta_window`` cannot catch this: it filters the
*estimate*, not reality.

Data sources, in order of preference
------------------------------------

1. ``--source logs`` — the ``credit_spread_pick`` event shipped with
   11.57. This is the **authoritative** source: it records the exact spot
   and IV proxy the picker used, plus a live spot captured at pick time,
   so no reconstruction is involved. Only available for picks made after
   the instrumentation shipped (2026-08-09).

   A run whose events are fully populated performs **no network I/O at
   all** — bars and ^VIX are fetched lazily and only for entries actually
   missing an input. That is load-bearing, not incidental: an audit that
   needs neither Yahoo nor Alpaca must not be able to fail because one of
   them is down. `TestBuildReportFetchesOnlyWhatIsMissing` pins it by
   making both network boundaries raise.

2. ``--source db`` (default) — reconstructs from ``trades`` rows. Needed
   for the historical sample that predates the instrumentation.
   **Carries a known and irreducible error**: the spot at the pick moment
   is not recorded anywhere, so the same-session close stands in for it.
   Entries fill intraday, so that proxy is wrong by however far the
   underlying travelled between the pick and the close. It biases the
   split between (A) and (B) more than it biases their total. Treat
   per-entry implied vols from this source as indicative, never as
   measurements — the 2026-08-09 run saw them span 8.4 to 30.8.

**Feed:** underlying closes are read on IEX, per CLAUDE.md's rule that
execution replay uses the feed the bot actually saw
(``ALPACA_DATA_FEED='iex'`` on paper). Daily SPY/QQQ closes barely differ
between feeds, but the rule exists so replays stay comparable.

Reproduce
---------

    venv/bin/python -m scripts.credit_spread_delta_audit
    venv/bin/python -m scripts.credit_spread_delta_audit --source logs
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from blackscholes import BlackScholesPut
from loguru import logger

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils.options_lookup import _RISK_FREE_RATE  # noqa: E402

# Execution replay uses the feed the bot saw, not BACKTEST_DATA_FEED.
REPLAY_FEED = "iex"


# ── OCC parsing ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OccContract:
    """Decoded OCC option symbol, e.g. ``QQQ260821P00690000``."""

    underlying: str
    expiry: date
    right: str      # "P" or "C"
    strike: float


def parse_occ(occ: str) -> OccContract:
    """
    Decode an OCC symbol into its parts.

    Layout is fixed-width from the RIGHT — the underlying is variable
    length, so slicing from the left breaks on 2- and 4-letter tickers.
    """
    # 15 fixed chars (yymmdd + right + 8-digit strike) plus at least one
    # character of underlying. This single guard also covers the
    # no-underlying case: a bare tail is exactly 15 characters.
    if len(occ) < 16:
        raise ValueError(f"not an OCC symbol: {occ!r}")
    tail = occ[-15:]
    underlying = occ[:-15]
    yy, mm, dd = int(tail[0:2]), int(tail[2:4]), int(tail[4:6])
    right = tail[6]
    if right not in ("P", "C"):
        raise ValueError(f"OCC right must be P or C, got {right!r} in {occ!r}")
    strike = int(tail[7:15]) / 1000.0
    return OccContract(
        underlying=underlying,
        expiry=date(2000 + yy, mm, dd),
        right=right,
        strike=strike,
    )


# ── Black-Scholes helpers (pure) ────────────────────────────────────────────


def put_delta(*, spot: float, strike: float, dte_days: float, sigma: float) -> float:
    """|delta| of a put. Mirrors utils.options_lookup.estimate_put_delta."""
    T = max(dte_days / 365.0, 0.001)
    bs = BlackScholesPut(S=spot, K=strike, T=T, r=_RISK_FREE_RATE, sigma=sigma)
    return abs(float(bs.delta()))


def spread_credit(
    *, spot: float, short_strike: float, long_strike: float,
    dte_days: float, sigma: float,
) -> float:
    """Model value of a bull put spread (short higher strike, long lower)."""
    T = max(dte_days / 365.0, 0.001)

    def _p(k: float) -> float:
        return float(BlackScholesPut(S=spot, K=k, T=T, r=_RISK_FREE_RATE, sigma=sigma).price())

    return _p(short_strike) - _p(long_strike)


def implied_sigma_from_credit(
    *, spot: float, short_strike: float, long_strike: float,
    dte_days: float, observed_credit: float,
    lo: float = 0.01, hi: float = 3.0,
) -> float | None:
    """
    Bisect for the flat sigma reproducing an observed two-leg credit.

    Returns ``None`` when the credit is unreachable even at ``hi`` — which
    happens for a deep-OTM spread quoted above any plausible model value,
    and is information rather than an error.
    """
    def _c(sig: float) -> float:
        return spread_credit(
            spot=spot, short_strike=short_strike, long_strike=long_strike,
            dte_days=dte_days, sigma=sig,
        )

    if _c(hi) < observed_credit:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if _c(mid) < observed_credit:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def decompose_delta_error(
    *,
    spot_at_decision: float,
    spot_live: float,
    short_strike: float,
    long_strike: float,
    dte_days: float,
    iv_points: float,
    observed_credit: float,
) -> dict[str, float | None]:
    """
    Split the short-leg delta error into its stale-spot and vol-input parts.

    Returns the delta the bot computed, the delta the same model gives at
    the live spot (spot error alone), and the delta at the live spot using
    the market's own implied vol (spot + vol). Differences between those
    three are the per-source contributions.

    The two components are NOT independently identified when ``spot_live``
    is a proxy: any error there is absorbed by the implied vol and shuffles
    the split. The total is the more robust number.
    """
    sigma = iv_points / 100.0
    d_bot = put_delta(
        spot=spot_at_decision, strike=short_strike, dte_days=dte_days, sigma=sigma
    )
    d_spot = put_delta(
        spot=spot_live, strike=short_strike, dte_days=dte_days, sigma=sigma
    )
    sig_mkt = implied_sigma_from_credit(
        spot=spot_live, short_strike=short_strike, long_strike=long_strike,
        dte_days=dte_days, observed_credit=observed_credit,
    )
    d_both = (
        put_delta(spot=spot_live, strike=short_strike, dte_days=dte_days, sigma=sig_mkt)
        if sig_mkt is not None
        else None
    )
    return {
        "delta_bot": d_bot,
        "delta_at_live_spot": d_spot,
        "implied_sigma": sig_mkt,
        "delta_at_market_vol": d_both,
        "err_spot": d_spot - d_bot,
        "err_vol": None if d_both is None else d_both - d_spot,
        "err_total": None if d_both is None else d_both - d_bot,
    }


# ── Entry extraction ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpreadEntry:
    """One observed credit-spread entry, however it was sourced."""

    underlying: str
    entry_date: date
    short_strike: float
    long_strike: float
    expiry: date
    net_credit: float
    source: str
    spot_at_decision: float | None = None   # logs only
    spot_live: float | None = None          # logs only
    iv_points: float | None = None          # logs only
    est_short_delta: float | None = None    # logs only

    @property
    def width(self) -> float:
        return self.short_strike - self.long_strike

    @property
    def dte(self) -> int:
        return (self.expiry - self.entry_date).days


def load_entries_from_db(db_path: Path) -> list[SpreadEntry]:
    """
    Reconstruct entries from the trade log.

    Both legs of a spread share a timestamp. The **short leg carries the
    net credit** in ``avg_fill_price`` and the long leg is written 0.0 —
    verified against the paired exit (QQQ 674/660 entered 2.28, exited
    1.12, booked +$116 = (2.28 − 1.12) × 100).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT timestamp, symbol, side, avg_fill_price
            FROM trades
            WHERE strategy = 'credit_spread'
              AND reason = 'spread entry'
              AND position_type = 'spread'
            ORDER BY timestamp
            """
        ).fetchall()
    finally:
        conn.close()

    by_ts: dict[str, dict[str, sqlite3.Row]] = {}
    for r in rows:
        by_ts.setdefault(r["timestamp"], {})[r["side"]] = r

    entries: list[SpreadEntry] = []
    for ts, legs in sorted(by_ts.items()):
        if "sell" not in legs or "buy" not in legs:
            continue  # half-recorded spread; skip rather than guess
        short = parse_occ(legs["sell"]["symbol"])
        long_ = parse_occ(legs["buy"]["symbol"])
        credit = legs["sell"]["avg_fill_price"]
        if not credit or credit <= 0:
            continue
        entries.append(SpreadEntry(
            underlying=short.underlying,
            entry_date=datetime.fromisoformat(ts).date(),
            short_strike=short.strike,
            long_strike=long_.strike,
            expiry=short.expiry,
            net_credit=float(credit),
            source="db",
        ))
    return entries


def load_entries_from_logs(log_paths: list[Path]) -> list[SpreadEntry]:
    """Read `credit_spread_pick` events — the authoritative source."""
    entries: list[SpreadEntry] = []
    for path in log_paths:
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line).get("record", {})
                except Exception:
                    continue
                extra = rec.get("extra", {})
                if extra.get("event") != "credit_spread_pick":
                    continue
                try:
                    short = parse_occ(extra["short_occ"])
                    ts = rec.get("time", {}).get("repr", "")[:10]
                    entries.append(SpreadEntry(
                        underlying=extra["underlying"],
                        entry_date=date.fromisoformat(ts),
                        short_strike=float(extra["short_strike"]),
                        long_strike=float(extra["long_strike"]),
                        expiry=short.expiry,
                        net_credit=float(extra["net_credit"]),
                        source="logs",
                        spot_at_decision=extra.get("spot_at_decision"),
                        spot_live=extra.get("spot_live"),
                        iv_points=extra.get("iv_proxy_points"),
                        est_short_delta=extra.get("est_short_delta"),
                    ))
                except (KeyError, ValueError):
                    continue
    return entries


# ── Report ──────────────────────────────────────────────────────────────────


def _prior_close(series: pd.Series, on: date) -> float:
    """
    Last close STRICTLY before ``on`` — what ``_decision_frame`` hands a
    1Day strategy mid-session, since the in-progress bar is dropped.
    """
    s = series[series.index < pd.Timestamp(on)]
    return float(s.iloc[-1]) if len(s) else float("nan")


def _session_close(series: pd.Series, on: date) -> float:
    s = series[series.index <= pd.Timestamp(on)]
    return float(s.iloc[-1]) if len(s) else float("nan")


def _fetch_vix_series(start: datetime, end: datetime) -> pd.Series:
    """Daily ^VIX closes. Network boundary — kept separate so the lazy
    caller can avoid it entirely and tests can prove that it did."""
    import yfinance as yf

    s = yf.Ticker("^VIX").history(start=start, end=end)["Close"]
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s


def _fetch_close_series(symbol: str, start: datetime, end: datetime) -> pd.Series:
    """Daily closes on the replay feed. Network boundary — see above."""
    from data.fetcher import fetch_symbol

    df, _ = fetch_symbol(
        symbol, start=start, end=end, timeframe="1Day", feed=REPLAY_FEED
    )
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df["close"]


def build_report(entries: list[SpreadEntry]) -> pd.DataFrame:
    """
    Assemble the per-entry decomposition table.

    **Fetches only what the entries do not already carry.** A
    ``--source logs`` run whose events are fully populated
    (``spot_at_decision``, ``spot_live``, ``iv_proxy_points`` all present)
    touches the network zero times — that is the point of the log source
    being authoritative, and an unrelated Yahoo or Alpaca outage must not
    be able to fail it. Bars and ^VIX are pulled lazily, once, and only
    when some entry actually needs a fallback.

    ``spot_live`` is legitimately ``None`` in a logged event when the live
    quote was unavailable at pick time, so the fallback decision is made
    per entry rather than per source.
    """
    if not entries:
        return pd.DataFrame()

    lo = min(e.entry_date for e in entries)
    hi = max(e.entry_date for e in entries)
    pad_start = datetime(lo.year, lo.month, 1, tzinfo=timezone.utc)
    pad_end = datetime(hi.year, hi.month, hi.day, tzinfo=timezone.utc) + pd.Timedelta(days=5)

    # Memoised lazy loaders: the fetch happens on first genuine need.
    _vix: list[pd.Series] = []
    _bars: dict[str, pd.Series] = {}

    def vix_series() -> pd.Series:
        if not _vix:
            _vix.append(_fetch_vix_series(pad_start, pad_end))
        return _vix[0]

    def close_series(symbol: str) -> pd.Series:
        if symbol not in _bars:
            _bars[symbol] = _fetch_close_series(symbol, pad_start, pad_end)
        return _bars[symbol]

    rows = []
    for e in entries:
        # Logged values win. Use `is None`, not truthiness — a logged 0.0
        # is corrupt data, and silently swapping in a fetched close would
        # hide that instead of surfacing it.
        if e.spot_at_decision is not None:
            spot_dec = e.spot_at_decision
        else:
            spot_dec = _prior_close(close_series(e.underlying), e.entry_date)

        if e.spot_live is not None:
            spot_live = e.spot_live
        else:
            spot_live = _session_close(close_series(e.underlying), e.entry_date)

        if e.iv_points is not None:
            iv = e.iv_points
        else:
            iv = _prior_close(vix_series(), e.entry_date)

        # Black-Scholes asserts S > 0 and sigma > 0 deep inside the
        # library, which would kill the whole batch with a message that
        # names neither the entry nor the field. Check here so one bad
        # row is reported and skipped rather than ending the run.
        bad = [
            name for name, val in (
                ("spot_at_decision", spot_dec),
                ("spot_live", spot_live),
                ("iv_points", iv),
            )
            if val is None or not (val > 0) or pd.isna(val)
        ]
        if bad:
            logger.warning(
                f"skipping {e.underlying} {e.entry_date} ({e.source}): "
                f"non-positive or missing {', '.join(bad)} "
                f"(spot_dec={spot_dec}, spot_live={spot_live}, iv={iv})"
            )
            continue

        d = decompose_delta_error(
            spot_at_decision=spot_dec,
            spot_live=spot_live,
            short_strike=e.short_strike,
            long_strike=e.long_strike,
            dte_days=e.dte,
            iv_points=iv,
            observed_credit=e.net_credit,
        )
        rows.append({
            "und": e.underlying,
            "entry": e.entry_date.isoformat(),
            "src": e.source,
            "Ks": e.short_strike,
            "w": e.width,
            "dte": e.dte,
            "credit": e.net_credit,
            "cred_pct": round(100 * e.net_credit / e.width, 1) if e.width else None,
            "spot_dec": round(spot_dec, 2),
            "spot_live": round(spot_live, 2),
            "vix": round(iv, 1),
            "sig_mkt": None if d["implied_sigma"] is None else round(100 * d["implied_sigma"], 1),
            "d_bot": round(d["delta_bot"], 3),
            "d_logged": e.est_short_delta,
            "d_true": None if d["delta_at_market_vol"] is None else round(d["delta_at_market_vol"], 3),
            "err_spot": round(d["err_spot"], 3),
            "err_vol": None if d["err_vol"] is None else round(d["err_vol"], 3),
            "err_total": None if d["err_total"] is None else round(d["err_total"], 3),
        })
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--source", choices=("db", "logs"), default="db")
    ap.add_argument("--db", default="data/trades.db")
    ap.add_argument("--logs", default="logs", help="directory of bot*.jsonl")
    args = ap.parse_args(argv)

    if args.source == "logs":
        paths = sorted(Path(args.logs).glob("bot*.jsonl"))
        entries = load_entries_from_logs(paths)
        note = f"{len(paths)} log file(s)"
    else:
        entries = load_entries_from_db(Path(args.db))
        note = args.db

    print(f"\ncredit-spread delta audit — source={args.source} ({note}), "
          f"replay feed={REPLAY_FEED}, entries={len(entries)}")
    if args.source == "db":
        print("WARNING: pick-time spot is not recorded pre-11.57; the same-session "
              "close stands in for it. Per-entry implied vols are indicative only "
              "— the split between err_spot and err_vol is soft, the total less so.")

    df = build_report(entries)
    if df.empty:
        print("\nNo entries found.\n")
        return 0

    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 40)
    print("\n=== Per-entry ===\n")
    print(df.to_string(index=False))

    print("\n=== Mean signed error by underlying ===\n")
    agg = df.groupby("und").agg(
        n=("und", "size"),
        mean_cred_pct=("cred_pct", "mean"),
        mean_d_bot=("d_bot", "mean"),
        mean_d_true=("d_true", "mean"),
        mean_err_spot=("err_spot", "mean"),
        mean_err_vol=("err_vol", "mean"),
        mean_err_total=("err_total", "mean"),
    ).round(3)
    print(agg.to_string())
    print("\nPositive err_total = the bot UNDERSTATED the delta it sold.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
