"""
Unit tests for scripts/watchlist_review.py.

Covers:
  - _row_latest: safe DataFrame value extraction with fallback keys
  - fetch_fundamentals: raw data fields across pass / fail / missing-data scenarios
  - CheckProfile: dataclass construction and field access
  - StrategyFitness.verdict: all verdict branches with both profiles
  - assess_fitness: pure function logic for all check combinations
  - format_report: matrix structure, flagged section, legend
  - run_review: orchestration, file output, signature

yfinance is fully mocked — no network calls are made.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.watchlist_review import (
    ALL_PROFILES,
    MIN_RSI_CASH_RUNWAY_MONTHS,
    MIN_SMA_CASH_RUNWAY_MONTHS,
    RSI_PROFILE,
    SMA_PROFILE,
    CheckProfile,
    StrategyFitness,
    SymbolFundamentals,
    _row_latest,
    assess_fitness,
    fetch_fundamentals,
    format_report,
    run_review,
)


# ── DataFrame helpers ─────────────────────────────────────────────────────────


def _df(data: dict[str, list]) -> pd.DataFrame:
    """
    Build a yfinance-style DataFrame: index = metric names, columns = years
    (most-recent first).

    Rows with fewer values than the widest row are padded with NaN so that
    DataFrame construction always succeeds.  _row_latest uses .dropna(), so
    padding is invisible to the logic under test.

    Usage:
        _df({"Free Cash Flow": [1e9, 9e8]})
        # index: ["Free Cash Flow"], columns: [0, 1]
    """
    if not data:
        return pd.DataFrame()
    max_len = max(len(v) for v in data.values())
    padded = {
        k: v + [float("nan")] * (max_len - len(v))
        for k, v in data.items()
    }
    return pd.DataFrame(padded).T


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame()


def _mock_ticker(
    cashflow: pd.DataFrame | None = None,
    income_stmt: pd.DataFrame | None = None,
    balance_sheet: pd.DataFrame | None = None,
    fast_info: dict | None = None,
    info: dict | None = None,
) -> MagicMock:
    """Return a mock yf.Ticker with the given DataFrames."""
    t = MagicMock()
    t.cashflow = cashflow if cashflow is not None else _empty_df()
    t.income_stmt = income_stmt if income_stmt is not None else _empty_df()
    t.balance_sheet = balance_sheet if balance_sheet is not None else _empty_df()
    t.fast_info = fast_info if fast_info is not None else {}
    t.info = info if info is not None else {}
    return t


# ── TestRowLatest ─────────────────────────────────────────────────────────────


class TestRowLatest:
    def test_key_present_returns_value(self):
        df = _df({"Free Cash Flow": [1_000_000.0]})
        assert _row_latest(df, "Free Cash Flow") == 1_000_000.0

    def test_key_missing_returns_none(self):
        df = _df({"Operating Cash Flow": [500_000.0]})
        assert _row_latest(df, "Free Cash Flow") is None

    def test_fallback_key_used_when_first_missing(self):
        df = _df({"Operating Cash Flow": [500_000.0]})
        val = _row_latest(df, "Free Cash Flow", "Operating Cash Flow")
        assert val == 500_000.0

    def test_first_key_takes_priority(self):
        df = _df({
            "Free Cash Flow": [1_000_000.0],
            "Operating Cash Flow": [500_000.0],
        })
        val = _row_latest(df, "Free Cash Flow", "Operating Cash Flow")
        assert val == 1_000_000.0

    def test_most_recent_value_returned(self):
        # columns 0=recent, 1=prior
        df = _df({"Free Cash Flow": [9_000_000.0, 7_000_000.0]})
        assert _row_latest(df, "Free Cash Flow") == 9_000_000.0

    def test_nan_skipped(self):
        df = _df({"Free Cash Flow": [float("nan"), 5_000_000.0]})
        assert _row_latest(df, "Free Cash Flow") == 5_000_000.0

    def test_all_nan_returns_none(self):
        df = _df({"Free Cash Flow": [float("nan")]})
        assert _row_latest(df, "Free Cash Flow") is None

    def test_empty_dataframe_returns_none(self):
        assert _row_latest(_empty_df(), "Free Cash Flow") is None

    def test_no_keys_returns_none(self):
        df = _df({"Free Cash Flow": [1_000_000.0]})
        assert _row_latest(df) is None


# ── TestFetchFundamentals ─────────────────────────────────────────────────────


class TestFetchFundamentals:
    def test_market_cap_from_fast_info(self):
        t = _mock_ticker(fast_info={"market_cap": 2_500_000_000.0})
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.market_cap == pytest.approx(2_500_000_000.0)

    def test_market_cap_fallback_to_info(self):
        t = _mock_ticker(info={"marketCap": 7_000_000_000.0})
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.market_cap == pytest.approx(7_000_000_000.0)

    # ── FCF field ──────────────────────────────────────────────────────────────

    def test_fcf_positive_stored(self):
        t = _mock_ticker(
            cashflow=_df({"Free Cash Flow": [1_000_000_000.0]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.fcf_annual == pytest.approx(1_000_000_000.0)

    def test_fcf_negative_stored(self):
        t = _mock_ticker(
            cashflow=_df({"Free Cash Flow": [-500_000_000.0]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("RIVN")
        assert r.fcf_annual == pytest.approx(-500_000_000.0)

    def test_fcf_fallback_to_ocf_minus_capex(self):
        # No "Free Cash Flow" row — compute from OCF + CapEx (CapEx is negative)
        t = _mock_ticker(
            cashflow=_df({
                "Operating Cash Flow": [2_000_000_000.0],
                "Capital Expenditure": [-600_000_000.0],
            }),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.fcf_annual == pytest.approx(1_400_000_000.0)

    def test_fcf_none_when_data_missing(self):
        t = _mock_ticker()   # empty DataFrames
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.fcf_annual is None

    def test_fcf_none_when_only_ocf_available(self):
        # OCF present but no CapEx — can't compute FCF via fallback
        t = _mock_ticker(
            cashflow=_df({"Operating Cash Flow": [1_000_000_000.0]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.fcf_annual is None

    # ── Revenue growth field ───────────────────────────────────────────────────

    def test_revenue_growth_positive_stored(self):
        t = _mock_ticker(
            income_stmt=_df({
                "Total Revenue": [400_000_000_000.0, 350_000_000_000.0],
            }),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.revenue_growth_pct == pytest.approx(
            (400e9 - 350e9) / 350e9 * 100, rel=1e-4
        )

    def test_revenue_growth_negative_stored(self):
        t = _mock_ticker(
            income_stmt=_df({
                "Total Revenue": [300_000_000_000.0, 350_000_000_000.0],
            }),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("MSFT")
        assert r.revenue_growth_pct is not None
        assert r.revenue_growth_pct < 0

    def test_revenue_growth_none_when_only_one_year(self):
        t = _mock_ticker(
            income_stmt=_df({"Total Revenue": [100_000_000_000.0]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("X")
        assert r.revenue_growth_pct is None

    def test_revenue_growth_none_when_data_missing(self):
        t = _mock_ticker()
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("X")
        assert r.revenue_growth_pct is None

    # ── Solvency fields ─────────────────────────────────────────────────────────

    def test_is_profitable_true_when_net_income_positive(self):
        t = _mock_ticker(
            income_stmt=_df({"Net Income": [100_000_000_000.0]}),
            balance_sheet=_df({"Cash And Cash Equivalents": [50_000_000_000.0]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.is_profitable is True

    def test_cash_runway_computed_when_unprofitable(self):
        # Unprofitable: $2B cash, $1B/yr loss → 24 months
        annual_loss = 1_000_000_000.0
        cash = 2_000_000_000.0
        t = _mock_ticker(
            income_stmt=_df({"Net Income": [-annual_loss]}),
            balance_sheet=_df({"Cash And Cash Equivalents": [cash]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("X")
        assert r.is_profitable is False
        assert r.cash_runway_months == pytest.approx(24.0)

    def test_cash_runway_none_when_profitable(self):
        # Profitable company: cash_runway_months is not set
        t = _mock_ticker(
            income_stmt=_df({"Net Income": [100_000_000_000.0]}),
            balance_sheet=_df({"Cash And Cash Equivalents": [50_000_000_000.0]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.cash_runway_months is None

    def test_solvency_uses_cash_equivalents_fallback(self):
        cash = 10_000_000_000.0
        t = _mock_ticker(
            income_stmt=_df({"Net Income": [5_000_000_000.0]}),
            balance_sheet=_df({
                "Cash Cash Equivalents And Short Term Investments": [cash],
            }),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("MSFT")
        assert r.is_profitable is True

    def test_is_profitable_none_when_net_income_missing(self):
        t = _mock_ticker()
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("X")
        assert r.is_profitable is None
        assert r.cash_runway_months is None

    # ── Error handling ─────────────────────────────────────────────────────────

    def test_error_stored_on_exception(self):
        with patch(
            "scripts.watchlist_review.yf.Ticker",
            side_effect=RuntimeError("network error"),
        ):
            r = fetch_fundamentals("AAPL")
        assert r.error is not None
        assert "network error" in r.error
        assert r.fcf_annual is None
        assert r.revenue_growth_pct is None
        assert r.is_profitable is None

    # ── Combined scenarios ─────────────────────────────────────────────────────

    def test_all_fields_set_healthy_company(self):
        """A large profitable company with positive FCF and growing revenue."""
        t = _mock_ticker(
            cashflow=_df({"Free Cash Flow": [100_000_000_000.0]}),
            income_stmt=_df({
                "Total Revenue": [400_000_000_000.0, 380_000_000_000.0],
                "Net Income": [100_000_000_000.0],
            }),
            balance_sheet=_df({"Cash And Cash Equivalents": [50_000_000_000.0]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("AAPL")
        assert r.fcf_annual == pytest.approx(100e9)
        assert r.revenue_growth_pct is not None
        assert r.revenue_growth_pct > 0
        assert r.is_profitable is True
        assert r.cash_runway_months is None  # profitable: not computed

    def test_all_fields_set_distressed_company(self):
        """An unprofitable company with negative FCF, declining revenue, low cash."""
        annual_loss = 3_000_000_000.0
        cash = 1_000_000_000.0  # ~4 months runway
        t = _mock_ticker(
            cashflow=_df({"Free Cash Flow": [-2_000_000_000.0]}),
            income_stmt=_df({
                "Total Revenue": [2_000_000_000.0, 4_000_000_000.0],  # declining
                "Net Income": [-annual_loss],
            }),
            balance_sheet=_df({"Cash And Cash Equivalents": [cash]}),
        )
        with patch("scripts.watchlist_review.yf.Ticker", return_value=t):
            r = fetch_fundamentals("RIVN")
        assert r.fcf_annual == pytest.approx(-2e9)
        assert r.revenue_growth_pct is not None
        assert r.revenue_growth_pct < 0
        assert r.is_profitable is False
        assert r.cash_runway_months == pytest.approx((cash / annual_loss) * 12)


# ── TestCheckProfile ──────────────────────────────────────────────────────────


class TestCheckProfile:
    def test_sma_profile_fields(self):
        assert SMA_PROFILE.strategy_name == "sma_crossover"
        assert SMA_PROFILE.display_name == "SMA Crossover"
        assert SMA_PROFILE.fcf_required is True
        assert SMA_PROFILE.revenue_required is True
        assert SMA_PROFILE.min_cash_runway_months == MIN_SMA_CASH_RUNWAY_MONTHS

    def test_rsi_profile_fields(self):
        assert RSI_PROFILE.strategy_name == "rsi_reversion"
        assert RSI_PROFILE.display_name == "RSI Reversion"
        assert RSI_PROFILE.fcf_required is False
        assert RSI_PROFILE.revenue_required is False
        assert RSI_PROFILE.min_cash_runway_months == MIN_RSI_CASH_RUNWAY_MONTHS

    def test_custom_profile_construction(self):
        p = CheckProfile(
            strategy_name="test_strat",
            display_name="Test Strategy",
            fcf_required=True,
            revenue_required=False,
            min_cash_runway_months=6,
        )
        assert p.strategy_name == "test_strat"
        assert p.fcf_required is True
        assert p.revenue_required is False
        assert p.min_cash_runway_months == 6

    def test_all_profiles_list(self):
        assert SMA_PROFILE in ALL_PROFILES
        assert RSI_PROFILE in ALL_PROFILES
        assert len(ALL_PROFILES) == 2


# ── TestStrategyFitnessVerdict ────────────────────────────────────────────────


def _make_fitness(
    profile: CheckProfile = None,
    fcf_ok: bool | None = None,
    revenue_ok: bool | None = None,
    solvency_ok: bool | None = None,
    error: str | None = None,
) -> StrategyFitness:
    if profile is None:
        profile = SMA_PROFILE
    return StrategyFitness(
        symbol="TEST",
        strategy_name=profile.strategy_name,
        display_name=profile.display_name,
        fcf_required=profile.fcf_required,
        revenue_required=profile.revenue_required,
        min_cash_runway_months=profile.min_cash_runway_months,
        fcf_ok=fcf_ok,
        revenue_ok=revenue_ok,
        solvency_ok=solvency_ok,
        error=error,
    )


class TestStrategyFitnessVerdict:
    def test_good_fit_all_pass(self):
        f = _make_fitness(SMA_PROFILE, fcf_ok=True, revenue_ok=True, solvency_ok=True)
        assert f.verdict == "✅ GOOD FIT"

    def test_good_fit_with_na_data(self):
        # None checks are not penalised
        f = _make_fitness(SMA_PROFILE, fcf_ok=True, revenue_ok=None, solvency_ok=None)
        assert f.verdict == "✅ GOOD FIT"

    def test_poor_fit_required_fcf_fails(self):
        # SMA profile: FCF is required
        f = _make_fitness(SMA_PROFILE, fcf_ok=False, revenue_ok=True, solvency_ok=True)
        assert f.verdict == "❌ POOR FIT"

    def test_marginal_optional_fcf_fails(self):
        # RSI profile: FCF is optional
        f = _make_fitness(RSI_PROFILE, fcf_ok=False, revenue_ok=True, solvency_ok=True)
        assert f.verdict == "⚠️  MARGINAL"

    def test_poor_fit_required_revenue_fails(self):
        # SMA profile: revenue is required
        f = _make_fitness(SMA_PROFILE, fcf_ok=True, revenue_ok=False, solvency_ok=True)
        assert f.verdict == "❌ POOR FIT"

    def test_marginal_optional_revenue_fails(self):
        # RSI profile: revenue is optional
        f = _make_fitness(RSI_PROFILE, fcf_ok=True, revenue_ok=False, solvency_ok=True)
        assert f.verdict == "⚠️  MARGINAL"

    def test_poor_fit_solvency_fails_sma_threshold(self):
        # 14 months runway; SMA needs 18 → solvency_ok=False → POOR FIT
        f = _make_fitness(SMA_PROFILE, fcf_ok=True, revenue_ok=True, solvency_ok=False)
        assert f.verdict == "❌ POOR FIT"

    def test_poor_fit_solvency_fails_rsi_threshold(self):
        # 10 months runway; RSI needs 12 → solvency_ok=False → POOR FIT
        f = _make_fitness(RSI_PROFILE, fcf_ok=True, revenue_ok=True, solvency_ok=False)
        assert f.verdict == "❌ POOR FIT"

    def test_good_fit_adequate_rsi_runway(self):
        # 14 months runway; RSI needs 12 → solvency_ok=True → GOOD FIT
        f = _make_fitness(RSI_PROFILE, fcf_ok=None, revenue_ok=None, solvency_ok=True)
        assert f.verdict == "✅ GOOD FIT"

    def test_error_verdict(self):
        f = _make_fitness(SMA_PROFILE, error="timeout")
        assert f.verdict == "⚠️  ERROR"

    def test_chk_helper(self):
        assert StrategyFitness._chk(True) == "✓"
        assert StrategyFitness._chk(False) == "✗"
        assert StrategyFitness._chk(None) == "–"


# ── TestAssessFitness ─────────────────────────────────────────────────────────


class TestAssessFitness:
    def _healthy(self) -> SymbolFundamentals:
        return SymbolFundamentals(
            symbol="AAPL",
            fcf_annual=100e9,
            revenue_growth_pct=5.2,
            is_profitable=True,
            cash_runway_months=None,
        )

    def test_all_pass_sma(self):
        fitness = assess_fitness(self._healthy(), SMA_PROFILE)
        assert fitness.fcf_ok is True
        assert fitness.revenue_ok is True
        assert fitness.solvency_ok is True
        assert fitness.verdict == "✅ GOOD FIT"

    def test_all_pass_rsi(self):
        fitness = assess_fitness(self._healthy(), RSI_PROFILE)
        assert fitness.fcf_ok is True
        assert fitness.revenue_ok is True
        assert fitness.solvency_ok is True
        assert fitness.verdict == "✅ GOOD FIT"

    def test_fcf_fail_required_for_sma(self):
        fund = SymbolFundamentals(
            symbol="RIVN", fcf_annual=-5e9, revenue_growth_pct=10.0, is_profitable=True
        )
        fitness = assess_fitness(fund, SMA_PROFILE)
        assert fitness.fcf_ok is False
        assert fitness.verdict == "❌ POOR FIT"

    def test_fcf_fail_informational_for_rsi(self):
        fund = SymbolFundamentals(
            symbol="RIVN", fcf_annual=-5e9, revenue_growth_pct=10.0, is_profitable=True
        )
        fitness = assess_fitness(fund, RSI_PROFILE)
        assert fitness.fcf_ok is False
        assert fitness.verdict == "⚠️  MARGINAL"

    def test_revenue_fail_required_for_sma(self):
        fund = SymbolFundamentals(
            symbol="X", fcf_annual=1e9, revenue_growth_pct=-5.0, is_profitable=True
        )
        fitness = assess_fitness(fund, SMA_PROFILE)
        assert fitness.revenue_ok is False
        assert fitness.verdict == "❌ POOR FIT"

    def test_revenue_fail_informational_for_rsi(self):
        fund = SymbolFundamentals(
            symbol="X", fcf_annual=1e9, revenue_growth_pct=-5.0, is_profitable=True
        )
        fitness = assess_fitness(fund, RSI_PROFILE)
        assert fitness.revenue_ok is False
        assert fitness.verdict == "⚠️  MARGINAL"

    def test_solvency_threshold_differs_per_profile(self):
        # 14 months runway: GOOD FIT for RSI (needs 12), POOR FIT for SMA (needs 18)
        # FCF and revenue are positive so they don't affect the verdict
        fund = SymbolFundamentals(
            symbol="X",
            fcf_annual=1e9,
            revenue_growth_pct=5.0,
            is_profitable=False,
            cash_runway_months=14.0,
        )
        rsi_fitness = assess_fitness(fund, RSI_PROFILE)
        sma_fitness = assess_fitness(fund, SMA_PROFILE)
        assert rsi_fitness.solvency_ok is True
        assert rsi_fitness.verdict == "✅ GOOD FIT"
        assert sma_fitness.solvency_ok is False
        assert sma_fitness.verdict == "❌ POOR FIT"

    def test_error_propagates(self):
        fund = SymbolFundamentals(symbol="X", error="timeout")
        fitness = assess_fitness(fund, SMA_PROFILE)
        assert fitness.error == "timeout"
        assert fitness.verdict == "⚠️  ERROR"
        assert fitness.fcf_ok is None
        assert fitness.revenue_ok is None
        assert fitness.solvency_ok is None

    def test_profitable_company_passes_solvency_always(self):
        # Profitable: solvency_ok=True regardless of profile thresholds
        fund = SymbolFundamentals(
            symbol="X", fcf_annual=1e9, revenue_growth_pct=5.0, is_profitable=True
        )
        for profile in ALL_PROFILES:
            fitness = assess_fitness(fund, profile)
            assert fitness.solvency_ok is True

    def test_none_data_not_penalised(self):
        # No data available → all checks remain None → GOOD FIT
        fund = SymbolFundamentals(symbol="X")
        fitness = assess_fitness(fund, SMA_PROFILE)
        assert fitness.fcf_ok is None
        assert fitness.revenue_ok is None
        assert fitness.solvency_ok is None
        assert fitness.verdict == "✅ GOOD FIT"


# ── TestFormatReport ─────────────────────────────────────────────────────────


def _make_symbol_results(
    symbol: str = "AAPL",
    fcf_annual: float = 100e9,
    revenue_growth_pct: float = 5.0,
    is_profitable: bool = True,
    cash_runway_months: float | None = None,
    error: str | None = None,
) -> tuple[SymbolFundamentals, list[StrategyFitness]]:
    fund = SymbolFundamentals(
        symbol=symbol,
        fcf_annual=fcf_annual,
        revenue_growth_pct=revenue_growth_pct,
        is_profitable=is_profitable,
        cash_runway_months=cash_runway_months,
        error=error,
    )
    fitness_list = [assess_fitness(fund, p) for p in ALL_PROFILES]
    return (fund, fitness_list)


class TestFormatReport:
    def test_report_has_matrix_header(self):
        results = [_make_symbol_results("AAPL")]
        report = format_report(results)
        assert "## Strategy Fitness Matrix" in report

    def test_report_shows_all_strategy_columns(self):
        results = [_make_symbol_results("AAPL")]
        report = format_report(results)
        assert "SMA Crossover" in report
        assert "RSI Reversion" in report

    def test_flagged_section_for_poor_fit(self):
        # RIVN with negative FCF → SMA: POOR FIT
        results = [_make_symbol_results(
            "RIVN",
            fcf_annual=-5e9,
            revenue_growth_pct=-20.0,
            is_profitable=False,
            cash_runway_months=18.0,
        )]
        report = format_report(results)
        assert "## Flagged Symbols" in report
        assert "RIVN" in report
        assert "❌ POOR FIT" in report

    def test_flagged_section_for_marginal(self):
        # Negative FCF → RSI: MARGINAL (optional check failed)
        results = [_make_symbol_results(
            "COIN",
            fcf_annual=-1e9,
            revenue_growth_pct=10.0,
            is_profitable=True,
        )]
        report = format_report(results)
        assert "## Flagged Symbols" in report
        assert "⚠️  MARGINAL" in report

    def test_no_flagged_section_when_all_good(self):
        results = [_make_symbol_results("AAPL")]
        report = format_report(results)
        assert "## Flagged Symbols" not in report

    def test_all_symbols_in_matrix(self):
        results = [
            _make_symbol_results("AAPL"),
            _make_symbol_results("MSFT"),
            _make_symbol_results("RIVN"),
        ]
        report = format_report(results)
        assert "AAPL" in report
        assert "MSFT" in report
        assert "RIVN" in report

    def test_report_contains_results_count(self):
        results = [_make_symbol_results("AAPL"), _make_symbol_results("MSFT")]
        report = format_report(results)
        assert "Symbols reviewed: 2" in report

    def test_report_is_string(self):
        results = [_make_symbol_results("AAPL")]
        report = format_report(results)
        assert isinstance(report, str)
        assert len(report) > 100

    def test_report_has_legend(self):
        results = [_make_symbol_results("AAPL")]
        report = format_report(results)
        assert "## Legend" in report

    def test_report_has_recommended_actions(self):
        results = [_make_symbol_results("AAPL")]
        report = format_report(results)
        assert "## Recommended Actions" in report

    def test_report_shows_error_symbol(self):
        results = [_make_symbol_results("BAD", error="no data")]
        report = format_report(results)
        assert "BAD" in report
        assert "no data" in report


# ── TestRunReview ─────────────────────────────────────────────────────────────


class TestRunReview:
    def _make_healthy_ticker(self) -> MagicMock:
        return _mock_ticker(
            cashflow=_df({"Free Cash Flow": [10_000_000_000.0]}),
            income_stmt=_df({
                "Total Revenue": [200_000_000_000.0, 180_000_000_000.0],
                "Net Income": [20_000_000_000.0],
            }),
            balance_sheet=_df({"Cash And Cash Equivalents": [50_000_000_000.0]}),
        )

    def test_run_review_returns_results(self):
        with patch(
            "scripts.watchlist_review.yf.Ticker",
            return_value=self._make_healthy_ticker(),
        ):
            results = run_review(["AAPL"], ALL_PROFILES, output_path=None)
        assert len(results) == 1
        fund, fitness_list = results[0]
        assert fund.symbol == "AAPL"
        assert len(fitness_list) == 2  # one per profile

    def test_run_review_saves_to_file(self, tmp_path: Path):
        output = tmp_path / "review.md"
        with patch(
            "scripts.watchlist_review.yf.Ticker",
            return_value=self._make_healthy_ticker(),
        ):
            run_review(["AAPL"], ALL_PROFILES, output_path=output)
        assert output.exists()
        content = output.read_text()
        assert "AAPL" in content

    def test_run_review_creates_parent_dirs(self, tmp_path: Path):
        output = tmp_path / "nested" / "dir" / "review.md"
        with patch(
            "scripts.watchlist_review.yf.Ticker",
            return_value=self._make_healthy_ticker(),
        ):
            run_review(["AAPL"], ALL_PROFILES, output_path=output)
        assert output.exists()

    def test_run_review_multiple_symbols(self):
        with patch(
            "scripts.watchlist_review.yf.Ticker",
            return_value=self._make_healthy_ticker(),
        ):
            results = run_review(["AAPL", "MSFT", "GOOGL"], ALL_PROFILES, output_path=None)
        assert len(results) == 3
        symbols = [fund.symbol for fund, _ in results]
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        assert "GOOGL" in symbols

    def test_run_review_single_profile(self):
        with patch(
            "scripts.watchlist_review.yf.Ticker",
            return_value=self._make_healthy_ticker(),
        ):
            results = run_review(["AAPL"], [SMA_PROFILE], output_path=None)
        fund, fitness_list = results[0]
        assert len(fitness_list) == 1
        assert fitness_list[0].strategy_name == "sma_crossover"
