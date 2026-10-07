"""Strategy-specific replay for disposable PLAN 11.61 shadow outcomes.

This module is offline-only.  The trading engine writes candidate facts but
never imports or calls a resolver.  A replay result is advisory evidence, not a
synthetic trade and never an input to allocation or order submission.
"""

from __future__ import annotations

import ast
import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from engine.candidate_observation import _CAPACITY_DISPOSITIONS
from execution.entry_guard import EntryPriceCap, compute_cap_price
from strategies.donchian_breakout import DonchianBreakout
from strategies.rsi_reversion import RSIReversion
from strategies.sma_crossover import SMACrossover


_NY = ZoneInfo("America/New_York")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_REPLAY_BASIS = "rsi_limit_stop_daily_v1"
_SMA_REPLAY_BASIS = "sma_market_stop_daily_v1"
_DONCHIAN_REPLAY_BASIS = "donchian_stop_limit_stop_daily_v1"


@dataclass(frozen=True)
class RSIReplayContract:
    """Frozen behavior required to replay one RSI candidate."""

    contract_version: int
    strategy: str
    timeframe: str
    period: int
    oversold: float
    overbought: float
    entry_mode: str
    exit_sma_window: int | None
    quick_exit_rsi: float | None
    entry_order_type: str
    entry_time_in_force: str
    max_entry_age_seconds: int
    stop_anchor: str
    atr_stop_multiplier: float
    exit_order_type: str
    modeled_exit_slippage_bps: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RSIReplayContract":
        contract = cls(
            contract_version=int(value["contract_version"]),
            strategy=str(value["strategy"]),
            timeframe=str(value["timeframe"]),
            period=int(value["period"]),
            oversold=float(value["oversold"]),
            overbought=float(value["overbought"]),
            entry_mode=str(value["entry_mode"]),
            exit_sma_window=(
                int(value["exit_sma_window"])
                if value.get("exit_sma_window") is not None
                else None
            ),
            quick_exit_rsi=(
                float(value["quick_exit_rsi"])
                if value.get("quick_exit_rsi") is not None
                else None
            ),
            entry_order_type=str(value["entry_order_type"]),
            entry_time_in_force=str(value["entry_time_in_force"]),
            max_entry_age_seconds=int(value["max_entry_age_seconds"]),
            stop_anchor=str(value["stop_anchor"]),
            atr_stop_multiplier=float(value["atr_stop_multiplier"]),
            exit_order_type=str(value["exit_order_type"]),
            modeled_exit_slippage_bps=float(
                value["modeled_exit_slippage_bps"]
            ),
        )
        contract.validate()
        return contract

    def validate(self) -> None:
        if self.contract_version not in {2, 3}:
            raise ValueError(
                f"unsupported RSI replay contract {self.contract_version}"
            )
        if self.strategy != "rsi_reversion" or self.timeframe != "1Day":
            raise ValueError("RSI shadow replay supports rsi_reversion/1Day only")
        if self.entry_order_type != "limit" or self.entry_time_in_force not in {
            "day",
            "gtc",
        }:
            raise ValueError("RSI shadow replay requires a DAY or GTC LIMIT entry")
        if self.exit_order_type != "market":
            raise ValueError("RSI shadow replay requires MARKET signal exits")
        if self.max_entry_age_seconds <= 0:
            raise ValueError("RSI shadow replay requires a positive entry lifetime")
        if self.stop_anchor not in {"reference", "fill"}:
            raise ValueError("RSI shadow replay requires a known stop anchor")
        if (
            not math.isfinite(self.atr_stop_multiplier)
            or not math.isfinite(self.modeled_exit_slippage_bps)
            or self.atr_stop_multiplier <= 0
            or self.modeled_exit_slippage_bps < 0
        ):
            raise ValueError("RSI replay risk/slippage settings must be non-negative")
        # Reuse the production constructor for the remaining parameter checks.
        RSIReversion(
            period=self.period,
            oversold=self.oversold,
            overbought=self.overbought,
            entry_mode=self.entry_mode,
            exit_sma_window=self.exit_sma_window,
            quick_exit_rsi=self.quick_exit_rsi,
        )

    @property
    def warmup_calendar_days(self) -> int:
        """Conservative calendar span for the frozen indicator lookback."""
        required_bars = max(self.period + 1, self.exit_sma_window or 0)
        return max(30, required_bars * 2 + 14)


@dataclass(frozen=True)
class SMAReplayContract:
    """Frozen production behavior for one SMA crossover candidate."""

    contract_version: int
    strategy: str
    timeframe: str
    fast_window: int
    slow_window: int
    entry_order_type: str
    entry_time_in_force: str
    stop_anchor: str
    atr_stop_multiplier: float
    exit_order_type: str
    modeled_exit_slippage_bps: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SMAReplayContract":
        contract = cls(
            contract_version=int(value["contract_version"]),
            strategy=str(value["strategy"]),
            timeframe=str(value["timeframe"]),
            fast_window=int(value["fast_window"]),
            slow_window=int(value["slow_window"]),
            entry_order_type=str(value["entry_order_type"]),
            entry_time_in_force=str(value["entry_time_in_force"]),
            stop_anchor=str(value["stop_anchor"]),
            atr_stop_multiplier=float(value["atr_stop_multiplier"]),
            exit_order_type=str(value["exit_order_type"]),
            modeled_exit_slippage_bps=float(value["modeled_exit_slippage_bps"]),
        )
        contract.validate()
        return contract

    def validate(self) -> None:
        if self.contract_version != 1:
            raise ValueError(f"unsupported SMA replay contract {self.contract_version}")
        if self.strategy != "sma_crossover" or self.timeframe != "1Day":
            raise ValueError("SMA shadow replay supports sma_crossover/1Day only")
        if self.entry_order_type != "market" or self.exit_order_type != "market":
            raise ValueError("SMA shadow replay requires MARKET entry and exit")
        if self.entry_time_in_force not in {"day", "gtc"}:
            raise ValueError("SMA shadow replay requires a known entry TIF")
        if self.stop_anchor != "reference":
            raise ValueError("SMA shadow replay requires reference stop anchoring")
        if (
            not math.isfinite(self.atr_stop_multiplier)
            or not math.isfinite(self.modeled_exit_slippage_bps)
            or self.atr_stop_multiplier <= 0
            or self.modeled_exit_slippage_bps < 0
        ):
            raise ValueError("SMA replay risk/slippage settings are invalid")
        SMACrossover(fast=self.fast_window, slow=self.slow_window)

    @property
    def warmup_calendar_days(self) -> int:
        return max(120, self.slow_window * 2 + 20)


