from strategies.filters.rsi_reversion import RSIEdgeFilter
from strategies.identity import resolve_strategy_identity, strategy_config_hash
from strategies.rsi_reversion import RSIReversion


class TestStrategyConfigIdentity:
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

    def test_unknown_strategy_is_stamped_honestly(self):
        strategy = RSIReversion()
        strategy.name = "external_strategy"

        identity = resolve_strategy_identity(strategy)

        assert identity.strategy_version == "unknown"
        assert identity.strategy_config_hash
