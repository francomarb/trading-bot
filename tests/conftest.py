"""
Shared pytest fixtures.

Unit tests (the default suite) must be fully offline — they never hit Alpaca,
they never read from the real cache dir. Anything needing live data goes
behind the `integration` marker and is deselected in CI-style runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_ohlcv(
    start: datetime,
    n: int,
    step: timedelta = timedelta(days=1),
    base_price: float = 100.0,
) -> pd.DataFrame:
    """Build a clean synthetic OHLCV DataFrame with a tz-aware index."""
    idx = pd.DatetimeIndex([start + step * i for i in range(n)], tz="UTC")
    prices = [base_price + i for i in range(n)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [1_000 + i for i in range(n)],
        },
        index=idx,
    )


@pytest.fixture
def make_ohlcv():
    """Factory fixture so tests can build small synthetic frames."""
    return _make_ohlcv


@pytest.fixture
def clean_ohlcv(utc_now) -> pd.DataFrame:
    """A valid 5-bar OHLCV frame."""
    return _make_ohlcv(utc_now - timedelta(days=5), 5)


@pytest.fixture
def tmp_cache_dir(tmp_path: Path, monkeypatch) -> Path:
    """
    Redirect the fetcher's CACHE_DIR to a pytest tmp_path so cache-writing
    tests never pollute data/historical/.
    """
    from data import fetcher

    monkeypatch.setattr(fetcher, "CACHE_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def isolate_runtime_artifacts(tmp_path: Path, monkeypatch) -> None:
    """
    Redirect runtime write targets into pytest tmp space for every test.

    This prevents unit tests from polluting real project files such as:
      - data/trades.db
      - data/trades_live.db
      - data/engine_state.json
      - logs/*.jsonl / alerts.log
    """
    from config import settings

    monkeypatch.setattr(settings, "TRADE_LOG_DB_PAPER", str(tmp_path / "trades.db"))
    monkeypatch.setattr(settings, "TRADE_LOG_DB_LIVE", str(tmp_path / "trades_live.db"))
    monkeypatch.setattr(settings, "TRADE_LOG_DB", str(tmp_path / "trades.db"))
    monkeypatch.setattr(settings, "STATE_SNAPSHOT_PATH", str(tmp_path / "engine_state.json"))
    monkeypatch.setattr(settings, "JSON_LOG_FILE", str(tmp_path / "bot.jsonl"))
    monkeypatch.setattr(settings, "ALERT_LOG_FILE", str(tmp_path / "alerts.log"))
