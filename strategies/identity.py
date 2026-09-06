"""Immutable strategy/run identity stamped on every new position lifecycle."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Iterable

from config import settings


_RUNTIME_FIELDS = frozenset({
    "_cache",
    "_cache_time",
    "_iv_resolver",
    "_last_scan_time",
    "_last_metrics",
    "_last_reason",
    "_last_reasons",
    "_open_spreads",
    "_position_base",
    "_position_hwm",
    "_quote_lookup",
    "_quote_types",
    "_regime",
    "_spy_cache",
    "_symbol",
})


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
    if callable(value):
        return None
    if hasattr(value, "__dict__"):
        fields = {
            key: _canonical(item)
            for key, item in vars(value).items()
            if key not in _RUNTIME_FIELDS and not callable(item)
        }
        return {
            "class": f"{value.__class__.__module__}.{value.__class__.__qualname__}",
            "fields": fields,
        }
    return repr(value)


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
    instance_fields = {
        key: _canonical(value)
        for key, value in vars(strategy).items()
        if key not in _RUNTIME_FIELDS and not callable(value)
    }
    return {
        "strategy": strategy_name,
        "class": f"{strategy.__class__.__module__}.{strategy.__class__.__qualname__}",
        "preferred_order_type": strategy.preferred_order_type.value,
        "instance": instance_fields,
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
