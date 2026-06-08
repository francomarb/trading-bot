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
from datetime import datetime, timedelta, timezone
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