@dataclass(frozen=True)
class DonchianReplayContract:
    """Frozen production behavior for one Donchian candidate."""

    contract_version: int
    strategy: str
    timeframe: str
    entry_window: int
    exit_window: int
    entry_order_type: str
    entry_time_in_force: str
    max_chase_bps: float | None
    max_chase_atr_fraction: float | None
    stop_anchor: str
    atr_stop_multiplier: float
    exit_order_type: str
    modeled_exit_slippage_bps: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DonchianReplayContract":
        contract = cls(
            contract_version=int(value["contract_version"]),
            strategy=str(value["strategy"]),
            timeframe=str(value["timeframe"]),
            entry_window=int(value["entry_window"]),
            exit_window=int(value["exit_window"]),
            entry_order_type=str(value["entry_order_type"]),
            entry_time_in_force=str(value["entry_time_in_force"]),
            max_chase_bps=(
                float(value["max_chase_bps"])
                if value.get("max_chase_bps") is not None
                else None
            ),
            max_chase_atr_fraction=(
                float(value["max_chase_atr_fraction"])
                if value.get("max_chase_atr_fraction") is not None
                else None
            ),
            stop_anchor=str(value["stop_anchor"]),
            atr_stop_multiplier=float(value["atr_stop_multiplier"]),
            exit_order_type=str(value["exit_order_type"]),
            modeled_exit_slippage_bps=float(value["modeled_exit_slippage_bps"]),
        )
        contract.validate()
        return contract

    def validate(self) -> None:
        if self.contract_version != 1:
            raise ValueError(
                f"unsupported Donchian replay contract {self.contract_version}"
            )
        if self.strategy != "donchian_breakout" or self.timeframe != "1Day":
            raise ValueError(
                "Donchian shadow replay supports donchian_breakout/1Day only"
            )
        if self.entry_order_type != "stop_limit" or self.entry_time_in_force != "day":
            raise ValueError("Donchian shadow replay requires a DAY STOP_LIMIT entry")
        if self.exit_order_type != "market" or self.stop_anchor != "reference":
            raise ValueError(
                "Donchian shadow replay requires MARKET exits and reference stops"
            )
        if (
            not math.isfinite(self.atr_stop_multiplier)
            or not math.isfinite(self.modeled_exit_slippage_bps)
            or self.atr_stop_multiplier <= 0
            or self.modeled_exit_slippage_bps < 0
        ):
            raise ValueError("Donchian replay risk/slippage settings are invalid")
        EntryPriceCap(
            max_chase_bps=self.max_chase_bps,
            max_chase_atr_fraction=self.max_chase_atr_fraction,
        )
        DonchianBreakout(
            entry_window=self.entry_window,
            exit_window=self.exit_window,
        )

    @property
    def warmup_calendar_days(self) -> int:
        return max(90, self.entry_window * 2 + 20)


@dataclass(frozen=True)
class ShadowReplayResult:
    """One truthful terminal, open, or unresolved shadow state."""

    status: str
    entry_price: float | None
    exit_price: float | None
    exit_at: datetime | None
    return_pct: float | None
    r_multiple: float | None
    max_favorable_pct: float | None
    max_adverse_pct: float | None
    metadata: dict[str, Any]
    outcome_basis: str = _REPLAY_BASIS

    def store_values(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "outcome_basis": self.outcome_basis,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "exit_at": self.exit_at,
            # A capacity-refused candidate has no approved quantity.  Dollar
            # P&L would therefore be fabricated; percent and R are comparable.
            "pnl_dollars": None,
            "return_pct": self.return_pct,
            "r_multiple": self.r_multiple,
            "max_favorable_pct": self.max_favorable_pct,
            "max_adverse_pct": self.max_adverse_pct,
            "metadata_json": self.metadata,
        }


def _decoded(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("candidate JSON payload must decode to an object")
        return decoded
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _literal_assignments(source: str) -> dict[str, Any]:
    """Read selected literal settings without executing historical code."""
    wanted = {
        "RSI_REVERSION_PARAMS",
        "ATR_STOP_MULTIPLIER",
        "SLIPPAGE_MODEL_MARKET_BPS",
        "STALE_LIMIT_MAX_AGE_SECONDS",
    }
    values: dict[str, Any] = {}
    for node in ast.parse(source).body:
        target = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value_node = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value_node = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and target.id in wanted
            and value_node is not None
        ):
            try:
                values[target.id] = ast.literal_eval(value_node)
            except ValueError:
                # settings.py declares the stale limit as
                # int(os.getenv("STALE_LIMIT_MAX_AGE_SECONDS", 86400)).  Read
                # only that numeric default without executing historical code.
                if (
                    target.id == "STALE_LIMIT_MAX_AGE_SECONDS"
                    and isinstance(value_node, ast.Call)
                    and isinstance(value_node.func, ast.Name)
                    and value_node.func.id == "int"
                    and len(value_node.args) == 1
                    and isinstance(value_node.args[0], ast.Call)
                    and isinstance(value_node.args[0].func, ast.Attribute)
                    and value_node.args[0].func.attr == "getenv"
                    and len(value_node.args[0].args) == 2
                ):
                    values[target.id] = ast.literal_eval(
                        value_node.args[0].args[1]
                    )
                else:
                    raise
    missing = sorted(wanted - values.keys())
    if missing:
        raise ValueError(f"historical settings missing literal values: {missing}")
    return values


