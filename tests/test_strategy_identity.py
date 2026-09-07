import pytest

from config import settings
from strategies.credit_spread import CreditSpread, CreditSpreadConfig
from strategies.donchian_breakout import DonchianBreakout
from strategies.filters.credit_spread import CreditSpreadEdgeFilter
from strategies.filters.donchian_breakout import DonchianEdgeFilter
from strategies.filters.rsi_reversion import RSIEdgeFilter
from strategies.filters.common import CompositeEdgeFilter
from strategies.filters.sector_momentum import SectorMomentumFilter
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.filters.spy_options_reversion import SPYOptionsEdgeFilter
from strategies.identity import resolve_strategy_identity, strategy_config_hash
from strategies.leveraged_trend import LeveragedTrend
from strategies.rsi_reversion import RSIReversion
from strategies.sma_crossover import SMACrossover
from strategies.spy_options_reversion import SPYOptionsReversionStrategy
from sector.gauge import SectorMomentumGauge
from sector.resolver import SectorResolver


class TestStrategyConfigIdentity:
    class _UnclassifiedFilter:
        def __call__(self, frame):
            return frame

    def test_same_behavior_has_same_hash(self):
        left = RSIReversion(period=3, quick_exit_rsi=55)
        right = RSIReversion(period=3, quick_exit_rsi=55)

        assert strategy_config_hash(left) == strategy_config_hash(right)

    def test_behavior_change_changes_hash(self):
        left = RSIReversion(period=3, quick_exit_rsi=55)
        right = RSIReversion(period=3, quick_exit_rsi=60)

        assert strategy_config_hash(left) != strategy_config_hash(right)

    def test_effective_feed_changes_hash(self):
        strategy = RSIReversion(period=3)

        assert strategy_config_hash(
            strategy, data_feed="iex", timeframe="1Day"
        ) != strategy_config_hash(strategy, data_feed="sip", timeframe="1Day")

    def test_filter_runtime_observations_do_not_change_hash(self):
        edge_filter = RSIEdgeFilter(
            stock_sma_window=20,
            vol_min_window=5,
            notional_min_avg=1_000,
        )
        strategy = RSIReversion(period=3, edge_filter=edge_filter)
        before = strategy_config_hash(strategy)
        edge_filter.set_symbol("AAPL")
        edge_filter._last_metrics = {"observed": 123.0}

        assert strategy_config_hash(strategy) == before

    def test_filter_parameter_change_changes_hash(self):
        low_floor = RSIReversion(
            period=3, edge_filter=RSIEdgeFilter(notional_min_avg=1_000)
        )
        high_floor = RSIReversion(
            period=3, edge_filter=RSIEdgeFilter(notional_min_avg=50_000_000)
        )

        assert strategy_config_hash(low_floor) != strategy_config_hash(high_floor)

    def test_unclassified_component_fails_instead_of_disappearing(self):
        strategy = RSIReversion(edge_filter=self._UnclassifiedFilter())

        with pytest.raises(TypeError, match="no strategy identity configuration"):
            strategy_config_hash(strategy)

    def test_runtime_resolution_degrades_unclassified_component_to_unknown(self):
        strategy = RSIReversion(edge_filter=self._UnclassifiedFilter())

        identity = resolve_strategy_identity(strategy)

        assert identity.strategy_version == "unknown"
        assert identity.strategy_config_hash == "unknown"
        assert identity.bot_git_commit

    def test_sector_runtime_caches_do_not_change_hash(self, tmp_path):
        gauge = SectorMomentumGauge({"technology": "XLK"})
        resolver = SectorResolver(
            cache_path=tmp_path / "sectors.json",
            valid_sectors={"technology"},
        )
        strategy = RSIReversion(
            period=3,
            edge_filter=CompositeEdgeFilter([
                RSIEdgeFilter(),
                SectorMomentumFilter(gauge, resolver, sector_entry_policy="warn"),
            ]),
        )
        before = strategy_config_hash(strategy)
        gauge._score_cache["technology"] = (object(), 123.0)
        gauge._etf_cache["XLK"] = (object(), 456.0)
        resolver._cache["AAPL"] = {"normalized": "technology"}

        assert strategy_config_hash(strategy) == before

    def test_unknown_strategy_is_stamped_honestly(self):
        strategy = RSIReversion()
        strategy.name = "external_strategy"

        identity = resolve_strategy_identity(strategy)

        assert identity.strategy_version == "unknown"
        assert identity.strategy_config_hash

    @pytest.mark.parametrize(
        "strategy",
        [
            SMACrossover(edge_filter=SMAEdgeFilter()),
            RSIReversion(edge_filter=RSIEdgeFilter()),
            DonchianBreakout(edge_filter=DonchianEdgeFilter()),
            LeveragedTrend(),
            SPYOptionsReversionStrategy(edge_filter=SPYOptionsEdgeFilter()),
            CreditSpread(
                CreditSpreadConfig.from_dict(
                    "SPY", settings.CREDIT_SPREAD_INSTRUMENTS["SPY"]
                ),
                edge_filter=CreditSpreadEdgeFilter(
                    iv_proxy_source="vix",
                    min_iv_proxy=14,
                ),
            ),
        ],
    )
    def test_every_active_strategy_has_explicit_contract(self, strategy):
        config_hash = strategy_config_hash(strategy)

        assert strategy.name in settings.STRATEGY_VERSIONS
        assert len(config_hash) == 12
