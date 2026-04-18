"""
Unit tests for data/fetcher.py

Scope: pure functions and cache round-trip. No live Alpaca calls — anything
needing the real API lives in phase2_verify.py (integration) or behind the
`integration` marker.

Covers:
  - _validate: accepts clean frames, rejects bad ones, dedupes/sorts
  - _to_utc: naive → UTC; aware tz → converted to UTC
  - _missing_ranges: empty cache / front / back / full overlap / no overlap
  - is_fresh / require_fresh: fresh, stale, empty, edge cases
  - cache round-trip: _write_cache + _read_cache + _read_meta preserve data+tz
  - _with_retry: retries 429 / 5xx, does NOT retry 4xx, respects max_attempts
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from data import fetcher
from data.fetcher import (
    DataValidationError,
    StaleDataError,
    _missing_ranges,
    _read_cache,
    _read_meta,
    _to_utc,
    _validate,
    _with_retry,
    _write_cache,
    is_fresh,
    require_fresh,
)


# ── _validate ────────────────────────────────────────────────────────────────


class TestValidate:
    def test_accepts_clean_frame(self, clean_ohlcv):
        result = _validate(clean_ohlcv, "TEST")
        assert len(result) == len(clean_ohlcv)
        assert result.index.tz is not None

    def test_empty_frame_is_allowed(self):
        # Empty is a valid intermediate state (e.g. a gap fetch returned nothing).
        result = _validate(pd.DataFrame(), "TEST")
        assert result.empty

    def test_missing_column_raises(self, clean_ohlcv):
        bad = clean_ohlcv.drop(columns=["volume"])
        with pytest.raises(DataValidationError, match="missing OHLCV columns"):
            _validate(bad, "TEST")

    def test_naive_index_raises(self, make_ohlcv, utc_now):
        df = make_ohlcv(utc_now, 3)
        df.index = df.index.tz_localize(None)
        with pytest.raises(DataValidationError, match="not timezone-aware"):
            _validate(df, "TEST")

    def test_non_datetime_index_raises(self, clean_ohlcv):
        bad = clean_ohlcv.reset_index(drop=True)
        with pytest.raises(DataValidationError, match="not a DatetimeIndex"):
            _validate(bad, "TEST")

    def test_nan_in_ohlcv_raises(self, clean_ohlcv):
        bad = clean_ohlcv.copy()
        bad.loc[bad.index[1], "open"] = float("nan")
        with pytest.raises(DataValidationError, match="NaNs in OHLCV"):
            _validate(bad, "TEST")

    def test_non_numeric_column_raises(self, clean_ohlcv):
        bad = clean_ohlcv.copy()
        bad["close"] = bad["close"].astype(str)
        with pytest.raises(DataValidationError, match="not numeric"):
            _validate(bad, "TEST")

    def test_duplicates_are_dropped(self, clean_ohlcv):
        dup = pd.concat([clean_ohlcv, clean_ohlcv.iloc[[0]]])
        result = _validate(dup, "TEST")
        assert len(result) == len(clean_ohlcv)
        assert not result.index.has_duplicates

    def test_unsorted_index_is_sorted(self, clean_ohlcv):
        shuffled = clean_ohlcv.iloc[[2, 0, 4, 1, 3]]
        result = _validate(shuffled, "TEST")
        assert result.index.is_monotonic_increasing


# ── _to_utc ──────────────────────────────────────────────────────────────────


class TestToUtc:
    def test_naive_becomes_utc(self):
        naive = datetime(2026, 1, 1, 12, 0)
        result = _to_utc(naive)
        assert result.tzinfo == timezone.utc
        assert result.hour == 12  # no clock shift

    def test_utc_aware_unchanged(self):
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert _to_utc(aware) == aware

    def test_other_tz_converts_to_utc(self):
        # US/Eastern noon in winter = 17:00 UTC
        eastern = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
        result = _to_utc(eastern)
        assert result.tzinfo == timezone.utc
        assert result.hour == 17


# ── _missing_ranges ──────────────────────────────────────────────────────────


class TestMissingRanges:
    def _dt(self, day: int) -> datetime:
        return datetime(2026, 1, day, tzinfo=timezone.utc)

    def test_no_cache_returns_full_range(self):
        ranges = _missing_ranges(None, None, self._dt(1), self._dt(10))
        assert ranges == [(self._dt(1), self._dt(10))]

    def test_full_overlap_returns_empty(self):
        # Cache fully covers request → no fetch needed.
        ranges = _missing_ranges(self._dt(1), self._dt(20), self._dt(5), self._dt(15))
        assert ranges == []

    def test_extension_on_front_only(self):
        # Request earlier than cache start.
        ranges = _missing_ranges(self._dt(10), self._dt(20), self._dt(5), self._dt(15))
        assert ranges == [(self._dt(5), self._dt(10))]

    def test_extension_on_back_only(self):
        # Request past cache end.
        ranges = _missing_ranges(self._dt(1), self._dt(10), self._dt(5), self._dt(20))
        assert ranges == [(self._dt(10), self._dt(20))]

    def test_extension_on_both_sides(self):
        # Request wraps cache.
        ranges = _missing_ranges(self._dt(10), self._dt(15), self._dt(5), self._dt(20))
        assert ranges == [(self._dt(5), self._dt(10)), (self._dt(15), self._dt(20))]

    def test_request_entirely_before_cache(self):
        # Cache starts after request ends — front-range is clamped to request end.
        ranges = _missing_ranges(self._dt(20), self._dt(30), self._dt(1), self._dt(10))
        assert ranges == [(self._dt(1), self._dt(10))]


# ── is_fresh / require_fresh ─────────────────────────────────────────────────


class TestFreshness:
    def test_fresh_within_threshold(self, make_ohlcv, utc_now):
        df = make_ohlcv(utc_now - timedelta(minutes=10), 1)
        assert is_fresh(df, timedelta(hours=1)) is True

    def test_stale_outside_threshold(self, make_ohlcv, utc_now):
        df = make_ohlcv(utc_now - timedelta(days=10), 1)
        assert is_fresh(df, timedelta(hours=1)) is False

    def test_empty_is_never_fresh(self):
        assert is_fresh(pd.DataFrame(), timedelta(days=365)) is False

    def test_require_fresh_raises_on_stale(self, make_ohlcv, utc_now):
        df = make_ohlcv(utc_now - timedelta(days=10), 1)
        with pytest.raises(StaleDataError, match="older than"):
            require_fresh(df, timedelta(hours=1), "AAPL")

    def test_require_fresh_raises_on_empty(self):
        with pytest.raises(StaleDataError):
            require_fresh(pd.DataFrame(), timedelta(days=365), "AAPL")

    def test_require_fresh_passes_when_fresh(self, make_ohlcv, utc_now):
        df = make_ohlcv(utc_now - timedelta(minutes=1), 1)
        # Should not raise.
        require_fresh(df, timedelta(hours=1), "AAPL")


# ── Cache round-trip ─────────────────────────────────────────────────────────


class TestCacheRoundTrip:
    def test_write_then_read_preserves_data_and_tz(
        self, tmp_cache_dir, clean_ohlcv, utc_now
    ):
        cov_start = utc_now - timedelta(days=10)
        cov_end = utc_now
        _write_cache(clean_ohlcv, "AAPL", "1Day", "all", cov_start, cov_end)

        back = _read_cache("AAPL", "1Day", "all")
        assert not back.empty
        assert back.index.tz is not None
        pd.testing.assert_frame_equal(back, clean_ohlcv)

    def test_meta_round_trip(self, tmp_cache_dir, clean_ohlcv, utc_now):
        cov_start = utc_now - timedelta(days=10)
        cov_end = utc_now
        _write_cache(clean_ohlcv, "AAPL", "1Day", "all", cov_start, cov_end)

        start, end = _read_meta("AAPL", "1Day", "all")
        assert start == cov_start
        assert end == cov_end

    def test_empty_frame_does_not_write(self, tmp_cache_dir, utc_now):
        _write_cache(
            pd.DataFrame(),
            "EMPTY",
            "1Day",
            "all",
            utc_now,
            utc_now + timedelta(days=1),
        )
        assert list(tmp_cache_dir.iterdir()) == []

    def test_read_missing_returns_empty(self, tmp_cache_dir):
        assert _read_cache("NOTHERE", "1Day", "all").empty
        assert _read_meta("NOTHERE", "1Day", "all") == (None, None)

    def test_corrupt_meta_is_ignored(self, tmp_cache_dir):
        # Write a bogus meta file; _read_meta should return (None, None), not crash.
        (tmp_cache_dir / "ABC_1Day_all.meta.json").write_text("not-json{{")
        assert _read_meta("ABC", "1Day", "all") == (None, None)


# ── _with_retry ──────────────────────────────────────────────────────────────


class _FakeAPIError(fetcher.APIError):
    """
    APIError whose status_code is settable. The real class derives it from an
    internal error dict; tests just need the attribute readable by _with_retry.
    """

    def __init__(self, status: int, msg: str = "boom"):
        # Skip the real __init__ (which expects an error dict / response).
        Exception.__init__(self, msg)
        self._test_status = status

    @property  # type: ignore[override]
    def status_code(self):  # noqa: D401
        return self._test_status


def _api_error(status: int, msg: str = "boom") -> _FakeAPIError:
    return _FakeAPIError(status, msg)


class TestWithRetry:
    def test_success_on_first_try(self):
        fn = MagicMock(return_value="ok")
        assert _with_retry(fn, max_attempts=3, base_delay=0) == "ok"
        assert fn.call_count == 1

    def test_retries_on_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
        fn = MagicMock(side_effect=[_api_error(429), _api_error(429), "ok"])
        assert _with_retry(fn, max_attempts=5, base_delay=0) == "ok"
        assert fn.call_count == 3

    def test_retries_on_5xx(self, monkeypatch):
        monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
        fn = MagicMock(side_effect=[_api_error(503), "ok"])
        assert _with_retry(fn, max_attempts=3, base_delay=0) == "ok"
        assert fn.call_count == 2

    def test_does_not_retry_4xx_other_than_429(self, monkeypatch):
        monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
        fn = MagicMock(side_effect=_api_error(404))
        with pytest.raises(fetcher.APIError):
            _with_retry(fn, max_attempts=5, base_delay=0)
        assert fn.call_count == 1  # no retry

    def test_gives_up_after_max_attempts(self, monkeypatch):
        monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
        fn = MagicMock(side_effect=_api_error(429))
        with pytest.raises(fetcher.APIError):
            _with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    def test_retries_on_connection_error(self, monkeypatch):
        monkeypatch.setattr(fetcher.time, "sleep", lambda *_: None)
        fn = MagicMock(side_effect=[ConnectionError("net"), "ok"])
        assert _with_retry(fn, max_attempts=3, base_delay=0) == "ok"
        assert fn.call_count == 2
