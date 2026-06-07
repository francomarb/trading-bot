"""
Unit tests for scripts/migrate_cache_to_feed_aware.py — pin that the
migration moves legacy top-level parquet + meta files into the feed-aware
subdir, is idempotent on a clean cache, and refuses to clobber.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest


def _load_migration_module():
    """Load scripts/migrate_cache_to_feed_aware.py as a module."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_cache_to_feed_aware.py"
    spec = importlib.util.spec_from_file_location("migrate_cache_to_feed_aware", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_cache_to_feed_aware"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration(monkeypatch, tmp_cache_dir):
    """Run the migration script with sys.argv stubbed; return the module."""
    return _load_migration_module()


def _seed_legacy_file(cache_dir: Path, symbol: str, content: pd.DataFrame) -> None:
    """Drop a legacy top-level parquet + meta pair into the cache."""
    parquet = cache_dir / f"{symbol}_1Day_all.parquet"
    meta = cache_dir / f"{symbol}_1Day_all.meta.json"
    content.to_parquet(parquet)
    meta.write_text(json.dumps({
        "covered_start": datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat(),
        "covered_end": datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
    }))


class TestMigration:
    def test_moves_legacy_files_to_iex_subdir(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)
        _seed_legacy_file(tmp_cache_dir, "MSFT", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", ["migrate_cache_to_feed_aware.py"])
        assert migration.main() == 0

        iex_dir = tmp_cache_dir / "iex"
        assert (iex_dir / "AAPL_1Day_all.parquet").exists()
        assert (iex_dir / "AAPL_1Day_all.meta.json").exists()
        assert (iex_dir / "MSFT_1Day_all.parquet").exists()
        assert (iex_dir / "MSFT_1Day_all.meta.json").exists()
        # Legacy paths are gone.
        assert not (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()
        assert not (tmp_cache_dir / "MSFT_1Day_all.parquet").exists()

    def test_idempotent_on_clean_cache(self, migration, monkeypatch, tmp_cache_dir):
        # No legacy files → migration is a no-op (and exits 0).
        monkeypatch.setattr(sys, "argv", ["migrate_cache_to_feed_aware.py"])
        assert migration.main() == 0

    def test_dry_run_does_not_touch_filesystem(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", ["migrate_cache_to_feed_aware.py", "--dry-run"])
        assert migration.main() == 0

        # Legacy file is untouched; iex/ either does not exist or is empty.
        assert (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()
        if (tmp_cache_dir / "iex").exists():
            assert not (tmp_cache_dir / "iex" / "AAPL_1Day_all.parquet").exists()

    def test_refuses_to_clobber_existing_feed_aware_file(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # Legacy file at top-level AND a file already at iex/ with the same
        # name. Migration must skip the conflict, not silently overwrite.
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)
        iex_dir = tmp_cache_dir / "iex"
        iex_dir.mkdir(parents=True, exist_ok=True)
        _seed_legacy_file(iex_dir, "AAPL", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", ["migrate_cache_to_feed_aware.py"])
        # Skipped files raise the exit code to 1 so CI can flag the
        # inconsistency for operator review.
        assert migration.main() == 1
        # Legacy file is still in place, unmoved.
        assert (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()
