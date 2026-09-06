"""Immutable strategy/run identity stamped on every new position lifecycle."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from config import settings


# Explicit configuration contract for every component reachable from an active
# strategy. Runtime caches, observations, clients and callbacks are absent by
# construction. An unknown component fails clearly instead of silently merging
# or fragmenting evidence cohorts.
_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "strategies.sma_crossover.SMACrossover": ("fast", "slow", "_edge_filter"),
    "strategies.rsi_reversion.RSIReversion": (
        "period", "oversold", "overbought", "entry_mode", "exit_sma_window",
        "quick_exit_rsi", "_edge_filter",
    ),
    "strategies.donchian_breakout.DonchianBreakout": (
        "entry_window", "exit_window", "_edge_filter",
    ),
    "strategies.leveraged_trend.LeveragedTrend": (
        "sma_length", "entry_days", "exit_days", "signal_column",
        "target_notional_pct", "stated_leverage_multiplier",
        "stress_exposure_multiplier", "_edge_filter",
    ),
    "strategies.spy_options_reversion.SPYOptionsReversionStrategy": (
        "config", "rsi_length", "rsi_threshold", "trail_activation_pct",
        "trail_pct", "_edge_filter",
    ),
    "strategies.credit_spread.CreditSpread": ("config", "symbol", "_edge_filter"),
    "strategies.filters.common.CompositeEdgeFilter": ("_filters",),
    "strategies.filters.common.EarningsBlackout": ("_days_before", "_days_after"),
    "strategies.filters.common.SPYTrendFilter": (
        "_windows", "_lookback_days", "_cache_ttl", "_sma_tolerance_pct",
    ),
    "strategies.filters.sma_crossover.SMAEdgeFilter": (
        "_stock_sma_window", "_vol_short", "_vol_long", "_earnings",
    ),
    "strategies.filters.rsi_reversion.RSIEdgeFilter": (
        "_stock_sma_window", "_vol_min_window", "_notional_min_avg",
    ),
    "strategies.filters.donchian_breakout.DonchianEdgeFilter": (
        "_stock_sma_window", "_vol_min_window", "_notional_min_avg",
        "_earnings", "_feed_label_override",
    ),
    "strategies.filters.sector_momentum.SectorMomentumFilter": (
        "_gauge", "_resolver", "_sector_entry_policy", "_score_threshold",
    ),
    "strategies.filters.spy_options_reversion.SPYOptionsEdgeFilter": (
        "_spy_filter", "_min_vix_percentile", "_vix_source",
    ),
    "strategies.filters.credit_spread.CreditSpreadEdgeFilter": (
        "_iv_source", "_min_iv_proxy", "_sma_window",
        "_trend_sma_buffer_pct", "_earnings",
    ),
    "sector.gauge.SectorMomentumGauge": (
        "_sector_etfs", "_cache_ttl", "_lookback_days", "_smooth_window",
    ),
    "sector.resolver.SectorResolver": (
        "_cache_path", "_valid_sectors", "_per_symbol_timeout", "_max_retries",
    ),
}


@dataclass(frozen=True)
class StrategyRunIdentity:
    """Human release, behavior fingerprint, and exact bot revision."""

    strategy_version: str
    strategy_config_hash: str
    bot_git_commit: str

    @property
    def cohort_key(self) -> str:
        """The fields that define a comparable performance cohort."""
        return f"{self.strategy_version}+{self.strategy_config_hash}"


def _canonical(value: Any) -> Any:
    """Convert configuration objects to stable, JSON-safe values."""
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        class_name = f"{value.__class__.__module__}.{value.__class__.__qualname__}"
        field_names = _CONFIG_FIELDS.get(class_name)
        if field_names is None:
            raise TypeError(
                f"no strategy identity configuration contract for {class_name}"
            )
        missing = [field for field in field_names if not hasattr(value, field)]
        if missing:
            raise TypeError(
                f"strategy identity contract for {class_name} references "
                f"missing fields {missing}"
            )
        return {
            "class": class_name,
            "fields": {
                field: _canonical(getattr(value, field)) for field in field_names
            },
        }
    raise TypeError(
        f"unsupported strategy identity configuration value: {type(value).__name__}"
    )


def strategy_config_payload(
    strategy: Any,
    *,
    allowed_regimes: Iterable[Any] | None = None,
    data_feed: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    """Return the behavior-affecting runtime configuration used for hashing."""
    strategy_name = str(strategy.name)
    regimes = (
        list(allowed_regimes)
        if allowed_regimes is not None
        else settings.STRATEGY_ALLOWED_REGIMES.get(strategy_name)
    )
    return {
        "strategy": strategy_name,
        "class": f"{strategy.__class__.__module__}.{strategy.__class__.__qualname__}",
        "preferred_order_type": strategy.preferred_order_type.value,
        "instance": _canonical(strategy),
        "allowed_regimes": _canonical(regimes),
        "allocation": _canonical(settings.STRATEGY_ALLOCATIONS.get(strategy_name)),
        "watchlist": _canonical(settings.STRATEGY_WATCHLISTS.get(strategy_name)),
        "entry_price_cap": _canonical(settings.ENTRY_PRICE_CAPS.get(strategy_name)),
        "runtime_policy": _canonical({
            "data_feed": data_feed or settings.ALPACA_DATA_FEED,
            "timeframe": timeframe,
            "live_trading": settings.LIVE_TRADING,
            "live_size_multiplier": settings.LIVE_SIZE_MULTIPLIER,
            "max_position_risk_pct": settings.MAX_POSITION_PCT,
            "max_position_notional_pct": settings.MAX_POSITION_NOTIONAL_PCT,
            "strategy_risk_per_trade_pct": settings.STRATEGY_RISK_PER_TRADE_PCT.get(
                strategy_name
            ),
            "strategy_heat_cap_pct": settings.STRATEGY_MAX_OPEN_HEAT_PCT.get(
                strategy_name
            ),
            "strategy_heat_cap_enforced": settings.STRATEGY_HEAT_CAP_ENFORCED,
            "modeled_market_slippage_bps": settings.SLIPPAGE_MODEL_MARKET_BPS,
        }),
        "strategy_family_policy": _canonical(
            settings.CREDIT_SPREAD_INSTRUMENTS
            if strategy_name == "credit_spread"
            else settings.LEVERAGED_TREND_INSTRUMENTS
            if strategy_name == "leveraged_trend"
            else None
        ),
    }


def strategy_config_hash(
    strategy: Any,
    *,
    allowed_regimes: Iterable[Any] | None = None,
    data_feed: str | None = None,
    timeframe: str | None = None,
) -> str:
    """Return a compact hash of the actual behavior configuration."""
    encoded = json.dumps(
        strategy_config_payload(
            strategy,
            allowed_regimes=allowed_regimes,
            data_feed=data_feed,
            timeframe=timeframe,
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


@lru_cache(maxsize=1)
def bot_git_commit() -> str:
    """Return the running Git revision, explicitly flagging dirty code."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"uncommitted:{commit}" if dirty else commit


def resolve_strategy_identity(
    strategy: Any,
    *,
    allowed_regimes: Iterable[Any] | None = None,
    data_feed: str | None = None,
    timeframe: str | None = None,
) -> StrategyRunIdentity:
    """Resolve the immutable identity for a newly admitted entry."""
    # Unknown/test/plugin strategies must not break order admission. They are
    # stamped honestly as unknown and excluded from comparable cohorts.
    version = settings.STRATEGY_VERSIONS.get(str(strategy.name), "unknown")
    if version == "unknown":
        return StrategyRunIdentity(
            strategy_version="unknown",
            strategy_config_hash="unknown",
            bot_git_commit=bot_git_commit(),
        )
    return StrategyRunIdentity(
        strategy_version=version,
        strategy_config_hash=strategy_config_hash(
            strategy,
            allowed_regimes=allowed_regimes,
            data_feed=data_feed,
            timeframe=timeframe,
        ),
        bot_git_commit=bot_git_commit(),
    )
