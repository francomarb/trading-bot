#!/usr/bin/env python3
"""
One-shot migration: move legacy top-level cache files into a feed-aware subdir.

Before PR #50 the fetcher cached every parquet as
``data/historical/{SYMBOL}_{timeframe}_{adjustment}.parquet`` regardless of
the data feed they came from. With the feed-aware layout the cache must be
partitioned by feed (``data/historical/iex/...`` and
``data/historical/sip/...``) so synthetic-SIP volume scaling on IEX bars
doesn't get applied to SIP bars and vice versa.

**Provenance problem.** Legacy files have no recorded feed. They might be
IEX (the historical default), SIP (if anyone ran with ``ALPACA_DATA_FEED=sip``
or via the watchlist scanners), or mixed (if the env var was flipped during a
single cache file's life). Without provenance, the safe default is
**quarantine** — move legacy files into ``data/historical/legacy_unknown_feed/``
where the fetcher will not read them. From there the operator decides
per-batch what to do:

  - **Confident the bars are IEX?** Re-run with ``--assume-feed=iex
    --confirm-assumed-feed``. You'll be required to acknowledge the
    unverifiable claim. Files move into ``iex/`` and the fetcher reads them
    as IEX going forward (with the 20× synthetic-SIP volume scaling applied
    at read time). **This works at any later point** — the script scans
    both top-level legacy files AND already-quarantined files when
    ``--assume-feed`` is provided, so you can quarantine first, decide
    later, and promote the quarantine in a second run.
  - **Not sure / cache may be mixed?** Leave files in
    ``legacy_unknown_feed/`` and let the fetcher repopulate from the API as
    each symbol is touched. The next live-bot cycle that needs MSFT will
    write a fresh ``iex/MSFT_1Day_all.parquet`` from the API; the next
    backtest that asks for ``feed="sip"`` will write a fresh
    ``sip/MSFT_1Day_all.parquet``. The quarantined files can be deleted
    once everything you care about has been refreshed.

Run this once during a bot recycle window:

    ./stop_bot.sh
    venv/bin/python scripts/migrate_cache_to_feed_aware.py            # quarantine
    # or, if confident all legacy bars are IEX:
    # venv/bin/python scripts/migrate_cache_to_feed_aware.py \\
    #     --assume-feed=iex --confirm-assumed-feed
    ./start_bot.sh

The script is idempotent: re-running it after migration is a no-op.

The fetcher already has a legacy-read fallback for IEX, so the bot won't
break if you forget to run this — but the fallback is dead-weight after
migration and SHOULD be removed in a follow-up once migration is confirmed
done on every machine. Run this so we can clean that up.

Flags:
    --dry-run                  Show what would move without touching the filesystem.
    --assume-feed=iex|sip      Explicitly claim provenance for the legacy files.
    --confirm-assumed-feed     Required alongside --assume-feed (acknowledges
                               the claim is operator-verified, not derivable).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import fetcher  # noqa: E402


_QUARANTINE_SUBDIR = "legacy_unknown_feed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would move without touching the filesystem.",
    )
    parser.add_argument(
        "--assume-feed", default=None, choices=["iex", "sip"],
        help="Explicitly claim provenance for legacy files and route them to "
             "that feed's subdir. Requires --confirm-assumed-feed because the "
             "claim is unverifiable from the file alone. Without this flag, "
             "legacy files are quarantined into data/historical/"
             f"{_QUARANTINE_SUBDIR}/ where the fetcher will not read them.",
    )
    parser.add_argument(
        "--confirm-assumed-feed", action="store_true",
        help="Required alongside --assume-feed. By passing this you acknowledge "
             "the feed provenance claim is operator-verified and not derivable "
             "from the file. If wrong, downstream synthetic-SIP volume scaling "
             "will produce silently wrong numbers.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if args.verbose else "INFO",
        format="<level>{level: <8}</level> | {message}",
    )

    # Decide destination
    if args.assume_feed is not None:
        if not args.confirm_assumed_feed:
            logger.error(
                f"--assume-feed={args.assume_feed} requires "
                "--confirm-assumed-feed to acknowledge the unverifiable "
                "provenance claim. Aborting."
            )
            return 2
        dest_label = args.assume_feed.lower()
        dest_dir = fetcher.CACHE_DIR / dest_label
        logger.warning(
            f"OPERATOR-CLAIMED PROVENANCE: legacy files will be moved into "
            f"{dest_dir} and read as {dest_label} from now on. If this claim "
            f"is wrong, synthetic-SIP volume scaling on read will produce "
            f"silently wrong numbers."
        )
    else:
        dest_label = _QUARANTINE_SUBDIR
        dest_dir = fetcher.CACHE_DIR / _QUARANTINE_SUBDIR
        logger.info(
            f"DEFAULT MODE: legacy files will be quarantined into {dest_dir}. "
            f"The fetcher will not read them. Use --assume-feed=iex --confirm-"
            f"assumed-feed to instead route into iex/ (and acknowledge the "
            f"unverifiable provenance claim)."
        )

    cache_dir = fetcher.CACHE_DIR
    quarantine_dir = cache_dir / _QUARANTINE_SUBDIR
    logger.info(f"cache root: {cache_dir}")
    logger.info(f"destination: {dest_dir}")

    if not cache_dir.exists():
        logger.warning(f"cache dir does not exist: {cache_dir}")
        return 0

    # Discover sources to migrate. The two-source design supports the
    # documented "quarantine first, decide later" workflow:
    #
    #   1. First run: no --assume-feed → top-level legacy files move to
    #      legacy_unknown_feed/. (Default mode; safe.)
    #   2. Later, once operator is confident: --assume-feed=iex
    #      --confirm-assumed-feed → both top-level legacy AND already-
    #      quarantined files get promoted into iex/.
    #
    # In default mode we only scan the top level. With explicit
    # --assume-feed, we additionally scan the quarantine subdir — promoting
    # already-quarantined files into the named feed subdir. This makes the
    # documented recovery path actually work (reviewer P2).
    sources: list[Path] = []
    sources += [p for p in cache_dir.glob("*.parquet") if p.is_file()]
    sources += [p for p in cache_dir.glob("*.meta.json") if p.is_file()]
    promoting_from_quarantine = (
        args.assume_feed is not None and quarantine_dir.exists()
    )
    if promoting_from_quarantine:
        sources += [p for p in quarantine_dir.glob("*.parquet") if p.is_file()]
        sources += [p for p in quarantine_dir.glob("*.meta.json") if p.is_file()]

    legacy_parquets = [p for p in sources if p.suffix == ".parquet"]
    legacy_metas = [p for p in sources if p.name.endswith(".meta.json")]
    legacy_total = len(legacy_parquets) + len(legacy_metas)

    if legacy_total == 0:
        logger.info("no legacy cache files found — nothing to migrate")
        return 0

    quarantine_count = sum(1 for p in sources if p.parent == quarantine_dir)
    top_level_count = legacy_total - quarantine_count
    logger.info(
        f"found {top_level_count} top-level + {quarantine_count} quarantined "
        f"= {legacy_total} legacy file(s) to migrate"
    )

    if not args.dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    skipped = 0
    for src in legacy_parquets + legacy_metas:
        dst = dest_dir / src.name
        if dst.exists():
            logger.warning(
                f"SKIP {src.name}: destination already exists "
                f"({dst}) — likely a previous partial migration; "
                f"resolve manually"
            )
            skipped += 1
            continue
        if args.dry_run:
            logger.info(f"DRY {src.name} → {dst}")
        else:
            src.rename(dst)
            logger.debug(f"moved {src.name} → {dst}")
        moved += 1

    if args.dry_run:
        logger.info(f"DRY-RUN summary: {moved} would move, {skipped} would skip")
    else:
        logger.info(f"migration complete: {moved} moved, {skipped} skipped (→ {dest_label})")

    if skipped:
        logger.warning(
            "skipped files mean the destination already had something at "
            "the same name — usually means migration was already partially "
            "done, or both IEX and SIP caches existed at the legacy layer. "
            "Inspect and resolve before assuming the cache is consistent."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
