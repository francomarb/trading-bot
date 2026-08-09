"""
Unit tests for scripts.credit_spread_delta_audit (PLAN 11.57).

Covers the pure maths (OCC decoding, Black-Scholes helpers, implied-vol
inversion, the error decomposition) and the trade-log pairing rule. No
network, no live data — `build_report` is the only I/O-bearing function
and is exercised through its pure pieces.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest

from scripts import credit_spread_delta_audit as audit
from scripts.credit_spread_delta_audit import (
    OccContract,
    SpreadEntry,
    decompose_delta_error,
    implied_sigma_from_credit,
    load_entries_from_db,
    parse_occ,
    put_delta,
    spread_credit,
)


class TestParseOcc:
    def test_decodes_a_standard_symbol(self):
        c = parse_occ("QQQ260821P00690000")
        assert c == OccContract(
            underlying="QQQ", expiry=date(2026, 8, 21), right="P", strike=690.0
        )

    @pytest.mark.parametrize(
        "occ,underlying",
        [
            ("F260821P00012000", "F"),
            ("SPY260821P00714000", "SPY"),
            ("GOOG260821P00190000", "GOOG"),
        ],
    )
    def test_underlying_length_does_not_shift_the_fields(self, occ, underlying):
        """
        The layout is fixed-width from the RIGHT. Slicing from the left
        silently mangles 1-, 2- and 4-letter tickers.
        """
        c = parse_occ(occ)
        assert c.underlying == underlying
        assert c.expiry == date(2026, 8, 21)
        assert c.right == "P"

    def test_fractional_strikes_survive(self):
        assert parse_occ("SPY260821P00714500").strike == pytest.approx(714.5)

    def test_rejects_a_bare_tail_with_no_underlying(self):
        """A tail with no ticker in front is exactly 15 chars, so the
        length guard is what catches it — there is no separate branch."""
        with pytest.raises(ValueError, match="not an OCC symbol"):
            parse_occ("260821P00690000")

    def test_rejects_bad_right(self):
        with pytest.raises(ValueError, match="P or C"):
            parse_occ("QQQ260821X00690000")

    def test_rejects_too_short(self):
        with pytest.raises(ValueError, match="not an OCC symbol"):
            parse_occ("QQQ26")


class TestBlackScholesHelpers:
    def test_put_delta_grows_as_spot_falls_toward_the_strike(self):
        far = put_delta(spot=750, strike=700, dte_days=35, sigma=0.16)
        near = put_delta(spot=710, strike=700, dte_days=35, sigma=0.16)
        itm = put_delta(spot=680, strike=700, dte_days=35, sigma=0.16)
        assert far < near < itm
        assert itm > 0.5

    def test_put_delta_grows_with_volatility_for_an_otm_put(self):
        low = put_delta(spot=750, strike=700, dte_days=35, sigma=0.12)
        high = put_delta(spot=750, strike=700, dte_days=35, sigma=0.28)
        assert high > low

    def test_spread_credit_is_positive_and_below_width(self):
        c = spread_credit(
            spot=740, short_strike=700, long_strike=685, dte_days=35, sigma=0.18
        )
        assert 0 < c < 15

    def test_spread_credit_rises_with_volatility(self):
        lo = spread_credit(
            spot=740, short_strike=700, long_strike=685, dte_days=35, sigma=0.12
        )
        hi = spread_credit(
            spot=740, short_strike=700, long_strike=685, dte_days=35, sigma=0.30
        )
        assert hi > lo


class TestImpliedSigmaFromCredit:
    def test_round_trips_a_known_sigma(self):
        truth = 0.213
        credit = spread_credit(
            spot=740, short_strike=700, long_strike=685, dte_days=35, sigma=truth
        )
        recovered = implied_sigma_from_credit(
            spot=740, short_strike=700, long_strike=685,
            dte_days=35, observed_credit=credit,
        )
        assert recovered == pytest.approx(truth, abs=1e-4)

    def test_returns_none_when_the_credit_is_unreachable(self):
        """
        A credit above any plausible model value is information, not an
        error — the caller reports it rather than crashing the run.
        """
        assert implied_sigma_from_credit(
            spot=740, short_strike=700, long_strike=685,
            dte_days=35, observed_credit=14.99,
        ) is None


class TestDecomposeDeltaError:
    _BASE = dict(
        short_strike=700.0, long_strike=685.0, dte_days=35, iv_points=16.0,
    )

    def test_stale_high_spot_understates_delta(self):
        """
        The core claim: if the decision spot is above the live spot, the
        logged delta is lower than the delta actually sold.
        """
        out = decompose_delta_error(
            spot_at_decision=750.0, spot_live=720.0,
            observed_credit=spread_credit(
                spot=720.0, short_strike=700, long_strike=685,
                dte_days=35, sigma=0.16,
            ),
            **self._BASE,
        )
        assert out["err_spot"] > 0
        assert out["delta_at_live_spot"] > out["delta_bot"]

    def test_no_spot_drift_leaves_only_the_vol_error(self):
        out = decompose_delta_error(
            spot_at_decision=740.0, spot_live=740.0,
            observed_credit=spread_credit(
                spot=740.0, short_strike=700, long_strike=685,
                dte_days=35, sigma=0.26,   # market vol well above the 16 proxy
            ),
            **self._BASE,
        )
        assert out["err_spot"] == pytest.approx(0.0, abs=1e-9)
        assert out["err_vol"] > 0
        assert out["err_total"] == pytest.approx(out["err_vol"])

    def test_market_vol_matching_the_proxy_leaves_only_the_spot_error(self):
        out = decompose_delta_error(
            spot_at_decision=750.0, spot_live=730.0,
            observed_credit=spread_credit(
                spot=730.0, short_strike=700, long_strike=685,
                dte_days=35, sigma=0.16,   # identical to iv_points/100
            ),
            **self._BASE,
        )
        assert out["err_vol"] == pytest.approx(0.0, abs=1e-3)
        assert out["err_total"] == pytest.approx(out["err_spot"], abs=1e-3)

    def test_components_sum_to_the_total(self):
        out = decompose_delta_error(
            spot_at_decision=752.0, spot_live=731.0,
            observed_credit=3.1,
            **self._BASE,
        )
        assert out["err_spot"] + out["err_vol"] == pytest.approx(out["err_total"])

    def test_unreachable_credit_degrades_to_none_without_raising(self):
        out = decompose_delta_error(
            spot_at_decision=750.0, spot_live=740.0,
            observed_credit=14.99,
            **self._BASE,
        )
        assert out["implied_sigma"] is None
        assert out["err_vol"] is None
        assert out["err_total"] is None
        # The spot half is still measurable.
        assert out["err_spot"] is not None


class TestLoadEntriesFromDb:
    def _db(self, tmp_path, rows):
        p = tmp_path / "t.db"
        conn = sqlite3.connect(p)
        conn.execute(
            "CREATE TABLE trades (timestamp TEXT, symbol TEXT, side TEXT, "
            "avg_fill_price REAL, strategy TEXT, reason TEXT, position_type TEXT)"
        )
        conn.executemany(
            "INSERT INTO trades VALUES (?,?,?,?,'credit_spread','spread entry','spread')",
            rows,
        )
        conn.commit()
        conn.close()
        return p

    def test_pairs_legs_and_takes_the_credit_from_the_short_leg(self, tmp_path):
        """
        The long leg is written 0.0; the short leg carries the NET credit.
        Verified against the paired exit in data/trades.db.
        """
        db = self._db(tmp_path, [
            ("2026-07-13T13:40:53+00:00", "QQQ260821P00690000", "sell", 3.14),
            ("2026-07-13T13:40:53+00:00", "QQQ260821P00675000", "buy", 0.0),
        ])
        entries = load_entries_from_db(db)
        assert len(entries) == 1
        e = entries[0]
        assert e.underlying == "QQQ"
        assert e.short_strike == 690.0 and e.long_strike == 675.0
        assert e.net_credit == pytest.approx(3.14)
        assert e.width == pytest.approx(15.0)
        assert e.dte == (date(2026, 8, 21) - date(2026, 7, 13)).days

    def test_skips_a_half_recorded_spread_rather_than_guessing(self, tmp_path):
        db = self._db(tmp_path, [
            ("2026-07-13T13:40:53+00:00", "QQQ260821P00690000", "sell", 3.14),
        ])
        assert load_entries_from_db(db) == []

    def test_skips_entries_with_no_recorded_credit(self, tmp_path):
        db = self._db(tmp_path, [
            ("2026-07-13T13:40:53+00:00", "QQQ260821P00690000", "sell", 0.0),
            ("2026-07-13T13:40:53+00:00", "QQQ260821P00675000", "buy", 0.0),
        ])
        assert load_entries_from_db(db) == []

    def test_separate_timestamps_yield_separate_spreads(self, tmp_path):
        db = self._db(tmp_path, [
            ("2026-07-10T13:00:00+00:00", "QQQ260814P00687000", "sell", 2.65),
            ("2026-07-10T13:00:00+00:00", "QQQ260814P00672000", "buy", 0.0),
            ("2026-07-13T13:40:53+00:00", "QQQ260821P00690000", "sell", 3.14),
            ("2026-07-13T13:40:53+00:00", "QQQ260821P00675000", "buy", 0.0),
        ])
        entries = load_entries_from_db(db)
        assert len(entries) == 2
        assert [e.entry_date for e in entries] == [date(2026, 7, 10), date(2026, 7, 13)]


class TestBuildReportFetchesOnlyWhatIsMissing:
    """
    The log source is documented as authoritative and reconstruction-free.
    That contract is only real if a fully-populated log run touches no
    network at all — otherwise an unrelated Yahoo or Alpaca outage fails
    an audit that needed neither.
    """

    def _logged(self, **overrides):
        base = dict(
            underlying="QQQ", entry_date=date(2026, 8, 11),
            short_strike=690.0, long_strike=675.0, expiry=date(2026, 9, 18),
            net_credit=3.05, source="logs",
            spot_at_decision=725.60, spot_live=722.10,
            iv_points=15.8, est_short_delta=0.124,
        )
        base.update(overrides)
        return SpreadEntry(**base)

    @pytest.fixture
    def no_network(self, monkeypatch):
        """Make both network boundaries fatal, the way a DNS failure is."""
        def boom(*a, **k):
            raise AssertionError("network was touched")

        monkeypatch.setattr(audit, "_fetch_vix_series", boom)
        monkeypatch.setattr(audit, "_fetch_close_series", boom)

    def test_complete_log_entries_build_offline(self, no_network):
        df = audit.build_report([self._logged()])
        assert len(df) == 1
        row = df.iloc[0]
        # The logged values are used verbatim, not re-derived.
        assert row["spot_dec"] == pytest.approx(725.60)
        assert row["spot_live"] == pytest.approx(722.10)
        assert row["vix"] == pytest.approx(15.8)
        assert row["d_logged"] == pytest.approx(0.124)

    def test_many_complete_entries_still_build_offline(self, no_network):
        entries = [
            self._logged(underlying="QQQ"),
            self._logged(underlying="SPY", short_strike=714.0, long_strike=704.0),
        ]
        assert len(audit.build_report(entries)) == 2

    def test_empty_input_does_not_fetch(self, no_network):
        assert audit.build_report([]).empty

    def test_missing_iv_fetches_vix_but_not_bars(self, monkeypatch):
        """Only the genuinely absent input is fetched."""
        calls: list[str] = []

        def fake_vix(start, end):
            calls.append("vix")
            idx = pd.to_datetime(["2026-08-08", "2026-08-10"])
            return pd.Series([16.2, 15.9], index=idx)

        def bars_boom(*a, **k):
            raise AssertionError("bars were fetched but spots were logged")

        monkeypatch.setattr(audit, "_fetch_vix_series", fake_vix)
        monkeypatch.setattr(audit, "_fetch_close_series", bars_boom)

        df = audit.build_report([self._logged(iv_points=None)])
        assert calls == ["vix"]
        # Prior close strictly before 2026-08-11.
        assert df.iloc[0]["vix"] == pytest.approx(15.9)

    def test_missing_live_spot_fetches_bars_but_not_vix(self, monkeypatch):
        """
        `spot_live` is legitimately None when the pick-time quote was
        unavailable, so the fallback decision is per entry, not per source.
        """
        calls: list[str] = []

        def fake_bars(symbol, start, end):
            calls.append(symbol)
            idx = pd.to_datetime(["2026-08-10", "2026-08-11"])
            return pd.Series([724.0, 719.5], index=idx)

        def vix_boom(*a, **k):
            raise AssertionError("VIX was fetched but iv_points was logged")

        monkeypatch.setattr(audit, "_fetch_close_series", fake_bars)
        monkeypatch.setattr(audit, "_fetch_vix_series", vix_boom)

        df = audit.build_report([self._logged(spot_live=None)])
        assert calls == ["QQQ"]
        assert df.iloc[0]["spot_dec"] == pytest.approx(725.60)   # still logged
        assert df.iloc[0]["spot_live"] == pytest.approx(719.5)   # session close

    def test_bars_are_fetched_once_per_symbol(self, monkeypatch):
        calls: list[str] = []

        def fake_bars(symbol, start, end):
            calls.append(symbol)
            idx = pd.to_datetime(["2026-08-10", "2026-08-11"])
            return pd.Series([724.0, 719.5], index=idx)

        monkeypatch.setattr(audit, "_fetch_close_series", fake_bars)
        monkeypatch.setattr(audit, "_fetch_vix_series", lambda *a, **k: pd.Series(dtype=float))

        audit.build_report([
            self._logged(spot_at_decision=None),
            self._logged(spot_at_decision=None, entry_date=date(2026, 8, 11)),
        ])
        assert calls == ["QQQ"], "memoisation failed — one fetch per symbol"

    def test_zero_spot_is_reported_and_skipped_not_silently_backfilled(
        self, no_network
    ):
        """
        Truthiness would swap a logged 0.0 for a fetched close and hide
        corrupt data. `is None` keeps it, and the validator then skips the
        row loudly — Black-Scholes would otherwise assert deep in the
        library with a message naming neither the entry nor the field.
        """
        df = audit.build_report([self._logged(spot_live=0.0)])
        assert df.empty  # skipped, and no network was touched to "fix" it

    def test_one_bad_row_does_not_kill_the_batch(self, no_network):
        df = audit.build_report([
            self._logged(spot_live=0.0),                      # corrupt
            self._logged(underlying="SPY", short_strike=714.0, long_strike=704.0),
        ])
        assert len(df) == 1
        assert df.iloc[0]["und"] == "SPY"

    def test_nan_iv_is_skipped_too(self, no_network):
        assert audit.build_report([self._logged(iv_points=float("nan"))]).empty


class TestSpreadEntry:
    def test_width_and_dte_are_derived_not_stored(self):
        e = SpreadEntry(
            underlying="QQQ", entry_date=date(2026, 7, 13),
            short_strike=690.0, long_strike=675.0, expiry=date(2026, 8, 21),
            net_credit=3.14, source="db",
        )
        assert e.width == pytest.approx(15.0)
        assert e.dte == 39
