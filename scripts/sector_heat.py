"""
Sector Heat Report — refreshes sector mappings and writes docs/sector_heat.md.

Run any time you want a fresh snapshot of sector momentum and symbol allocation:

    python scripts/sector_heat.py

What it does:
  1. Hydrates the SectorResolver with the current watchlists (new symbols get
     resolved via yfinance; already-cached symbols are skipped).
  2. Fetches current ETF bars and scores all 12 sectors via SectorMomentumGauge.
  3. Groups every watched symbol by sector, annotated with which strategies own it.
  4. Writes docs/sector_heat.md — sorted HOT → NEUTRAL → COLD, with signal
     breakdowns and a summary table.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

# ── path setup so the script can be run from the repo root ──────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from sector.gauge import SectorMomentum, SectorMomentumGauge
from sector.resolver import SectorResolver

# ── constants ────────────────────────────────────────────────────────────────

OUTPUT_PATH = Path("docs/sector_heat.md")

STRATEGY_TAG = {
    "sma": "SMA",
    "rsi": "RSI",
    "don": "DON",
}

CLASSIFICATION_EMOJI = {
    SectorMomentum.HOT:     "🔥 HOT",
    SectorMomentum.NEUTRAL: "➖ NEUTRAL",
    SectorMomentum.COLD:    "🧊 COLD",
}

CLASSIFICATION_ORDER = [SectorMomentum.HOT, SectorMomentum.NEUTRAL, SectorMomentum.COLD]


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_symbol_map(
    resolver: SectorResolver,
) -> dict[str, list[tuple[str, list[str]]]]:
    """Return {sector: [(symbol, [strategy_tags]), ...]} for all watched symbols."""
    sma_set = set(settings.SMA_WATCHLIST)
    rsi_set = set(settings.RSI_WATCHLIST)
    don_set = set(settings.DONCHIAN_WATCHLIST)

    sector_symbols: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    unmapped: list[tuple[str, list[str]]] = []

    all_symbols = sorted(sma_set | rsi_set | don_set)
    for sym in all_symbols:
        tags = []
        if sym in sma_set:
            tags.append("SMA")
        if sym in rsi_set:
            tags.append("RSI")
        if sym in don_set:
            tags.append("DON")

        sector = resolver.resolve(sym)
        if sector:
            sector_symbols[sector].append((sym, tags))
        else:
            unmapped.append((sym, tags))

    return dict(sector_symbols), unmapped


def _signal_row(detail) -> str:
    """One-line signal breakdown for the score table."""
    cross = "golden" if detail.golden_cross else "death"
    vol = "✓" if detail.vol_confirm else "–"
    return (
        f">SMA200={'✓' if detail.above_sma200 else '✗'}  "
        f">SMA50={'✓' if detail.above_sma50 else '✗'}  "
        f"{cross} cross  "
        f"dist={detail.dist_sma50_pct:+.1%}  "
        f"vol={vol}"
    )


def _tag_string(tags: list[str]) -> str:
    return " ".join(f"`{t}`" for t in tags)


# ── report writer ─────────────────────────────────────────────────────────────

def write_report(
    gauge: SectorMomentumGauge,
    resolver: SectorResolver,
    out_path: Path,
) -> None:
    symbol_map, unmapped = _build_symbol_map(resolver)

    # Fetch all sector details
    details = {
        sector: gauge.get_details(sector)
        for sector in settings.SECTOR_ETFS
    }

    # Group sectors by classification
    by_class: dict[SectorMomentum, list[str]] = defaultdict(list)
    for sector, detail in sorted(details.items(), key=lambda x: x[0]):
        by_class[detail.classification].append(sector)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []

    lines.append("# Sector Heat Report")
    lines.append(f"\n_Generated {now}_\n")

    # ── Summary table ────────────────────────────────────────────────────────
    lines.append("## Summary\n")
    lines.append("| Sector | ETF | Score | Status | Signals |")
    lines.append("|---|---|---|---|---|")

    for cls in CLASSIFICATION_ORDER:
        for sector in sorted(by_class.get(cls, [])):
            d = details[sector]
            etf = settings.SECTOR_ETFS.get(sector, "N/A")
            status = CLASSIFICATION_EMOJI[d.classification]
            signals = _signal_row(d)
            close_str = f"${d.last_close:.2f}" if d.last_close else "N/A"
            lines.append(
                f"| {sector} | {etf} ({close_str}) | **{d.score:+d}** | {status} | {signals} |"
            )

    lines.append("")

    # ── Per-sector symbol sections ────────────────────────────────────────────
    lines.append("## Symbols by Sector\n")

    for cls in CLASSIFICATION_ORDER:
        sectors_in_class = sorted(by_class.get(cls, []))
        if not sectors_in_class:
            continue

        lines.append(f"### {CLASSIFICATION_EMOJI[cls]}\n")

        for sector in sectors_in_class:
            d = details[sector]
            etf = settings.SECTOR_ETFS.get(sector, "N/A")
            syms = symbol_map.get(sector, [])

            lines.append(f"#### {sector.replace('_', ' ').title()} — {etf} | score {d.score:+d}")
            lines.append("")

            if syms:
                lines.append("| Symbol | Strategies |")
                lines.append("|---|---|")
                for sym, tags in sorted(syms, key=lambda x: x[0]):
                    lines.append(f"| {sym} | {_tag_string(tags)} |")
            else:
                lines.append("_No watched symbols in this sector._")

            lines.append("")

    # ── Unmapped symbols ──────────────────────────────────────────────────────
    if unmapped:
        lines.append("## Unmapped Symbols\n")
        lines.append("These symbols have no sector mapping (ETFs or unresolved).\n")
        lines.append("| Symbol | Strategies |")
        lines.append("|---|---|")
        for sym, tags in sorted(unmapped, key=lambda x: x[0]):
            lines.append(f"| {sym} | {_tag_string(tags)} |")
        lines.append("")

    # ── Write file ────────────────────────────────────────────────────────────
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    logger.info(f"sector heat report written → {out_path}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.remove()
    logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}", level="INFO")

    all_symbols = sorted(
        set(settings.SMA_WATCHLIST) | set(settings.RSI_WATCHLIST) | set(settings.DONCHIAN_WATCHLIST)
    )
    logger.info(f"hydrating resolver for {len(all_symbols)} symbols …")

    resolver = SectorResolver(valid_sectors=set(settings.SECTOR_ETFS))
    resolver.hydrate(all_symbols)

    cached = sum(1 for s in all_symbols if resolver.resolve(s) is not None)
    logger.info(f"resolved {cached}/{len(all_symbols)} symbols  ({len(all_symbols) - cached} unmapped — ETFs or unknown)")

    logger.info("scoring sector ETFs …")
    gauge = SectorMomentumGauge(sector_etfs=settings.SECTOR_ETFS)

    # Eager-load all sectors so we can log the summary before writing the file
    for sector in sorted(settings.SECTOR_ETFS):
        d = gauge.get_details(sector)
        etf = settings.SECTOR_ETFS[sector]
        label = d.classification.value.upper()
        logger.info(f"  {sector:<16} {etf:<5} score={d.score:+d}  [{label}]")

    write_report(gauge, resolver, OUTPUT_PATH)
    logger.info(f"done — open {OUTPUT_PATH} to review")


if __name__ == "__main__":
    main()
