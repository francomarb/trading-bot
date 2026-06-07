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


_QUARANTINE = "legacy_unknown_feed"


class TestMigration:
    def test_default_quarantines_legacy_files(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # Default mode (no --assume-feed) MUST quarantine — provenance is
        # unverifiable so we don't pretend the bars are IEX.
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)
        _seed_legacy_file(tmp_cache_dir, "MSFT", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", ["migrate_cache_to_feed_aware.py"])
        assert migration.main() == 0

        quarantine = tmp_cache_dir / _QUARANTINE
        assert (quarantine / "AAPL_1Day_all.parquet").exists()
        assert (quarantine / "AAPL_1Day_all.meta.json").exists()
        assert (quarantine / "MSFT_1Day_all.parquet").exists()
        # MUST NOT have created iex/ — that would be the silent-IEX-assumption
        # bug this whole quarantine design exists to prevent.
        assert not (tmp_cache_dir / "iex").exists()
        # Legacy paths are gone.
        assert not (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()

    def test_assume_feed_iex_with_confirmation_routes_to_iex(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # With explicit --assume-feed=iex --confirm-assumed-feed, operator
        # is claiming provenance; files move into iex/.
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", [
            "migrate_cache_to_feed_aware.py",
            "--assume-feed=iex",
            "--confirm-assumed-feed",
        ])
        assert migration.main() == 0
        assert (tmp_cache_dir / "iex" / "AAPL_1Day_all.parquet").exists()
        assert not (tmp_cache_dir / _QUARANTINE).exists()

    def test_assume_feed_without_confirmation_aborts(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # Passing --assume-feed=iex alone (no confirm) must abort without
        # moving files — this guard prevents accidental IEX-assumption.
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", [
            "migrate_cache_to_feed_aware.py",
            "--assume-feed=iex",
        ])
        assert migration.main() == 2  # explicit non-zero abort code
        # Legacy file untouched, neither destination created.
        assert (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()
        assert not (tmp_cache_dir / "iex").exists()
        assert not (tmp_cache_dir / _QUARANTINE).exists()

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

        # Legacy file is untouched; quarantine subdir either does not exist
        # or contains no AAPL file.
        assert (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()
        quarantine = tmp_cache_dir / _QUARANTINE
        if quarantine.exists():
            assert not (quarantine / "AAPL_1Day_all.parquet").exists()

    def test_refuses_to_clobber_existing_destination_file(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # Legacy file at top-level AND a file already at the quarantine
        # subdir with the same name. Default mode (quarantine) tries to
        # move the top-level file to a destination that already exists.
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)
        quarantine = tmp_cache_dir / _QUARANTINE
        quarantine.mkdir(parents=True, exist_ok=True)
        _seed_legacy_file(quarantine, "AAPL", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", ["migrate_cache_to_feed_aware.py"])
        # Skipped files raise the exit code to 1 so CI can flag the
        # inconsistency for operator review.
        assert migration.main() == 1
        # Legacy file is still in place, unmoved.
        assert (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()

    def test_assume_feed_promotes_already_quarantined_files(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # Reviewer P2: the documented workflow is "quarantine first, later
        # decide the files are IEX after all and promote them." This test
        # pins the promotion path: with --assume-feed=iex
        # --confirm-assumed-feed, the script must also scan
        # legacy_unknown_feed/ and move those files into iex/.
        quarantine = tmp_cache_dir / _QUARANTINE
        quarantine.mkdir(parents=True, exist_ok=True)
        _seed_legacy_file(quarantine, "AAPL", clean_ohlcv)
        _seed_legacy_file(quarantine, "MSFT", clean_ohlcv)
        # No top-level legacy files at all — only quarantined.

        monkeypatch.setattr(sys, "argv", [
            "migrate_cache_to_feed_aware.py",
            "--assume-feed=iex",
            "--confirm-assumed-feed",
        ])
        assert migration.main() == 0

        iex_dir = tmp_cache_dir / "iex"
        assert (iex_dir / "AAPL_1Day_all.parquet").exists()
        assert (iex_dir / "AAPL_1Day_all.meta.json").exists()
        assert (iex_dir / "MSFT_1Day_all.parquet").exists()
        # Quarantined files are gone (moved out, not copied).
        assert not (quarantine / "AAPL_1Day_all.parquet").exists()
        assert not (quarantine / "MSFT_1Day_all.parquet").exists()

    def test_assume_feed_promotes_both_top_level_and_quarantined(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # Mixed state: top-level has AAPL, quarantine has MSFT. With
        # --assume-feed=iex the script must move BOTH into iex/.
        _seed_legacy_file(tmp_cache_dir, "AAPL", clean_ohlcv)
        quarantine = tmp_cache_dir / _QUARANTINE
        quarantine.mkdir(parents=True, exist_ok=True)
        _seed_legacy_file(quarantine, "MSFT", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", [
            "migrate_cache_to_feed_aware.py",
            "--assume-feed=iex",
            "--confirm-assumed-feed",
        ])
        assert migration.main() == 0

        iex_dir = tmp_cache_dir / "iex"
        assert (iex_dir / "AAPL_1Day_all.parquet").exists()
        assert (iex_dir / "MSFT_1Day_all.parquet").exists()
        assert not (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()
        assert not (quarantine / "MSFT_1Day_all.parquet").exists()

    def test_default_mode_does_not_touch_quarantined_files(
        self, migration, monkeypatch, tmp_cache_dir, clean_ohlcv
    ):
        # Inverse of the promotion test: in default (no --assume-feed) mode,
        # already-quarantined files must NOT be moved or touched. Only the
        # promotion mode is allowed to reach into the quarantine subdir.
        quarantine = tmp_cache_dir / _QUARANTINE
        quarantine.mkdir(parents=True, exist_ok=True)
        _seed_legacy_file(quarantine, "AAPL", clean_ohlcv)
        # Also seed a top-level file so the script has SOMETHING to do.
        _seed_legacy_file(tmp_cache_dir, "MSFT", clean_ohlcv)

        monkeypatch.setattr(sys, "argv", ["migrate_cache_to_feed_aware.py"])
        assert migration.main() == 0

        # MSFT moved into quarantine (default mode).
        assert (quarantine / "MSFT_1Day_all.parquet").exists()
        # AAPL still in quarantine — unmoved, untouched.
        assert (quarantine / "AAPL_1Day_all.parquet").exists()
        # iex/ never created.
        assert not (tmp_cache_dir / "iex").exists()
