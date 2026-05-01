#!/usr/bin/env python3
"""
Cross-strategy Sharpe comparison — SMA vs RSI vs BollingerSqueeze vs DonchianBreakout.

Runs all four strategies through the SAME backtest harness with IDENTICAL
settings (init cash, slippage, feed, date range), each on its own production
watchlist, so the resulting Sharpes are directly comparable.

This answers: "How does each strategy's Sharpe stack up against the others
in this codebase under the same backtester?"

Each strategy is run with edge filter ON for a fair production-equivalent
comparison.

Output:
    Prints a markdown table to stdout AND (by default) writes a full
    reference report to docs/strategy_sharpe_comparison.md including the
    exact symbol list used per run, the held-constant settings, and
    methodology caveats.

Usage:
    python scripts/compare_strategy_sharpes.py
    python scripts/compare_strategy_sharpes.py --no-write   # stdout only
    python scripts/compare_strategy_sharpes.py --output path/to/file.md
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import BacktestConfig, run_backtest
from config import settings
from scripts.backtest_bollinger_squeeze import (
    UNIVERSES, fetch_bars, run_one as run_squeeze, aggregate_metrics,
)
from strategies.bollinger_squeeze import BollingerSqueeze
from strategies.donchian_breakout import DonchianBreakout
from strategies.filters.bollinger_squeeze import BollingerSqueezeEdgeFilter
from strategies.filters.donchian_breakout import DonchianEdgeFilter
from strategies.filters.rsi_reversion import RSIEdgeFilter
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.rsi_reversion import RSIReversion
from strategies.sma_crossover import SMACrossover


def run_strategy(
    strategy, bars_by_sym: dict[str, pd.DataFrame], cfg: BacktestConfig
) -> dict:
    """Aggregate metrics across all symbols for one strategy."""
    rows = []
    for symbol, df in bars_by_sym.items():
        try:
            result = run_backtest(strategy, df, cfg, symbol=symbol)
        except Exception:  # noqa: BLE001
            continue
        s = result.stats
        rows.append({
            "symbol": symbol,
            "total_return": float(s["total_return"]),
            "cagr": float(s["cagr"]),
            "sharpe": float(s["sharpe"]) if pd.notna(s["sharpe"]) else float("nan"),
            "max_dd": float(s["max_drawdown"]),
            "trades": int(s["trade_count"]),
            "win_rate": float(s["win_rate"]),
        })
    if not rows:
        return {"n_symbols": 0, "n_traded": 0, "mean_sharpe": float("nan"),
                "mean_return": 0.0, "mean_dd": 0.0, "total_trades": 0,
                "weighted_winrate": 0.0}
    n = len(rows)
    sharpes = [r["sharpe"] for r in rows if pd.notna(r["sharpe"])]
    total_trades = sum(r["trades"] for r in rows)
    weighted_winrate = (
        sum(r["win_rate"] * r["trades"] for r in rows) / max(1, total_trades)
    )
    return {
        "n_symbols": n,
        "n_traded": sum(1 for r in rows if r["trades"] > 0),
        "mean_sharpe": sum(sharpes) / max(1, len(sharpes)),
        "mean_return": sum(r["total_return"] for r in rows) / n,
        "mean_dd": sum(r["max_dd"] for r in rows) / n,
        "total_trades": total_trades,
        "weighted_winrate": weighted_winrate,
    }


DEFAULT_OUTPUT = ROOT / "docs" / "strategy_sharpe_comparison.md"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=str, default=str(DEFAULT_OUTPUT),
        help=f"Write report to this markdown path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--no-write", action="store_true",
        help="Print to stdout only; do not write a file.",
    )
    args = parser.parse_args()

    cfg = BacktestConfig()
    end_date = datetime(2026, 4, 28, tzinfo=timezone.utc)
    years = 4.0

    runs = [
        # (label, watchlist_kind, strategy_factory, watchlist)
        (
            "SMA Crossover (20/50)",
            "SMA_WATCHLIST (static, periodically rotated by scripts/sma_watchlist_scan.py)",
            lambda: SMACrossover(fast=20, slow=50, edge_filter=SMAEdgeFilter()),
            settings.SMA_WATCHLIST,
        ),
        (
            "RSI Reversion (14, 30/70)",
            "RSI_WATCHLIST (static snapshot of dynamic scanner output — see caveat below)",
            lambda: RSIReversion(
                period=14, oversold=30, overbought=70, edge_filter=RSIEdgeFilter()
            ),
            settings.RSI_WATCHLIST,
        ),
        (
            "BB Squeeze (bb=10, kc=10, min=6, roc=5)",
            "Sector ETFs (GICS SPDRs — selected by universe research)",
            lambda: BollingerSqueeze(
                bb_length=10, kc_length=10, min_squeeze_bars=6, roc_lookback=5,
                edge_filter=BollingerSqueezeEdgeFilter(),
            ),
            UNIVERSES["sector_etfs"],
        ),
        (
            "BB Squeeze (aggressive 10/4/3)",
            "AI / Big-Tech / Semis (user thesis universe)",
            lambda: BollingerSqueeze(
                bb_length=10, kc_length=10, min_squeeze_bars=4, roc_lookback=3,
                edge_filter=BollingerSqueezeEdgeFilter(),
            ),
            UNIVERSES["ai_bigtech"],
        ),
        (
            "Donchian Breakout (30/15, mid-range)",
            "AI / Big-Tech / Semis (DONCHIAN_WATCHLIST — universe research winner)",
            lambda: DonchianBreakout(
                entry_window=30, exit_window=15,
                edge_filter=DonchianEdgeFilter(),
            ),
            UNIVERSES["ai_bigtech"],
        ),
    ]

    sections: list[str] = []
    sections.append(
        f"# Strategy Sharpe Comparison\n\n"
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n\n"
        f"This is a snapshot reference comparing the three strategies "
        f"(`SMACrossover`, `RSIReversion`, `BollingerSqueeze`) under identical "
        f"backtest settings. Re-run via `python scripts/compare_strategy_sharpes.py`.\n"
    )
    sections.append(
        "## Held-constant settings\n\n"
        f"| Setting | Value |\n"
        f"|---|---|\n"
        f"| Bar range end (pinned) | {end_date.date()} UTC |\n"
        f"| History length | {years} years |\n"
        f"| Bar timeframe | 1Day |\n"
        f"| Initial cash | ${cfg.initial_cash:,.0f} per symbol |\n"
        f"| Slippage | {cfg.slippage_bps} bps |\n"
        f"| Commission | ${cfg.commission_per_trade} per trade |\n"
        f"| Data feed | {settings.ALPACA_DATA_FEED} |\n"
        f"| Edge filters | ON for all three strategies |\n"
        f"| ATR stops in backtest | NO — vectorbt does not model the engine's "
        f"`ATR_STOP_MULTIPLIER=2.0` stop legs |\n"
        f"| Aggregation | Equally weighted across each strategy's universe |\n"
    )

    sections.append("## Results\n")
    sections.append(
        "| Strategy | Universe (kind) | Symbols traded / total | Sharpe | MeanRet | MeanDD | Trades | WinRate |\n"
        "|----------|-----------------|-----------------------:|------:|------:|-----:|------:|------:|"
    )

    universe_blocks: list[str] = []

    print(f"# Strategy Sharpe Comparison")
    print(f"- Bar range end: {end_date.date()}, history {years}y, init_cash=${cfg.initial_cash:,.0f}, slippage={cfg.slippage_bps} bps, feed={settings.ALPACA_DATA_FEED}")
    print()
    print("| Strategy | Universe | Symbols | Sharpe | MeanRet | MeanDD | Trades | WinRate |")
    print("|----------|---------:|--------:|-------:|--------:|-------:|-------:|--------:|")

    for label, watchlist_kind, factory, watchlist in runs:
        bars = fetch_bars(list(watchlist), years, end_date=end_date)
        if not bars:
            continue
        strategy = factory()
        m = run_strategy(strategy, bars, cfg)

        row = (
            f"| {label} | {watchlist_kind} | "
            f"{m['n_traded']}/{m['n_symbols']} | "
            f"{m['mean_sharpe']:+.2f} | "
            f"{m['mean_return']*100:+.1f}% | "
            f"{m['mean_dd']*100:+.1f}% | "
            f"{m['total_trades']} | "
            f"{m['weighted_winrate']*100:.1f}% |"
        )
        sections.append(row)

        # Console one-liner.
        print(
            f"| {label:<42} | {m['n_symbols']:>2} sym | "
            f"{m['n_traded']:>3} traded | {m['mean_sharpe']:>+5.2f} | "
            f"{m['mean_return']*100:>+6.1f}% | {m['mean_dd']*100:>+5.1f}% | "
            f"{m['total_trades']:>5} | {m['weighted_winrate']*100:>5.1f}% |"
        )

        # Per-strategy universe block.
        universe_blocks.append(
            f"### {label}\n\n"
            f"- **Universe kind:** {watchlist_kind}\n"
            f"- **Symbols ({len(watchlist)}):** `{', '.join(watchlist)}`\n"
            f"- **Symbols that produced any trade:** {m['n_traded']} of {m['n_symbols']}\n"
        )

    sections.append("\n## Universe details\n")
    sections.extend(universe_blocks)

    sections.append(
        "## Methodology caveats\n\n"
        "1. **No ATR stops in backtest.** The vectorbt harness does not "
        "execute the engine's 2× ATR stop-loss. In production, SMA Crossover "
        "and BB Squeeze (AI/BigTech) drawdowns would compress meaningfully "
        "without much Sharpe penalty.\n"
        "\n"
        "2. **RSI's low trade count is structural, not a bug.** "
        "`RSI_WATCHLIST` is a *static snapshot* of a weekly scanner "
        "(`scripts/rsi_watchlist_scan.py`) that selects names *currently close "
        "to triggering* the RSI-oversold setup. Backtesting that frozen list "
        "over 4 years means most names spent most of the period not setting "
        "up — only the ones that happened to oversold-revert during the window "
        "produced trades. **The 4-year Sharpe understates the production "
        "strategy** because production rotates the watchlist. A more honest "
        "RSI Sharpe would require a walk-forward backtest that re-runs the "
        "scanner each week — out of scope for this snapshot.\n"
        "\n"
        "3. **Edge filters ON for all strategies.** Same configuration as "
        "production (`forward_test.py`).\n"
        "\n"
        "4. **Equal-weight aggregation.** Each universe's Sharpe is the mean "
        "of per-symbol Sharpes (skipping NaN where a symbol produced no "
        "trades and Sharpe is undefined). This matches how each strategy is "
        "actually deployed — one position per symbol, no inter-symbol "
        "weighting.\n"
        "\n"
        "5. **In-sample, single window.** No walk-forward. Step 2 (ATR stops) "
        "and walk-forward validation are deferred work.\n"
    )

    sections.append(
        "## Reproducibility\n\n"
        "```bash\n"
        "python scripts/compare_strategy_sharpes.py\n"
        "```\n"
        "\n"
        "Settings live in `scripts/compare_strategy_sharpes.py` — change the "
        "factory closures or `runs` list to add a new strategy/universe.\n"
        "\n"
        "Related research: [bollinger_squeeze_universe_research.md]"
        "(./bollinger_squeeze_universe_research.md).\n"
    )

    full_report = "\n".join(sections)

    if not args.no_write:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(full_report)
        print(f"\nwrote report to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
