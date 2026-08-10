"""
Unit tests for data/fetcher.py

Scope: pure functions and cache round-trip. No live Alpaca calls — anything
needing the real API belongs in a targeted manual paper check or behind the
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

import json
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests
from requests.adapters import HTTPAdapter

from data import fetcher
from data.fetcher import (
    HTTP_TIMEOUT_SECONDS,
    DataValidationError,
    StaleDataError,
    _TimeoutAdapter,
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
        _write_cache(clean_ohlcv, "AAPL", "1Day", "all", cov_start, cov_end, "iex")

        back = _read_cache("AAPL", "1Day", "all", "iex")
        assert not back.empty
        assert back.index.tz is not None
        pd.testing.assert_frame_equal(back, clean_ohlcv)

    def test_meta_round_trip(self, tmp_cache_dir, clean_ohlcv, utc_now):
        cov_start = utc_now - timedelta(days=10)
        cov_end = utc_now
        _write_cache(clean_ohlcv, "AAPL", "1Day", "all", cov_start, cov_end, "iex")

        start, end = _read_meta("AAPL", "1Day", "all", "iex")
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
            "iex",
        )
        # Neither legacy nor feed-aware paths should have been written.
        # The feed subdir may not even exist if _write_cache returned early.
        assert not (tmp_cache_dir / "EMPTY_1Day_all.parquet").exists()
        assert not (tmp_cache_dir / "iex" / "EMPTY_1Day_all.parquet").exists()

    def test_read_missing_returns_empty(self, tmp_cache_dir):
        assert _read_cache("NOTHERE", "1Day", "all", "iex").empty
        assert _read_meta("NOTHERE", "1Day", "all", "iex") == (None, None)

    def test_corrupt_meta_is_ignored(self, tmp_cache_dir):
        # Write a bogus meta file at the feed-aware path; _read_meta should
        # return (None, None), not crash.
        (tmp_cache_dir / "iex").mkdir(parents=True, exist_ok=True)
        (tmp_cache_dir / "iex" / "ABC_1Day_all.meta.json").write_text("not-json{{")
        assert _read_meta("ABC", "1Day", "all", "iex") == (None, None)


class TestFeedAwareCacheLayout:
    """
    PR review on PR #49 surfaced that the cache used to silently mix bars
    from different data feeds in the same parquet file. This test family
    pins the feed-aware layout:

      data/historical/{feed}/{symbol}_{timeframe}_{adjustment}.parquet

    Writes always go to the feed-aware path; reads check there first and
    fall back to the legacy top-level path for IEX only (so the live bot
    keeps working between the code deploy and the migration script run).
    """

    def test_write_creates_feed_subdir(self, tmp_cache_dir, clean_ohlcv, utc_now):
        _write_cache(
            clean_ohlcv, "AAPL", "1Day", "all",
            utc_now - timedelta(days=10), utc_now,
            "iex",
        )
        assert (tmp_cache_dir / "iex" / "AAPL_1Day_all.parquet").exists()
        assert (tmp_cache_dir / "iex" / "AAPL_1Day_all.meta.json").exists()
        # Nothing at the legacy top level.
        assert not (tmp_cache_dir / "AAPL_1Day_all.parquet").exists()

    def test_writes_to_different_feeds_are_isolated(
        self, tmp_cache_dir, make_ohlcv, utc_now
    ):
        # Two writes for the same symbol but different feeds must land in
        # separate subdirs and not clobber each other.
        iex_df = make_ohlcv(utc_now - timedelta(days=10), 10, base_price=100.0)
        sip_df = make_ohlcv(utc_now - timedelta(days=10), 10, base_price=200.0)

        _write_cache(iex_df, "AAPL", "1Day", "all", utc_now - timedelta(days=10), utc_now, "iex")
        _write_cache(sip_df, "AAPL", "1Day", "all", utc_now - timedelta(days=10), utc_now, "sip")

        back_iex = _read_cache("AAPL", "1Day", "all", "iex")
        back_sip = _read_cache("AAPL", "1Day", "all", "sip")
        assert back_iex["close"].iloc[0] == 100.0
        assert back_sip["close"].iloc[0] == 200.0

    def test_iex_read_falls_back_to_legacy_path(
        self, tmp_cache_dir, clean_ohlcv, utc_now
    ):
        # Simulate a pre-migration state: legacy top-level file exists, no
        # iex/ subdir. Reading with feed="iex" must find it via fallback.
        clean_ohlcv.to_parquet(tmp_cache_dir / "LEGACY_1Day_all.parquet")
        meta = {
            "covered_start": (utc_now - timedelta(days=10)).isoformat(),
            "covered_end": utc_now.isoformat(),
        }
        (tmp_cache_dir / "LEGACY_1Day_all.meta.json").write_text(
            json.dumps(meta)
        )
        back = _read_cache("LEGACY", "1Day", "all", "iex")
        assert not back.empty
        cov_start, cov_end = _read_meta("LEGACY", "1Day", "all", "iex")
        assert cov_start is not None and cov_end is not None

    def test_sip_read_does_not_fall_back_to_legacy(
        self, tmp_cache_dir, clean_ohlcv, utc_now
    ):
        # Legacy files were always IEX-fed. A SIP read MUST NOT silently
        # serve them — that would reintroduce the cross-feed cache mixing
        # bug this PR is fixing.
        clean_ohlcv.to_parquet(tmp_cache_dir / "LEGACY_1Day_all.parquet")
        (tmp_cache_dir / "LEGACY_1Day_all.meta.json").write_text(json.dumps({
            "covered_start": (utc_now - timedelta(days=10)).isoformat(),
            "covered_end": utc_now.isoformat(),
        }))
        back = _read_cache("LEGACY", "1Day", "all", "sip")
        assert back.empty
        assert _read_meta("LEGACY", "1Day", "all", "sip") == (None, None)

    def test_feed_aware_takes_precedence_over_legacy(
        self, tmp_cache_dir, make_ohlcv, utc_now
    ):
        # If both legacy and feed-aware paths exist for IEX, the feed-aware
        # one wins (it's newer by definition).
        legacy_df = make_ohlcv(utc_now - timedelta(days=10), 10, base_price=999.0)
        new_df = make_ohlcv(utc_now - timedelta(days=10), 10, base_price=42.0)

        legacy_df.to_parquet(tmp_cache_dir / "OVL_1Day_all.parquet")
        _write_cache(
            new_df, "OVL", "1Day", "all",
            utc_now - timedelta(days=10), utc_now, "iex",
        )
        back = _read_cache("OVL", "1Day", "all", "iex")
        assert back["close"].iloc[0] == 42.0  # the feed-aware one


class TestStrictFeedValidation:
    """
    PR #50 review caught: pre-validation, ``_FEED_MAP.get(feed, DataFeed.IEX)``
    silently fell through to IEX for unknown feeds while the cache layer still
    used the raw string for its subdir name. A typo like ``feed="six"`` would
    create ``data/historical/six/`` with IEX bars in it, then on the next call
    the IEX synthetic-SIP volume scaling was NOT applied (only the exact string
    "iex" triggers it). Strict validation prevents the silently-wrong-volume
    failure mode.
    """

    def test_unknown_feed_string_raises(self, tmp_cache_dir, utc_now):
        with pytest.raises(ValueError, match="feed must be one of"):
            fetcher.fetch_symbol(
                "AAPL",
                start=utc_now - timedelta(days=30),
                end=utc_now,
                timeframe="1Day",
                use_cache=False,
                feed="six",  # typo
            )

    def test_empty_feed_string_raises(self, tmp_cache_dir, utc_now):
        with pytest.raises(ValueError, match="feed must be one of"):
            fetcher.fetch_symbol(
                "AAPL",
                start=utc_now - timedelta(days=30),
                end=utc_now,
                timeframe="1Day",
                use_cache=False,
                feed="",
            )

    def test_non_string_feed_raises(self, tmp_cache_dir, utc_now):
        with pytest.raises(ValueError, match="feed must be a str"):
            fetcher.fetch_symbol(
                "AAPL",
                start=utc_now - timedelta(days=30),
                end=utc_now,
                timeframe="1Day",
                use_cache=False,
                feed=42,  # type: ignore[arg-type]
            )

    def test_case_insensitive_accepted(self, tmp_cache_dir, utc_now, monkeypatch):
        # ``IEX`` and ``Iex`` should be accepted as well, normalised to lower-case.
        captured: dict = {}

        def fake_api(symbol, timeframe, start, end, adjustment, feed):
            captured["feed"] = feed
            return pd.DataFrame()

        monkeypatch.setattr(fetcher, "_fetch_bars_api", fake_api)
        fetcher.fetch_symbol(
            "AAPL",
            start=utc_now - timedelta(days=30),
            end=utc_now,
            timeframe="1Day",
            use_cache=False,
            feed="IEX",
        )
        assert captured["feed"] == "iex"


class TestSipEndClamp:
    """
    Basic Alpaca accounts can query SIP historical data only for bars whose
    timestamp is at least 15 minutes old. The fetcher must clamp the
    requested `end` rather than letting the API return 422. Captured by
    monkeypatching the bars-API hook and asserting what `end` it sees.
    """

    @pytest.fixture
    def captured_api(self, monkeypatch, tmp_cache_dir):
        # Stub _fetch_bars_api to capture the (start, end) it gets called with
        # and return an empty frame so fetch_symbol doesn't try to merge data.
        captured: dict = {}

        def fake_api(symbol, timeframe, start, end, adjustment, feed):
            captured["start"] = start
            captured["end"] = end
            captured["feed"] = feed
            return pd.DataFrame()

        monkeypatch.setattr(fetcher, "_fetch_bars_api", fake_api)
        # Force every call to be a "missing range" so the API gets hit.
        return captured

    def test_sip_end_is_clamped_when_too_recent(self, captured_api, utc_now):
        # Asking for end = now should get clamped to ~now-15min for SIP.
        sip_cutoff_window = timedelta(minutes=16)  # generous margin
        fetcher.fetch_symbol(
            "AAPL",
            start=utc_now - timedelta(days=30),
            end=utc_now,
            timeframe="1Day",
            use_cache=False,
            feed="sip",
        )
        # captured["end"] is the end the API was called with, AFTER the
        # clamp. It must be at least 15 minutes before now.
        delta_to_now = datetime.now(timezone.utc) - captured_api["end"]
        assert delta_to_now >= timedelta(minutes=15) - timedelta(seconds=5), (
            f"SIP end was not clamped: captured end={captured_api['end']}, "
            f"now-end delta={delta_to_now}"
        )
        assert delta_to_now < sip_cutoff_window  # not over-clamped

    def test_iex_end_is_not_clamped(self, captured_api, utc_now):
        # IEX has no 15-min restriction; the captured end must equal what
        # the caller asked for.
        requested_end = utc_now
        fetcher.fetch_symbol(
            "AAPL",
            start=utc_now - timedelta(days=30),
            end=requested_end,
            timeframe="1Day",
            use_cache=False,
            feed="iex",
        )
        assert captured_api["end"] == requested_end

    def test_sip_end_well_in_past_is_left_alone(self, captured_api, utc_now):
        # SIP backtests on bars from years ago must not see any clamping —
        # the requested end is already comfortably past the 15-min cutoff.
        old_end = utc_now - timedelta(days=365)
        fetcher.fetch_symbol(
            "AAPL",
            start=utc_now - timedelta(days=400),
            end=old_end,
            timeframe="1Day",
            use_cache=False,
            feed="sip",
        )
        assert captured_api["end"] == old_end

    def test_sip_window_entirely_in_delay_raises(self, captured_api, utc_now):
        # Reviewer P2: a recent-only SIP request (start=now-10min, end=now-5min)
        # passes the initial start<end check, but the SIP end-clamp collapses
        # end to now-15min, making end < start. Without re-validation the
        # caller gets a misleading "no bars" return instead of a clear error.
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="SIP window collapsed"):
            fetcher.fetch_symbol(
                "AAPL",
                start=now - timedelta(minutes=10),
                end=now - timedelta(minutes=5),
                timeframe="1Day",
                use_cache=False,
                feed="sip",
            )


class TestTimeoutAdapter:
    def test_sets_default_timeout_when_missing(self, monkeypatch):
        adapter = _TimeoutAdapter()
        request = requests.Request("GET", "https://example.com").prepare()
        captured: dict[str, object] = {}

        def fake_send(self, req, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(HTTPAdapter, "send", fake_send)

        adapter.send(request)

        assert captured["timeout"] == HTTP_TIMEOUT_SECONDS

    def test_overrides_explicit_none_timeout(self, monkeypatch):
        adapter = _TimeoutAdapter()
        request = requests.Request("GET", "https://example.com").prepare()
        captured: dict[str, object] = {}

        def fake_send(self, req, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(HTTPAdapter, "send", fake_send)

        adapter.send(request, timeout=None)

        assert captured["timeout"] == HTTP_TIMEOUT_SECONDS

    def test_preserves_explicit_timeout(self, monkeypatch):
        adapter = _TimeoutAdapter()
        request = requests.Request("GET", "https://example.com").prepare()
        captured: dict[str, object] = {}

        def fake_send(self, req, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(HTTPAdapter, "send", fake_send)

        adapter.send(request, timeout=5)

        assert captured["timeout"] == 5


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


# ── Arrival-price quote ──────────────────────────────────────────────────────


class TestFetchLatestQuoteMidpoint:
    """`fetch_latest_quote_midpoint` is the arrival-price benchmark for
    execution-quality slippage measurement (Issue B in the slippage PR).
    It must be defensive: a malformed quote, one-sided book, or API
    failure must return None rather than raising into the trading loop.
    """

    @pytest.fixture
    def mock_client(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(fetcher, "_get_client", lambda: client)
        return client

    def test_returns_midpoint_of_two_sided_quote(self, mock_client):
        quote = MagicMock(bid_price=100.0, ask_price=100.20)
        mock_client.get_stock_latest_quote.return_value = {"AAPL": quote}
        assert fetcher.fetch_latest_quote_midpoint("AAPL") == pytest.approx(100.10)

    def test_returns_none_on_zero_bid(self, mock_client):
        """One-sided book (pre-market, halt, illiquid) is not a usable
        arrival price — return None rather than synthesizing a midpoint."""
        quote = MagicMock(bid_price=0.0, ask_price=100.20)
        mock_client.get_stock_latest_quote.return_value = {"AAPL": quote}
        assert fetcher.fetch_latest_quote_midpoint("AAPL") is None

    def test_returns_none_on_zero_ask(self, mock_client):
        quote = MagicMock(bid_price=100.0, ask_price=0.0)
        mock_client.get_stock_latest_quote.return_value = {"AAPL": quote}
        assert fetcher.fetch_latest_quote_midpoint("AAPL") is None

    def test_returns_none_on_missing_symbol(self, mock_client):
        mock_client.get_stock_latest_quote.return_value = {}
        assert fetcher.fetch_latest_quote_midpoint("AAPL") is None

    def test_returns_none_on_api_error(self, mock_client):
        from alpaca.common.exceptions import APIError
        mock_client.get_stock_latest_quote.side_effect = APIError(
            {"message": "rate limited"}
        )
        # Must not raise — broker quote failures cannot stop the trading loop.
        assert fetcher.fetch_latest_quote_midpoint("AAPL") is None

    def test_returns_none_on_empty_symbol(self, mock_client):
        # Defensive: empty / None input doesn't even hit the API.
        assert fetcher.fetch_latest_quote_midpoint("") is None
        mock_client.get_stock_latest_quote.assert_not_called()

    def test_returns_none_on_unparseable_prices(self, mock_client):
        quote = MagicMock(bid_price="not a number", ask_price=100.20)
        mock_client.get_stock_latest_quote.return_value = {"AAPL": quote}
        assert fetcher.fetch_latest_quote_midpoint("AAPL") is None


class TestCoverageRecordsTheResponse:
    """PLAN 11.52 — the sidecar meta must record what came BACK, not what
    was asked for.

    It used to store the requested end. When a catch-up fetch came back
    short, the missing tail was still stamped covered, and `_missing_ranges`
    only ever asks for bars before covered_start or after covered_end — so
    those sessions were never requested again. Confirmed 2026-07-31: after
    the laptop was off 2026-07-13 -> 07-20, 92 of 106 SIP symbols lost that
    entire trading week, undetectable from the metadata.
    """

    def _bars(self, days: list[str]) -> pd.DataFrame:
        idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days])
        return pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
            index=idx,
        )

    def _meta(self, tmp_cache_dir, symbol="AAPL", feed="iex"):
        import json
        p = tmp_cache_dir / feed / f"{symbol}_1Day_all.meta.json"
        return json.loads(p.read_text())

    def test_short_response_does_not_claim_the_missing_tail(
        self, tmp_cache_dir, monkeypatch
    ):
        """The exact 11.52 mechanism: ask for a week, get back two days."""
        start = datetime(2026, 7, 13, tzinfo=timezone.utc)
        end = datetime(2026, 7, 20, tzinfo=timezone.utc)

        monkeypatch.setattr(
            fetcher, "_fetch_bars_api",
            lambda *a, **k: self._bars(["2026-07-13", "2026-07-14"]),
        )
        fetcher.fetch_symbol("AAPL", start=start, end=end,
                             timeframe="1Day", feed="iex")

        covered_end = datetime.fromisoformat(self._meta(tmp_cache_dir)["covered_end"])
        # Must be the last bar we actually got, NOT the 07-20 we requested.
        assert covered_end.date() == date(2026, 7, 14)
        assert covered_end.date() != end.date()

    def test_the_missing_tail_is_re_requested_next_time(
        self, tmp_cache_dir, monkeypatch
    ):
        """The consequence that matters. Without the fix the second call
        sees the range as covered and never asks again — which is how the
        holes became permanent."""
        start = datetime(2026, 7, 13, tzinfo=timezone.utc)
        end = datetime(2026, 7, 20, tzinfo=timezone.utc)

        monkeypatch.setattr(
            fetcher, "_fetch_bars_api",
            lambda *a, **k: self._bars(["2026-07-13", "2026-07-14"]),
        )
        fetcher.fetch_symbol("AAPL", start=start, end=end,
                             timeframe="1Day", feed="iex")

        asked: list[tuple] = []

        def record(symbol, timeframe, s, e, adjustment, feed):
            asked.append((s.date(), e.date()))
            return self._bars(["2026-07-15", "2026-07-16", "2026-07-17"])

        monkeypatch.setattr(fetcher, "_fetch_bars_api", record)
        fetcher.fetch_symbol("AAPL", start=start, end=end,
                             timeframe="1Day", feed="iex")

        assert asked, "second call made no API request — the tail was lost"
        # The END is NOT the discriminator: `effective_cov_end` already
        # subtracts one bar interval to force a re-fetch of the latest bar,
        # so a request reaching 07-20 happens either way. What matters is
        # that the refetch reaches BACK far enough to cover 07-15..07-17 —
        # the sessions the short response never delivered. Without the fix
        # coverage claims 07-20, so the request starts at 07-19 and the
        # hole is skipped permanently.
        earliest_asked = min(s for s, _ in asked)
        assert earliest_asked <= date(2026, 7, 15), (
            f"refetch started at {earliest_asked} — it skipped the hole at "
            "2026-07-15..07-17 rather than filling it"
        )

    def test_complete_response_still_records_full_coverage(
        self, tmp_cache_dir, monkeypatch
    ):
        """No behaviour change on the happy path — a response that reaches
        the requested end still claims it, so normal fetches don't start
        re-requesting ranges they already have."""
        start = datetime(2026, 7, 13, tzinfo=timezone.utc)
        end = datetime(2026, 7, 17, tzinfo=timezone.utc)
        monkeypatch.setattr(
            fetcher, "_fetch_bars_api",
            lambda *a, **k: self._bars(
                ["2026-07-13", "2026-07-14", "2026-07-15",
                 "2026-07-16", "2026-07-17"]
            ),
        )
        fetcher.fetch_symbol("AAPL", start=start, end=end,
                             timeframe="1Day", feed="iex")
        covered_end = datetime.fromisoformat(self._meta(tmp_cache_dir)["covered_end"])
        assert covered_end.date() == date(2026, 7, 17)

    def test_start_side_is_deliberately_not_clamped(
        self, tmp_cache_dir, monkeypatch
    ):
        """Feed-depth boundaries live on the start side: SIP begins
        2016-01-04, so asking earlier legitimately returns nothing before
        it. Clamping there would re-request the missing years forever, so
        the requested start is recorded on purpose."""
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 17, tzinfo=timezone.utc)
        monkeypatch.setattr(
            fetcher, "_fetch_bars_api",
            lambda *a, **k: self._bars(["2026-07-15", "2026-07-16", "2026-07-17"]),
        )
        fetcher.fetch_symbol("AAPL", start=start, end=end,
                             timeframe="1Day", feed="iex")
        covered_start = datetime.fromisoformat(
            self._meta(tmp_cache_dir)["covered_start"]
        )
        assert covered_start.date() == date(2026, 7, 1)


class TestUseCacheFalseIsNonDestructive:
    """PLAN 11.52(b) — `use_cache=False` means bypass, not discard.

    It blanked `cached` and then let `_write_cache` overwrite the file with
    only the fetched window, so an ad-hoc diagnostic fetch silently
    truncated a symbol's whole history. On 2026-07-31 this took MU from
    2659 rows to 125, and AAPL to 22.
    """

    def _bars(self, days):
        idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days])
        return pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
            index=idx,
        )

    def test_diagnostic_fetch_leaves_the_cache_file_untouched(
        self, tmp_cache_dir, monkeypatch
    ):
        # Seed a cache with a wide history.
        wide = [f"2026-06-{d:02d}" for d in range(1, 26)]
        monkeypatch.setattr(fetcher, "_fetch_bars_api",
                            lambda *a, **k: self._bars(wide))
        fetcher.fetch_symbol(
            "MU", start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 25, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex",
        )
        cache_file = tmp_cache_dir / "iex" / "MU_1Day_all.parquet"
        before = cache_file.read_bytes()
        assert len(pd.read_parquet(cache_file)) == len(wide)

        # A narrow diagnostic read with use_cache=False.
        monkeypatch.setattr(
            fetcher, "_fetch_bars_api",
            lambda *a, **k: self._bars(["2026-06-24", "2026-06-25"]),
        )
        df, _ = fetcher.fetch_symbol(
            "MU", start=datetime(2026, 6, 24, tzinfo=timezone.utc),
            end=datetime(2026, 6, 25, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex", use_cache=False,
        )

        assert len(df) == 2, "the caller still gets exactly what it asked for"
        assert cache_file.read_bytes() == before, (
            "use_cache=False truncated the cache file"
        )
        assert len(pd.read_parquet(cache_file)) == len(wide)

    def test_use_cache_false_does_not_create_a_cache_file(
        self, tmp_cache_dir, monkeypatch
    ):
        monkeypatch.setattr(
            fetcher, "_fetch_bars_api",
            lambda *a, **k: self._bars(["2026-06-24", "2026-06-25"]),
        )
        fetcher.fetch_symbol(
            "NVDA", start=datetime(2026, 6, 24, tzinfo=timezone.utc),
            end=datetime(2026, 6, 25, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex", use_cache=False,
        )
        assert not (tmp_cache_dir / "iex" / "NVDA_1Day_all.parquet").exists()


class TestJuly2026ShutdownRegression:
    """The specific incident 11.52 documents, end to end.

    2026-07-13 → 07-20 the laptop was off. On resume the bot issued one
    large catch-up fetch; the meta recorded the REQUEST, so whatever subset
    came back was accepted silently and the missing sessions sat inside
    [covered_start, covered_end] forever. 92 of 106 SIP symbols lost that
    trading week, undetectable from the metadata, and it later produced a
    wrong analytical result (the ANET counterfactual in 11.54).

    Two things these tests do that the rest of the file does not:

    1. **Start from a POPULATED cache.** July began with history and a
       shutdown gap, which takes a different branch — cov_start/cov_end are
       set, `_missing_ranges` computes a tail range, and the response
       merges into existing bars. Empty-cache tests never exercise it.
    2. **Use an API fake that respects the requested window.** A fake that
       returns the missing bars regardless of what was asked lets the buggy
       code "heal" the hole for the wrong reason — it passed against the
       original fetcher until this was fixed.
    """

    def _bars(self, days):
        idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days])
        return pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
            index=idx,
        )

    def _api(self, available, log=None):
        """A fake that behaves like Alpaca: only ever returns bars INSIDE
        the requested range. Anything looser makes these tests vacuous."""
        def fake(symbol, timeframe, s, e, adjustment, feed):
            if log is not None:
                log.append((s.date(), e.date()))
            inside = [
                d for d in available
                if s.date() <= date.fromisoformat(d) <= e.date()
            ]
            return self._bars(inside) if inside else pd.DataFrame()
        return fake

    def _cached_days(self, tmp_cache_dir, symbol="AAPL", feed="iex"):
        f = tmp_cache_dir / feed / f"{symbol}_1Day_all.parquet"
        return {str(t.date()) for t in pd.read_parquet(f).index}

    BEFORE = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
    RETURNED_FIRST = ["2026-07-13", "2026-07-14"]      # short catch-up response
    MISSING = ["2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20"]

    def _resume(self, monkeypatch, available, log=None, end_day=21):
        monkeypatch.setattr(fetcher, "_fetch_bars_api", self._api(available, log))
        fetcher.fetch_symbol(
            "AAPL",
            start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, end_day, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex",
        )

    def _seed_and_hole(self, monkeypatch):
        """Cache with pre-shutdown history, then a resume that comes back
        short — the exact July state."""
        monkeypatch.setattr(fetcher, "_fetch_bars_api", self._api(self.BEFORE))
        fetcher.fetch_symbol(
            "AAPL",
            start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, 10, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex",
        )
        self._resume(monkeypatch, self.BEFORE + self.RETURNED_FIRST)

    def test_the_july_hole_is_filled_on_the_next_cycle(
        self, tmp_cache_dir, monkeypatch
    ):
        """The whole point: the hole must not be permanent.

        Without the fix the resume stamps covered_end at the requested
        07-21, so the next cycle only asks from 07-20 — and a realistic API
        returns nothing before that, leaving 07-15..07-17 gone for good.
        """
        self._seed_and_hole(monkeypatch)
        holed = self._cached_days(tmp_cache_dir)
        assert not (set(self.MISSING) & holed), "fixture wrong: no hole created"

        # Next cycle — the API now has the full week available.
        self._resume(monkeypatch, self.BEFORE + self.RETURNED_FIRST + self.MISSING)

        final = self._cached_days(tmp_cache_dir)
        assert set(self.MISSING) <= final, (
            f"the July hole survived — still missing "
            f"{sorted(set(self.MISSING) - final)}"
        )
        assert set(self.BEFORE) <= final, "pre-shutdown history was lost"

    def test_the_refetch_reaches_back_into_the_holed_week(
        self, tmp_cache_dir, monkeypatch
    ):
        """The mechanism, asserted on what the API is actually asked for."""
        self._seed_and_hole(monkeypatch)
        asked = []
        self._resume(
            monkeypatch, self.BEFORE + self.RETURNED_FIRST + self.MISSING, log=asked
        )
        assert asked, "no refetch at all — the week was written off"
        assert min(s for s, _ in asked) <= date(2026, 7, 15), (
            f"refetch started at {min(s for s, _ in asked)}, after the hole at "
            "2026-07-15 — this is exactly the July failure"
        )

    def test_repeated_cycles_converge_and_stop_fetching(
        self, tmp_cache_dir, monkeypatch
    ):
        """Once whole, the cache must settle: no endless re-request loop.

        A fix that healed holes by refetching forever would trade a silent
        bug for a permanent API cost.
        """
        self._seed_and_hole(monkeypatch)
        whole = self.BEFORE + self.RETURNED_FIRST + self.MISSING
        for _ in range(3):
            self._resume(monkeypatch, whole)
        assert set(self.MISSING) <= self._cached_days(tmp_cache_dir)

        calls = []
        self._resume(monkeypatch, whole, log=calls, end_day=20)
        # At most the single trailing-bar refresh `effective_cov_end` forces.
        assert len(calls) <= 1, (
            f"settled cache still made {len(calls)} API calls — re-request loop"
        )


class TestSessionGapDetection:
    """PLAN 11.52 — interval coverage cannot see a gap in the MIDDLE.

    `_missing_ranges` only ever asks for bars before covered_start or after
    covered_end, so clamping covered_end to the last bar received heals a
    TAIL truncation and nothing else. Measured before this was added, of
    the four plausible catch-up response shapes only two healed:

        tail truncation  (07-13, 07-14)  -> healed
        interior drop    (07-13, 07-20)  -> STILL HOLED
        start truncation (07-20 only)    -> STILL HOLED
        empty response                   -> healed

    Diffing what we hold against a real session calendar closes all four.
    """

    BEFORE = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
    WEEK = ["2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16",
            "2026-07-17", "2026-07-20"]

    @pytest.fixture(autouse=True)
    def _calendar(self, monkeypatch):
        """Deterministic calendar — never the network, never a cached symbol."""
        from data import market_calendar
        every = self.BEFORE + self.WEEK
        monkeypatch.setattr(
            market_calendar, "trading_sessions",
            lambda s, e: {
                date.fromisoformat(d) for d in every
                if s <= date.fromisoformat(d) <= e
            },
        )
        market_calendar._reset_for_tests()

    def _bars(self, days):
        if not days:
            return pd.DataFrame()
        return pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
            index=pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days]),
        )

    def _api(self, available, log=None):
        def fake(symbol, timeframe, s, e, adjustment, feed):
            if log is not None:
                log.append((s.date(), e.date()))
            inside = [d for d in available
                      if s.date() <= date.fromisoformat(d) <= e.date()]
            return self._bars(inside) if inside else pd.DataFrame()
        return fake

    def _cycle(self, monkeypatch, available, log=None, end_day=21):
        monkeypatch.setattr(fetcher, "_fetch_bars_api", self._api(available, log))
        fetcher.fetch_symbol(
            "AAPL", start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, end_day, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex",
        )

    def _days(self, tmp_cache_dir):
        f = tmp_cache_dir / "iex" / "AAPL_1Day_all.parquet"
        return {str(t.date()) for t in pd.read_parquet(f).index}

    @pytest.mark.parametrize("shape,resume", [
        ("tail_truncation", ["2026-07-13", "2026-07-14"]),
        ("interior_drop", ["2026-07-13", "2026-07-20"]),
        ("start_truncation", ["2026-07-20"]),
        ("empty_response", []),
    ])
    def test_every_catch_up_response_shape_heals(
        self, tmp_cache_dir, monkeypatch, shape, resume
    ):
        """The acceptance matrix. Two of these fail on tail-clamping alone."""
        self._cycle(monkeypatch, self.BEFORE, end_day=10)
        self._cycle(monkeypatch, self.BEFORE + resume)          # short catch-up
        for _ in range(2):
            self._cycle(monkeypatch, self.BEFORE + self.WEEK)   # data now available

        missing = set(self.WEEK) - self._days(tmp_cache_dir)
        assert not missing, f"{shape}: sessions never recovered — {sorted(missing)}"

    def test_genuinely_absent_sessions_stop_being_requested(
        self, tmp_cache_dir, monkeypatch
    ):
        """`iex/SPY` has a real 634-day feed-depth gap. Chasing it every
        call for the life of the cache would trade a silent bug for a
        permanent API cost, so a session missed twice is retired."""
        never = {"2026-07-08", "2026-07-09"}
        available = [d for d in self.BEFORE + self.WEEK if d not in never]

        for _ in range(3):
            self._cycle(monkeypatch, available, end_day=20)

        meta = json.loads(
            (tmp_cache_dir / "iex" / "AAPL_1Day_all.meta.json").read_text()
        )
        assert set(meta.get("absent_sessions", [])) >= never

        calls: list = []
        self._cycle(monkeypatch, available, log=calls, end_day=20)
        assert len(calls) <= 1, (
            f"settled cache made {len(calls)} calls — absent sessions are "
            "still being chased"
        )

    def test_first_miss_is_retried_before_being_written_off(
        self, tmp_cache_dir, monkeypatch
    ):
        """One strike is not enough. Retiring on the first miss is what
        would have hidden the July incident — a truncated response looks
        identical to absent data until you ask again."""
        self._cycle(monkeypatch, self.BEFORE, end_day=10)
        self._cycle(monkeypatch, self.BEFORE + ["2026-07-13"])

        meta = json.loads(
            (tmp_cache_dir / "iex" / "AAPL_1Day_all.meta.json").read_text()
        )
        absent = set(meta.get("absent_sessions", []))
        assert "2026-07-14" not in absent, (
            "a single miss retired the session — a truncated response would "
            "never be retried"
        )

    def test_calendar_unavailable_falls_back_to_previous_behaviour(
        self, tmp_cache_dir, monkeypatch
    ):
        """A market-data outage must never block a fetch. Cost of failing
        closed is worse than the hole it would prevent."""
        from data import market_calendar
        monkeypatch.setattr(market_calendar, "trading_sessions",
                            lambda s, e: None)
        self._cycle(monkeypatch, self.BEFORE, end_day=10)
        df, _ = fetcher.fetch_symbol(
            "AAPL", start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, 10, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex",
        )
        assert len(df) == len(self.BEFORE)

    def test_gap_detection_never_consults_a_cached_symbol(self):
        """11.52: a sweep using SPY as the session reference reported
        'clean' while 92 of 106 symbols were holed, because SPY is cached
        by the same code and shared the gap. The calendar module must not
        read the bar cache at all."""
        src = (Path(fetcher.__file__).parent / "market_calendar.py").read_text()
        assert "_read_cache" not in src
        assert "read_parquet" not in src


class TestCalendarIsolation:
    """The calendar must never reach the network or the real cache file
    from a unit test.

    `market_calendar` writes to a fixed `data/historical/.market_calendar.json`
    and falls back to a live Alpaca call. `tmp_cache_dir` only redirects
    `fetcher.CACHE_DIR`, so before the `isolate_market_calendar` autouse
    fixture existed, any test on the daily fetch path did both — a run on
    2026-08-10 left a real file holding 290 genuine trading sessions.
    """

    def test_cache_path_is_redirected_out_of_the_repo(self):
        from data import market_calendar
        assert "data/historical" not in str(market_calendar._CACHE_PATH)

    def test_no_real_calendar_file_is_written(self, tmp_cache_dir, monkeypatch):
        from data import market_calendar
        real = Path(fetcher.__file__).parent / "historical" / ".market_calendar.json"
        existed = real.exists()

        monkeypatch.setattr(
            fetcher, "_fetch_bars_api",
            lambda *a, **k: pd.DataFrame(
                {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
                index=pd.DatetimeIndex([pd.Timestamp("2026-07-06", tz="UTC")]),
            ),
        )
        fetcher.fetch_symbol(
            "AAPL", start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, 10, tzinfo=timezone.utc),
            timeframe="1Day", feed="iex",
        )
        market_calendar.trading_sessions(date(2026, 7, 1), date(2026, 7, 10))
        assert real.exists() == existed, "a unit test wrote the real calendar cache"

    def test_the_live_fetch_path_is_stubbed_out(self):
        """The fixture replaces `_fetch`, so an unstubbed `trading_sessions`
        degrades to the fail-open None rather than calling Alpaca."""
        from data import market_calendar
        assert market_calendar._fetch(date(2026, 1, 1), date(2026, 1, 2)) is None
        assert market_calendar.trading_sessions(
            date(2026, 1, 1), date(2026, 1, 2)
        ) is None