def historical_contract_from_settings_source(
    source: str, *, entry_time_in_force: str
) -> RSIReplayContract:
    """Build the legacy RSI replay contract from historical settings source."""
    values = _literal_assignments(source)
    params = dict(values["RSI_REVERSION_PARAMS"])
    return RSIReplayContract.from_mapping(
        {
            "contract_version": 2,
            "strategy": "rsi_reversion",
            "timeframe": "1Day",
            **params,
            "entry_order_type": "limit",
            "entry_time_in_force": entry_time_in_force,
            "max_entry_age_seconds": values["STALE_LIMIT_MAX_AGE_SECONDS"],
            # Legacy/current ordinary RSI GTC OTO stops were submitted from
            # the reference and were not rebuilt after a gap-down fill.
            "stop_anchor": "reference",
            "atr_stop_multiplier": values["ATR_STOP_MULTIPLIER"],
            "exit_order_type": "market",
            "modeled_exit_slippage_bps": values[
                "SLIPPAGE_MODEL_MARKET_BPS"
            ],
        }
    )


def _rsi_replay_contract_for_candidate(
    candidate: Mapping[str, Any], *, repo_root: str | Path
) -> tuple[RSIReplayContract, str]:
    """Load the stored contract, or recover old 11.61a rows from Git.

    The legacy path is intentionally strict: it accepts only an immutable Git
    commit and parses literal settings through ``ast.literal_eval``.  It never
    executes old source and never substitutes current settings.
    """
    common = _decoded(candidate.get("common_context_json"))
    stored = common.get("shadow_replay_contract")
    if isinstance(stored, Mapping):
        stored_values = dict(stored)
        if (
            int(stored_values.get("contract_version", 0)) == 1
            and "max_entry_age_seconds" not in stored_values
        ):
            commit = str(candidate.get("bot_git_commit") or "")
            if commit.startswith("uncommitted:") or not _COMMIT_RE.fullmatch(commit):
                raise ValueError(
                    "stored v1 contract cannot recover its historical entry lifetime"
                )
            result = subprocess.run(
                ["git", "show", f"{commit}:config/settings.py"],
                cwd=Path(repo_root),
                check=True,
                capture_output=True,
                text=True,
            )
            historical = _literal_assignments(result.stdout)
            stored_values["contract_version"] = 2
            stored_values["max_entry_age_seconds"] = historical[
                "STALE_LIMIT_MAX_AGE_SECONDS"
            ]
            return (
                RSIReplayContract.from_mapping(stored_values),
                "stored_candidate_contract+historical_entry_age_default",
            )
        return (
            RSIReplayContract.from_mapping(stored_values),
            "stored_candidate_contract",
        )

    commit = str(candidate.get("bot_git_commit") or "")
    if commit.startswith("uncommitted:") or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("candidate has no stored contract or immutable bot commit")
    result = subprocess.run(
        ["git", "show", f"{commit}:config/settings.py"],
        cwd=Path(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    peer_tif = str(candidate.get("peer_entry_time_in_force") or "").lower()
    if peer_tif not in {"day", "gtc"}:
        raise ValueError(
            "legacy candidate has no exact selected-peer entry time in force"
        )
    contract = historical_contract_from_settings_source(
        result.stdout, entry_time_in_force=peer_tif
    )

    features = _decoded(candidate.get("strategy_features_json"))
    for name in ("oversold", "quick_exit_rsi", "exit_sma_window", "entry_mode"):
        observed = features.get(name)
        expected = getattr(contract, name)
        if observed is None and expected is None:
            continue
        if isinstance(expected, float):
            matches = math.isclose(float(observed), expected, abs_tol=1e-9)
        else:
            matches = observed == expected
        if not matches:
            raise ValueError(
                f"historical replay contract disagrees with candidate {name}"
            )
    return contract, f"historical_commit:{commit[:10]}"


def _historical_source(candidate: Mapping[str, Any], repo_root: str | Path) -> tuple[str, str]:
    """Read settings from a candidate's immutable commit without executing it."""
    commit = str(candidate.get("bot_git_commit") or "")
    if commit.startswith("uncommitted:") or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("candidate has no stored contract or immutable bot commit")
    result = subprocess.run(
        ["git", "show", f"{commit}:config/settings.py"],
        cwd=Path(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout, commit


def _literal_setting(source: str, name: str) -> Any:
    """Return one literal assignment from historical source, never executing it."""
    for node in ast.parse(source).body:
        target = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value_node = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value_node = node.target, node.value
        if isinstance(target, ast.Name) and target.id == name and value_node is not None:
            try:
                return ast.literal_eval(value_node)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"historical setting {name} is not literal") from exc
    raise ValueError(f"historical settings missing {name}")


def _historical_donchian_cap(source: str) -> tuple[float | None, float | None]:
    """Safely parse the Donchian EntryPriceCap constructor from settings.py."""
    for node in ast.parse(source).body:
        target = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value_node = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value_node = node.target, node.value
        if not (
            isinstance(target, ast.Name)
            and target.id == "ENTRY_PRICE_CAPS"
            and isinstance(value_node, ast.Dict)
        ):
            continue
        for key_node, item in zip(value_node.keys, value_node.values, strict=True):
            try:
                key = ast.literal_eval(key_node)
            except (ValueError, TypeError):
                continue
            if key != "donchian_breakout" or not isinstance(item, ast.Call):
                continue
            keywords = {keyword.arg: keyword.value for keyword in item.keywords if keyword.arg}
            bps_node = keywords.get("max_chase_bps")
            atr_node = keywords.get("max_chase_atr_fraction")
            bps = ast.literal_eval(bps_node) if bps_node is not None else None
            atr = ast.literal_eval(atr_node) if atr_node is not None else None
            return (
                float(bps) if bps is not None else None,
                float(atr) if atr is not None else None,
            )
    raise ValueError("historical settings missing Donchian entry-price cap")


def replay_contract_for_candidate(
    candidate: Mapping[str, Any], *, repo_root: str | Path
) -> tuple[RSIReplayContract | SMAReplayContract | DonchianReplayContract, str]:
    """Return the exact strategy-specific contract for one refused candidate."""
    strategy = str(candidate.get("strategy") or "")
    if strategy == "rsi_reversion":
        return _rsi_replay_contract_for_candidate(candidate, repo_root=repo_root)

    common = _decoded(candidate.get("common_context_json"))
    stored = common.get("shadow_replay_contract")
    if isinstance(stored, Mapping):
        if strategy == "sma_crossover":
            return SMAReplayContract.from_mapping(stored), "stored_candidate_contract"
        if strategy == "donchian_breakout":
            return DonchianReplayContract.from_mapping(stored), "stored_candidate_contract"
        raise ValueError(f"unsupported candidate strategy {strategy!r}")

    source, commit = _historical_source(candidate, repo_root)
    features = _decoded(candidate.get("strategy_features_json"))
    common_values = {
        "contract_version": 1,
        "strategy": strategy,
        "timeframe": str(candidate.get("timeframe") or ""),
        "stop_anchor": "reference",
        "atr_stop_multiplier": float(_literal_setting(source, "ATR_STOP_MULTIPLIER")),
        "exit_order_type": "market",
        "modeled_exit_slippage_bps": float(
            _literal_setting(source, "SLIPPAGE_MODEL_MARKET_BPS")
        ),
    }
    if strategy == "sma_crossover":
        peer_tif = str(candidate.get("peer_entry_time_in_force") or "").lower()
        if peer_tif not in {"day", "gtc"}:
            raise ValueError("legacy SMA candidate has no selected-peer entry TIF")
        contract = SMAReplayContract.from_mapping(
            {
                **common_values,
                "fast_window": features["fast_window"],
                "slow_window": features["slow_window"],
                "entry_order_type": "market",
                "entry_time_in_force": peer_tif,
            }
        )
    elif strategy == "donchian_breakout":
        chase_bps, chase_atr = _historical_donchian_cap(source)
        contract = DonchianReplayContract.from_mapping(
            {
                **common_values,
                "entry_window": features["entry_window"],
                "exit_window": features["exit_window"],
                "entry_order_type": "stop_limit",
                "entry_time_in_force": "day",
                "max_chase_bps": chase_bps,
                "max_chase_atr_fraction": chase_atr,
            }
        )
    else:
        raise ValueError(f"unsupported candidate strategy {strategy!r}")
    return contract, f"historical_commit:{commit[:10]}"


def _bars(value: pd.DataFrame, *, name: str) -> pd.DataFrame:
    required = {"open", "high", "low", "close"}
    missing = sorted(required - set(value.columns))
    if missing:
        raise ValueError(f"{name} bars missing columns {missing}")
    out = value.copy().sort_index()
    index = pd.DatetimeIndex(out.index)
    if index.tz is None:
        raise ValueError(f"{name} bars require a timezone-aware index")
    out.index = index.tz_convert("UTC")
    return out


def _metric_result(
    *,
    status: str,
    entry: float,
    stop: float,
    exit_price: float | None,
    exit_at: datetime | None,
    high: float,
    low: float,
    metadata: dict[str, Any],
    outcome_basis: str = _REPLAY_BASIS,
) -> ShadowReplayResult:
    realized = exit_price is not None
    return_pct = exit_price / entry - 1.0 if realized else None
    risk = entry - stop
    r_multiple = (exit_price - entry) / risk if realized and risk > 0 else None
    return ShadowReplayResult(
        status=status,
        entry_price=entry,
        exit_price=exit_price,
        exit_at=exit_at,
        return_pct=return_pct,
        r_multiple=r_multiple,
        max_favorable_pct=high / entry - 1.0,
        max_adverse_pct=low / entry - 1.0,
        metadata=metadata,
        outcome_basis=outcome_basis,
    )


def resolve_rsi_shadow(
    candidate: Mapping[str, Any],
    contract: RSIReplayContract,
    *,
    daily_bars: pd.DataFrame,
    entry_minutes: pd.DataFrame,
    as_of: datetime | None = None,
    entry_window_complete: bool,
    contract_source: str,
) -> ShadowReplayResult:
    """Replay one refused RSI candidate without inventing dollar P&L.

    The observation session uses only full one-minute bars after the candidate
    existed.  A GTC order then remains eligible only until the earlier of the
    frozen engine stale-entry age and the broker's 90-day ceiling.  Thereafter
    production RSI exits and the fixed ATR stop are replayed on completed daily
    bars.  Ordering that cannot be proven from the available bar resolution
    remains ``needs_review``.
    """
    if candidate.get("strategy") != "rsi_reversion":
        raise ValueError("resolve_rsi_shadow received a non-RSI candidate")
    if candidate.get("disposition") not in _CAPACITY_DISPOSITIONS:
        raise ValueError("RSI shadow resolver accepts capacity refusals only")
    observed = datetime.fromisoformat(str(candidate["observed_at"]))
    if observed.tzinfo is None:
        raise ValueError("candidate observed_at must be timezone-aware")
    observed = observed.astimezone(timezone.utc)
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    as_of = as_of.astimezone(timezone.utc)
    limit_price = float(candidate["reference_price"])
    atr = float(candidate["atr"])
    if not all(math.isfinite(value) for value in (limit_price, atr)):
        raise ValueError("candidate reference/ATR must be finite")
    if limit_price <= 0 or atr <= 0:
        raise ValueError("candidate reference/ATR must be positive")

    def stop_for_entry(entry_price: float) -> float:
        anchor = limit_price if contract.stop_anchor == "reference" else entry_price
        value = anchor - contract.atr_stop_multiplier * atr
        if (
            not math.isfinite(value)
            or value <= 0
            or (contract.stop_anchor == "fill" and value >= entry_price)
        ):
            raise ValueError("candidate replay contract does not produce a valid stop")
        return value

    daily = _bars(daily_bars, name="daily")
    local_dates = pd.Index(daily.index.tz_convert(_NY).date)
    daily = daily.assign(_session_date=local_dates)
    today_ny = as_of.astimezone(_NY)
    if today_ny.time() < time(16, 0):
        daily = daily[daily["_session_date"] < today_ny.date()]
    else:
        daily = daily[daily["_session_date"] <= today_ny.date()]

    minutes = _bars(entry_minutes, name="entry minute")
    session_date = observed.astimezone(_NY).date()
    session_close = datetime.combine(session_date, time(16, 0), _NY).astimezone(
        timezone.utc
    )
    # The observation can occur part-way through a minute.  Do not use that
    # bar's earlier trades; begin with the first complete minute afterward.
    first_full_minute = pd.Timestamp(observed).ceil("min")
    usable = minutes[
        (minutes.index >= first_full_minute) & (minutes.index < session_close)
    ]
    touched = usable[usable["low"].astype(float) <= limit_price]
    base_meta: dict[str, Any] = {
        "replay_version": 1,
        "contract_source": contract_source,
        "contract": asdict(contract),
        "as_of": as_of.isoformat(),
        "entry_limit_price": limit_price,
        "stop_anchor": contract.stop_anchor,
        "entry_timing_basis": (
            "first complete 1-minute bar after observation; "
            "later completed daily bars for GTC"
        ),
        "exit_timing_basis": "next-session open after completed daily signal",
    }
    if not touched.empty:
        fill_at = touched.index[0]
        fill_bar = touched.iloc[0]
        entry = min(float(fill_bar["open"]), limit_price)
        stop = stop_for_entry(entry)
        post_fill = usable[usable.index >= fill_at]
        high = max(entry, float(fill_bar["high"]))
        low = min(entry, float(fill_bar["low"]))
        base_meta.update(
            {
                "fill_status": "filled",
                "entry_at": fill_at.isoformat(),
                "fill_resolution": "1_minute",
                "excursion_precision": "one_minute_bounds",
                "stop_price": stop,
            }
        )
        if float(fill_bar["low"]) <= stop:
            return _metric_result(
                status="needs_review",
                entry=entry,
                stop=stop,
                exit_price=None,
                exit_at=None,
                high=high,
                low=low,
                metadata={
                    **base_meta,
                    "unresolved_reason": "entry and stop touched in the same minute",
                },
            )
        later_stop = post_fill.iloc[1:]
        later_stop = later_stop[later_stop["low"].astype(float) <= stop]
        if not later_stop.empty:
            stop_at = later_stop.index[0]
            raw_exit = min(float(later_stop.iloc[0]["open"]), stop)
            before_stop = post_fill.loc[post_fill.index < stop_at]
            if not before_stop.empty:
                high = max(high, float(before_stop["high"].max()))
                low = min(low, float(before_stop["low"].min()))
            high = max(high, raw_exit)
            low = min(low, raw_exit)
            exit_price = raw_exit * (
                1.0 - contract.modeled_exit_slippage_bps / 10_000.0
            )
            return _metric_result(
                status="resolved",
                entry=entry,
                stop=stop,
                exit_price=exit_price,
                exit_at=stop_at.to_pydatetime(),
                high=high,
                low=low,
                metadata={
                    **base_meta,
                    "exit_reason": "protective_stop_entry_session",
                    "stop_bar_excursion_excluded": True,
                },
            )
        high = max(high, float(post_fill["high"].max()))
        low = min(low, float(post_fill["low"].min()))
    else:
        if not entry_window_complete:
            status = "pending_data"
            return ShadowReplayResult(
                status=status,
                entry_price=None,
                exit_price=None,
                exit_at=None,
                return_pct=None,
                r_multiple=None,
                max_favorable_pct=None,
                max_adverse_pct=None,
                metadata={**base_meta, "fill_status": status},
            )
        if contract.entry_time_in_force == "day":
            return ShadowReplayResult(
                status="not_filled",
                entry_price=None,
                exit_price=None,
                exit_at=None,
                return_pct=None,
                r_multiple=None,
                max_favorable_pct=None,
                max_adverse_pct=None,
                metadata={**base_meta, "fill_status": "not_filled"},
            )
        broker_expires_at = observed + timedelta(days=90)
        policy_expires_at = observed + timedelta(
            seconds=contract.max_entry_age_seconds
        )
        expires_at = min(broker_expires_at, policy_expires_at)
        later = daily[daily["_session_date"] > session_date]
        fully_eligible_indexes: list[pd.Timestamp] = []
        boundary_rows: list[tuple[pd.Timestamp, pd.Series]] = []
        for timestamp, row in later.iterrows():
            row_date = row["_session_date"]
            session_end = datetime.combine(row_date, time(16, 0), _NY).astimezone(
                timezone.utc
            )
            if session_end <= expires_at:
                fully_eligible_indexes.append(timestamp)
            else:
                # Cleanup runs only inside a market-hours engine cycle.  The
                # first session ending after the wall-clock threshold still
                # sees the order at its open, including when the threshold
                # fell on a weekend or holiday.  Daily bars cannot prove a
                # later intraday touch happened before the cleanup cycle.
                boundary_rows.append((timestamp, row))
                break
        eligible = later.loc[fully_eligible_indexes]
        later_touches = eligible[eligible["low"].astype(float) <= limit_price]
        if later_touches.empty and boundary_rows:
            boundary_at, boundary = boundary_rows[0]
            if float(boundary["open"]) <= limit_price:
                later_touches = later.loc[[boundary_at]]
            elif float(boundary["low"]) <= limit_price:
                return ShadowReplayResult(
                    status="needs_review",
                    entry_price=None,
                    exit_price=None,
                    exit_at=None,
                    return_pct=None,
                    r_multiple=None,
                    max_favorable_pct=None,
                    max_adverse_pct=None,
                    metadata={
                        **base_meta,
                        "fill_status": "needs_review",
                        "unresolved_reason": (
                            "entry limit touched during the local-cancellation "
                            "boundary session; daily bars cannot prove ordering"
                        ),
                        "entry_policy_expires_at": policy_expires_at.isoformat(),
                        "broker_gtc_expires_at": broker_expires_at.isoformat(),
                    },
                )
        if later_touches.empty:
            # A wall-clock threshold is not itself a terminal observation.
            # Until the first applicable market session is complete, the
            # production order may still fill at that session's open.
            status = (
                "not_filled"
                if boundary_rows and as_of >= expires_at
                else "awaiting_fill"
            )
            return ShadowReplayResult(
                status=status,
                entry_price=None,
                exit_price=None,
                exit_at=None,
                return_pct=None,
                r_multiple=None,
                max_favorable_pct=None,
                max_adverse_pct=None,
                metadata={
                    **base_meta,
                    "fill_status": status,
                    "entry_policy_expires_at": policy_expires_at.isoformat(),
                    "broker_gtc_expires_at": broker_expires_at.isoformat(),
                    "effective_entry_expires_at": expires_at.isoformat(),
                },
            )
        fill_at = later_touches.index[0]
        fill_bar = later_touches.iloc[0]
        session_date = fill_bar["_session_date"]
        entry = min(float(fill_bar["open"]), limit_price)
        stop = stop_for_entry(entry)
        high = entry
        low = entry
        base_meta.update(
            {
                "fill_status": "filled",
                "entry_at": fill_at.isoformat(),
                "fill_resolution": "daily",
                "excursion_precision": "fill_session_excluded",
                "entry_policy_expires_at": policy_expires_at.isoformat(),
                "broker_gtc_expires_at": broker_expires_at.isoformat(),
                "effective_entry_expires_at": expires_at.isoformat(),
                "stop_price": stop,
            }
        )
        if float(fill_bar["low"]) <= stop:
            return _metric_result(
                status="needs_review",
                entry=entry,
                stop=stop,
                exit_price=None,
                exit_at=None,
                high=high,
                low=low,
                metadata={
                    **base_meta,
                    "unresolved_reason": "entry and stop touched in the same daily bar",
                },
            )

    if daily.empty or session_date not in set(daily["_session_date"]):
        return _metric_result(
            status="pending_data",
            entry=entry,
            stop=stop,
            exit_price=None,
            exit_at=None,
            high=high,
            low=low,
            metadata={**base_meta, "unresolved_reason": "entry daily bar unavailable"},
        )

    signal_frame = daily.drop(columns="_session_date")
    strategy = RSIReversion(
        period=contract.period,
        oversold=contract.oversold,
        overbought=contract.overbought,
        entry_mode=contract.entry_mode,
        exit_sma_window=contract.exit_sma_window,
        quick_exit_rsi=contract.quick_exit_rsi,
    )
    exits = strategy._raw_signals(signal_frame).exits
    sessions = list(daily.iterrows())
    entry_idx = next(
        idx
        for idx, (_timestamp, row) in enumerate(sessions)
        if row["_session_date"] == session_date
    )
    pending_exit = bool(exits.iloc[entry_idx])
    for idx in range(entry_idx + 1, len(sessions)):
        timestamp, row = sessions[idx]
        open_price = float(row["open"])
        if pending_exit:
            reason = "protective_stop_gap" if open_price <= stop else "rsi_signal"
            raw_exit = min(open_price, stop) if open_price <= stop else open_price
            exit_price = raw_exit * (
                1.0 - contract.modeled_exit_slippage_bps / 10_000.0
            )
            high = max(high, raw_exit)
            low = min(low, raw_exit)
            return _metric_result(
                status="resolved",
                entry=entry,
                stop=stop,
                exit_price=exit_price,
                exit_at=pd.Timestamp(timestamp).to_pydatetime(),
                high=high,
                low=low,
                metadata={**base_meta, "exit_reason": reason},
            )
        if open_price <= stop or float(row["low"]) <= stop:
            raw_exit = min(open_price, stop)
            high = max(high, raw_exit)
            low = min(low, raw_exit)
            exit_price = raw_exit * (
                1.0 - contract.modeled_exit_slippage_bps / 10_000.0
            )
            return _metric_result(
                status="resolved",
                entry=entry,
                stop=stop,
                exit_price=exit_price,
                exit_at=pd.Timestamp(timestamp).to_pydatetime(),
                high=high,
                low=low,
                metadata={
                    **base_meta,
                    "exit_reason": "protective_stop",
                    "stop_bar_excursion_excluded": True,
                },
            )
        high = max(high, float(row["high"]))
        low = min(low, float(row["low"]))
        pending_exit = bool(exits.iloc[idx])

    return _metric_result(
        status="open",
        entry=entry,
        stop=stop,
        exit_price=None,
        exit_at=None,
        high=high,
        low=low,
        metadata={
            **base_meta,
            "pending_exit_signal": pending_exit,
            "last_mark": float(sessions[-1][1]["close"]),
        },
    )


def _empty_result(
    *, status: str, basis: str, metadata: dict[str, Any]
) -> ShadowReplayResult:
    return ShadowReplayResult(
        status=status,
        entry_price=None,
        exit_price=None,
        exit_at=None,
        return_pct=None,
        r_multiple=None,
        max_favorable_pct=None,
        max_adverse_pct=None,
        metadata=metadata,
        outcome_basis=basis,
    )


def _as_of_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return resolved.astimezone(timezone.utc)


def _resolve_equity_after_entry(
    *,
    strategy: SMACrossover | DonchianBreakout,
    daily_bars: pd.DataFrame,
    entry_minutes: pd.DataFrame,
    entry_at: pd.Timestamp,
    entry: float,
    stop: float,
    as_of: datetime,
    slippage_bps: float,
    basis: str,
    metadata: dict[str, Any],
) -> ShadowReplayResult:
    """Apply the production protective stop and raw daily signal exit."""
    minutes = _bars(entry_minutes, name="entry minute")
    post_fill = minutes[minutes.index >= entry_at]
    fill_bar = post_fill.iloc[0]
    high = max(entry, float(fill_bar["high"]))
    low = min(entry, float(fill_bar["low"]))
    if float(fill_bar["low"]) <= stop:
        return _metric_result(
            status="needs_review",
            entry=entry,
            stop=stop,
            exit_price=None,
            exit_at=None,
            high=high,
            low=low,
            metadata={
                **metadata,
                "unresolved_reason": "entry and stop touched in the same minute",
            },
            outcome_basis=basis,
        )
    for timestamp, row in post_fill.iloc[1:].iterrows():
        open_price = float(row["open"])
        if open_price <= stop or float(row["low"]) <= stop:
            raw_exit = min(open_price, stop)
            high = max(high, raw_exit)
            low = min(low, raw_exit)
            return _metric_result(
                status="resolved",
                entry=entry,
                stop=stop,
                exit_price=raw_exit * (1.0 - slippage_bps / 10_000.0),
                exit_at=pd.Timestamp(timestamp).to_pydatetime(),
                high=high,
                low=low,
                metadata={
                    **metadata,
                    "exit_reason": "protective_stop_entry_session",
                    "stop_bar_excursion_excluded": True,
                },
                outcome_basis=basis,
            )
        high = max(high, float(row["high"]))
        low = min(low, float(row["low"]))

    daily = _bars(daily_bars, name="daily")
    local_dates = pd.Index(daily.index.tz_convert(_NY).date)
    daily = daily.assign(_session_date=local_dates)
    today_ny = as_of.astimezone(_NY)
    if today_ny.time() < time(16, 0):
        daily = daily[daily["_session_date"] < today_ny.date()]
    else:
        daily = daily[daily["_session_date"] <= today_ny.date()]
    entry_date = entry_at.tz_convert(_NY).date()
    entry_matches = list(daily.index[daily["_session_date"] == entry_date])
    if not entry_matches:
        return _metric_result(
            status="open",
            entry=entry,
            stop=stop,
            exit_price=None,
            exit_at=None,
            high=high,
            low=low,
            metadata={**metadata, "pending_exit_signal": False},
            outcome_basis=basis,
        )

    signal_frame = daily.drop(columns="_session_date")
    exits = strategy._raw_signals(signal_frame).exits
    sessions = list(daily.iterrows())
    entry_idx = next(
        idx for idx, (_timestamp, row) in enumerate(sessions)
        if row["_session_date"] == entry_date
    )
    pending_exit = bool(exits.iloc[entry_idx])
    for idx in range(entry_idx + 1, len(sessions)):
        timestamp, row = sessions[idx]
        open_price = float(row["open"])
        if open_price <= stop:
            raw_exit = open_price
            reason = "protective_stop_gap"
        elif pending_exit:
            raw_exit = open_price
            reason = "strategy_signal"
        elif float(row["low"]) <= stop:
            raw_exit = stop
            reason = "protective_stop"
        else:
            high = max(high, float(row["high"]))
            low = min(low, float(row["low"]))
            pending_exit = bool(exits.iloc[idx])
            continue
        high = max(high, raw_exit)
        low = min(low, raw_exit)
        return _metric_result(
            status="resolved",
            entry=entry,
            stop=stop,
            exit_price=raw_exit * (1.0 - slippage_bps / 10_000.0),
            exit_at=pd.Timestamp(timestamp).to_pydatetime(),
            high=high,
            low=low,
            metadata={
                **metadata,
                "exit_reason": reason,
                "stop_bar_excursion_excluded": reason.startswith("protective_stop"),
            },
            outcome_basis=basis,
        )

    return _metric_result(
        status="open",
        entry=entry,
        stop=stop,
        exit_price=None,
        exit_at=None,
        high=high,
        low=low,
        metadata={
            **metadata,
            "pending_exit_signal": pending_exit,
            "last_mark": float(sessions[-1][1]["close"]),
        },
        outcome_basis=basis,
    )


def resolve_sma_shadow(
    candidate: Mapping[str, Any],
    contract: SMAReplayContract,
    *,
    daily_bars: pd.DataFrame,
    entry_minutes: pd.DataFrame,
    as_of: datetime | None = None,
    entry_window_complete: bool,
    contract_source: str,
) -> ShadowReplayResult:
    """Replay one refused SMA market entry and its production exits."""
    if candidate.get("strategy") != "sma_crossover":
        raise ValueError("resolve_sma_shadow received a non-SMA candidate")
    if candidate.get("disposition") not in _CAPACITY_DISPOSITIONS:
        raise ValueError("SMA shadow resolver accepts capacity refusals only")
    observed = datetime.fromisoformat(str(candidate["observed_at"]))
    if observed.tzinfo is None:
        raise ValueError("candidate observed_at must be timezone-aware")
    observed = observed.astimezone(timezone.utc)
    as_of = _as_of_utc(as_of)
    reference = float(candidate["reference_price"])
    atr = float(candidate["atr"])
    if not all(math.isfinite(value) and value > 0 for value in (reference, atr)):
        raise ValueError("candidate reference/ATR must be finite and positive")
    stop = round(reference - contract.atr_stop_multiplier * atr, 2)
    base = {
        "replay_version": 1,
        "contract_source": contract_source,
        "contract": asdict(contract),
        "as_of": as_of.isoformat(),
        "entry_timing_basis": "first complete 1-minute bar after observation",
        "exit_timing_basis": "next-session open after completed daily signal",
        "stop_price": stop,
    }
    if stop <= 0:
        return _empty_result(status="not_eligible", basis=_SMA_REPLAY_BASIS, metadata={
            **base, "unresolved_reason": "production stop would be non-positive"
        })
    minutes = _bars(entry_minutes, name="entry minute")
    session_close = datetime.combine(
        observed.astimezone(_NY).date(), time(16, 0), _NY
    ).astimezone(timezone.utc)
    usable = minutes[
        (minutes.index >= pd.Timestamp(observed).ceil("min"))
        & (minutes.index < session_close)
    ]
    if usable.empty:
        return _empty_result(
            status="pending_data",
            basis=_SMA_REPLAY_BASIS,
            metadata={
                **base,
                "unresolved_reason": (
                    "market-entry minute unavailable"
                    if entry_window_complete
                    else "entry session still in progress"
                ),
            },
        )
    entry_at = usable.index[0]
    entry = float(usable.iloc[0]["open"])
    return _resolve_equity_after_entry(
        strategy=SMACrossover(
            fast=contract.fast_window,
            slow=contract.slow_window,
        ),
        daily_bars=daily_bars,
        entry_minutes=usable,
        entry_at=entry_at,
        entry=entry,
        stop=stop,
        as_of=as_of,
        slippage_bps=contract.modeled_exit_slippage_bps,
        basis=_SMA_REPLAY_BASIS,
        metadata={
            **base,
            "fill_status": "filled",
            "entry_at": entry_at.isoformat(),
            "entry_price_basis": "minute_open_proxy",
            "excursion_precision": "one_minute_bounds",
        },
    )


def resolve_donchian_shadow(
    candidate: Mapping[str, Any],
    contract: DonchianReplayContract,
    *,
    daily_bars: pd.DataFrame,
    entry_minutes: pd.DataFrame,
    as_of: datetime | None = None,
    entry_window_complete: bool,
    contract_source: str,
) -> ShadowReplayResult:
    """Replay one refused Donchian DAY stop-limit and its production exits."""
    if candidate.get("strategy") != "donchian_breakout":
        raise ValueError("resolve_donchian_shadow received a non-Donchian candidate")
    if candidate.get("disposition") not in _CAPACITY_DISPOSITIONS:
        raise ValueError("Donchian shadow resolver accepts capacity refusals only")
    observed = datetime.fromisoformat(str(candidate["observed_at"]))
    if observed.tzinfo is None:
        raise ValueError("candidate observed_at must be timezone-aware")
    observed = observed.astimezone(timezone.utc)
    as_of = _as_of_utc(as_of)
    reference = float(candidate["reference_price"])
    atr = float(candidate["atr"])
    features = _decoded(candidate.get("strategy_features_json"))
    trigger = round(float(features["entry_trigger"]), 2)
    policy = EntryPriceCap(
        max_chase_bps=contract.max_chase_bps,
        max_chase_atr_fraction=contract.max_chase_atr_fraction,
    )
    limit = round(compute_cap_price(
        reference_price=trigger,
        atr=atr,
        side="buy",
        policy=policy,
    ), 2)
    stop = round(reference - contract.atr_stop_multiplier * atr, 2)
    base = {
        "replay_version": 1,
        "contract_source": contract_source,
        "contract": asdict(contract),
        "as_of": as_of.isoformat(),
        "entry_trigger": trigger,
        "entry_limit": limit,
        "stop_price": stop,
        "entry_timing_basis": "remaining complete 1-minute bars in DAY session",
        "exit_timing_basis": "next-session open after completed daily signal",
    }
    if stop <= 0 or stop >= trigger:
        return _empty_result(
            status="not_eligible",
            basis=_DONCHIAN_REPLAY_BASIS,
            metadata={
                **base,
                "unresolved_reason": "production risk gate would reject the stop",
            },
        )
    minutes = _bars(entry_minutes, name="entry minute")
    session_close = datetime.combine(
        observed.astimezone(_NY).date(), time(16, 0), _NY
    ).astimezone(timezone.utc)
    usable = minutes[
        (minutes.index >= pd.Timestamp(observed).ceil("min"))
        & (minutes.index < session_close)
    ]
    entry_at = None
    entry = None
    for timestamp, row in usable.iterrows():
        open_price = float(row["open"])
        if float(row["high"]) < trigger or float(row["low"]) > limit:
            continue
        entry_at = pd.Timestamp(timestamp)
        entry = limit if open_price > limit else max(open_price, trigger)
        break
    if entry_at is None or entry is None:
        return _empty_result(
            status="not_filled" if entry_window_complete else "pending_data",
            basis=_DONCHIAN_REPLAY_BASIS,
            metadata={
                **base,
                "fill_status": "not_filled" if entry_window_complete else "pending_data",
            },
        )
    return _resolve_equity_after_entry(
        strategy=DonchianBreakout(
            entry_window=contract.entry_window,
            exit_window=contract.exit_window,
        ),
        daily_bars=daily_bars,
        entry_minutes=usable,
        entry_at=entry_at,
        entry=float(entry),
        stop=stop,
        as_of=as_of,
        slippage_bps=contract.modeled_exit_slippage_bps,
        basis=_DONCHIAN_REPLAY_BASIS,
        metadata={
            **base,
            "fill_status": "filled",
            "entry_at": entry_at.isoformat(),
            "entry_price_basis": "stop_limit_minute_model",
            "excursion_precision": "one_minute_bounds",
        },
    )


__all__ = [
    "DonchianReplayContract",
    "RSIReplayContract",
    "SMAReplayContract",
    "ShadowReplayResult",
    "replay_contract_for_candidate",
    "resolve_donchian_shadow",
    "resolve_rsi_shadow",
    "resolve_sma_shadow",
    "historical_contract_from_settings_source",
]
