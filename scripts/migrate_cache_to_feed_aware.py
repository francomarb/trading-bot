#!/usr/bin/env python3
"""
One-shot migration: move legacy top-level cache files into the feed-aware
subdir (`data/historical/iex/`).

Before this PR the fetcher cached every parquet as
``data/historical/{SYMBOL}_{timeframe}_{adjustment}.parquet`` regardless of
the data feed they came from. With the feed-aware layout the same files
belong at ``data/historical/iex/...`` (we treat all legacy bars as IEX-fed
because that was the historical default value of ``ALPACA_DATA_FEED``).

Run this once during a bot recycle window:

    ./stop_bot.sh
    venv/bin/python scripts/migrate_cache_to_feed_aware.py
    ./start_bot.sh

The script is idempotent: re-running it after migration is a no-op.

The fetcher already has a legacy-read fallback so the bot doesn't break if
you forget to run this — but the fallback is dead-weight and SHOULD be
removed in a follow-up once migration is confirmed complete on every
machine. Run this so we can clean that up.

Flags:
    --dry-run         Show what would move without touching the filesystem.
    --feed iex|sip    Destination subdir for legacy files (default: iex).
                      You almost certainly want the default; this is here
                      for completeness only.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would move without touching the filesystem.",
    )
    parser.add_argument(
        "--feed", default="iex", choices=["iex", "sip"],
        help="Destination subdir for legacy files (default: iex — almost "
             "certainly what you want).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if args.verbose else "INFO",
        format="<level>{level: <8}</level> | {message}",
    )

    cache_dir = fetcher.CACHE_DIR
    dest_dir = cache_dir / args.feed.lower()
    logger.info(f"cache root: {cache_dir}")
    logger.info(f"destination: {dest_dir}")

    if not cache_dir.exists():
        logger.warning(f"cache dir does not exist: {cache_dir}")
        return 0

    # Discover legacy top-level files. Anything inside an existing subdir
    # has already been migrated (or was never legacy).
    legacy_parquets = [p for p in cache_dir.glob("*.parquet") if p.is_file()]
    legacy_metas = [p for p in cache_dir.glob("*.meta.json") if p.is_file()]
    legacy_total = len(legacy_parquets) + len(legacy_metas)

    if legacy_total == 0:
        logger.info("no legacy top-level cache files found — nothing to migrate")
        return 0

    logger.info(
        f"found {len(legacy_parquets)} parquet + {len(legacy_metas)} meta = "
        f"{legacy_total} legacy file(s) to migrate"
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
        logger.info(f"migration complete: {moved} moved, {skipped} skipped")

    if skipped:
        logger.warning(
            "skipped files mean the feed-aware path already had something at "
            "the same name — usually means migration was already partially "
            "done, or both IEX and SIP caches existed at the legacy layer. "
            "Inspect and resolve before assuming the cache is consistent."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
