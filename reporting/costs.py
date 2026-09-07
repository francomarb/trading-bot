"""Versioned Alpaca retail regulatory-cost model for graduation evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from utils.option_symbols import is_occ_option


@dataclass(frozen=True)
class CostEstimate:
    amount: float | None
    complete: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegulatoryCostModel:
    """Reviewed rates applied to actual fills; slippage is already realized."""

    version: str = "alpaca-retail-us-2026-06-01-v1"
    effective_from: str = "2026-06-01"
    commission_per_order: float = 0.0
    equity_taf_per_sell_share: float = 0.000195
    equity_taf_cap_per_order: float = 9.79
    option_taf_per_sell_contract: float = 0.00329
    option_orf_per_contract: float = 0.02295
    option_occ_per_contract: float = 0.025
    sec_sell_principal_rate: float = 0.00002060
    cat_per_equivalent_share: float = 0.000003

    @property
    def sources(self) -> tuple[str, ...]:
        """Primary references reviewed for this schedule."""
        return (
            "https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf",
            "https://www.finra.org/rules-guidance/rule-filings/"
            "sr-finra-2024-019/fee-adjustment-schedule",
            "https://www.catnmsplan.com/cat-fee-alerts",
        )

    @property
    def parameters(self) -> dict[str, float]:
        """Machine-readable rates printed in every report."""
        return {
            "commission_per_order": self.commission_per_order,
            "equity_taf_per_sell_share": self.equity_taf_per_sell_share,
            "equity_taf_cap_per_order": self.equity_taf_cap_per_order,
            "option_taf_per_sell_contract": self.option_taf_per_sell_contract,
            "option_orf_per_contract": self.option_orf_per_contract,
            "option_occ_per_contract": self.option_occ_per_contract,
            "sec_sell_principal_rate": self.sec_sell_principal_rate,
            "cat_per_equivalent_share": self.cat_per_equivalent_share,
        }

    @staticmethod
    def _ceil_cent(value: float) -> float:
        return math.ceil(value * 100.0 - 1e-12) / 100.0 if value > 0 else 0.0

    def estimate(self, rows: Iterable[dict[str, Any]]) -> CostEstimate:
        """Estimate pass-through costs for one completed lifecycle.

        Rows must be actual fill records. Missing option leg prices make the
        sell-principal SEC component unknowable and therefore the whole result
        unavailable rather than understated.
        """
        total = 0.0
        reasons: set[str] = set()
        saw_buy = False
        saw_sell = False
        saw_fill = False
        charged_orders: set[str] = set()
        for index, row in enumerate(rows):
            status = str(row.get("status") or "").lower()
            if status not in {"filled", "partial", "partially_filled"}:
                continue
            qty_raw = row.get("filled_qty")
            if qty_raw is None:
                qty_raw = row.get("qty")
            try:
                qty = abs(float(qty_raw or 0.0))
            except (TypeError, ValueError):
                reasons.add("invalid_fill_quantity")
                continue
            if not math.isfinite(qty) or qty <= 0:
                continue
            saw_fill = True
            order_key = str(row.get("order_id") or f"unkeyed-fill-{index}")
            if order_key not in charged_orders:
                total += self.commission_per_order
                charged_orders.add(order_key)
            side = str(row.get("side") or "").lower()
            if side == "buy":
                saw_buy = True
            elif side == "sell":
                saw_sell = True
            else:
                reasons.add("unknown_fill_side")
                continue
            timestamp = str(row.get("timestamp") or "")
            try:
                trade_day = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).date()
            except ValueError:
                reasons.add("invalid_fill_timestamp")
                continue
            if trade_day.isoformat() < self.effective_from:
                reasons.add("outside_cost_model_period")
                continue

            option = is_occ_option(str(row.get("symbol") or ""))
            equivalent_shares = qty * (100.0 if option else 1.0)
            total += equivalent_shares * self.cat_per_equivalent_share
            if option:
                total += qty * (
                    self.option_orf_per_contract + self.option_occ_per_contract
                )
                if side == "sell":
                    total += self._ceil_cent(qty * self.option_taf_per_sell_contract)
            elif side == "sell":
                total += min(
                    self._ceil_cent(qty * self.equity_taf_per_sell_share),
                    self.equity_taf_cap_per_order,
                )

            if side == "sell" and self.sec_sell_principal_rate > 0:
                price = row.get("avg_fill_price")
                try:
                    price_value = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price_value = None
                if (
                    price_value is None
                    or not math.isfinite(price_value)
                    or price_value <= 0
                ):
                    reasons.add("missing_sell_principal")
                else:
                    principal = price_value * equivalent_shares
                    total += self._ceil_cent(principal * self.sec_sell_principal_rate)

        if not saw_fill:
            reasons.add("no_fill_rows")
        if not saw_buy or not saw_sell:
            reasons.add("incomplete_round_trip")
        if reasons:
            return CostEstimate(None, False, tuple(sorted(reasons)))
        return CostEstimate(round(total, 6), True)


DEFAULT_COST_MODEL = RegulatoryCostModel()
