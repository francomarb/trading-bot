"""Navigation and read-model tests for the multipage dashboard shell."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dashboard import (
    DashboardContext,
    MARKET_OBSERVATORY_VIEWS,
    _attention_items,
    _render_market_page,
    _render_market_observatory_view,
    _open_position_rows,
    _summarize_unrealized_pnl,
    render_dashboard,
)


def _context(
    *,
    state: dict | None = None,
    display_state: dict | None = None,
) -> DashboardContext:
    base_state = {
        "running": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": "RANGING",
    }
    if state:
        base_state.update(state)
    return DashboardContext(
        state=base_state,
        display_state=display_state or base_state,
        trades_df=pd.DataFrame(),
        live_trading=False,
        db_path="data/trades.db",
        account_metrics={
            "equity": 100_000.0,
            "equity_delta": 0.0,
            "daily_pnl": 0.0,
            "session_pnl": 0.0,
            "previous_close_equity": 100_000.0,
            "source": "snapshot",
        },
        account_warning=None,
        trades_load_error=None,
    )


class TestDashboardNavigation:
    def test_registers_five_focused_pages_in_order(self) -> None:
        pages: list[dict] = []
        navigation = MagicMock()

        def page_factory(callable_, **kwargs):
            pages.append({"callable": callable_, **kwargs})
            return MagicMock()

        with (
            patch("dashboard.st.set_page_config"),
            patch("dashboard.inject_styles"),
            patch("dashboard.st.Page", side_effect=page_factory),
            patch("dashboard.st.navigation", return_value=navigation) as nav,
        ):
            render_dashboard()

        assert [page["title"] for page in pages] == [
            "Overview",
            "Positions & Risk",
            "Strategies & Performance",
            "Market Observatory",
            "Activity & Execution",
        ]
        assert pages[0]["default"] is True
        assert [page["url_path"] for page in pages] == [
            "overview",
            "positions-risk",
            "strategies-performance",
            "market-observatory",
            "activity-execution",
        ]
        nav.assert_called_once()
        assert nav.call_args.kwargs == {"position": "top"}
        navigation.run.assert_called_once_with()

    def test_market_observatory_views_are_task_focused(self) -> None:
        assert MARKET_OBSERVATORY_VIEWS == (
            "Pre-Open",
            "Trend & Regime",
            "Sector Breadth",
            "Earnings Clusters",
        )

    @pytest.mark.parametrize(
        ("view", "expected_renderer"),
        [
            ("Pre-Open", "preopen"),
            ("Trend & Regime", "trend"),
            ("Sector Breadth", "sector"),
            ("Earnings Clusters", None),
        ],
    )
    def test_market_observatory_renders_only_selected_tool(
        self,
        view: str,
        expected_renderer: str | None,
    ) -> None:
        renderers = {
            "preopen": MagicMock(),
            "trend": MagicMock(),
            "sector": MagicMock(),
        }
        with (
            patch("dashboard._render_preopen_pulse", renderers["preopen"]),
            patch("dashboard._render_leveraged_trend_panel", renderers["trend"]),
            patch("dashboard._render_sector_heat", renderers["sector"]),
            patch("dashboard.render_section_header"),
            patch("dashboard.st.caption"),
            patch("dashboard.st.info"),
        ):
            _render_market_observatory_view(view, _context())

        for name, renderer in renderers.items():
            assert renderer.call_count == (1 if name == expected_renderer else 0)

    def test_market_observatory_rejects_unknown_view(self) -> None:
        with pytest.raises(ValueError, match="unsupported Market Observatory view"):
            _render_market_observatory_view("Unknown", _context())

    def test_market_page_routes_radio_selection_to_one_view(self) -> None:
        ctx = _context()
        with (
            patch("dashboard._load_dashboard_context", return_value=ctx),
            patch("dashboard._render_page_title"),
            patch("dashboard._render_status_strip"),
            patch("dashboard.st.divider"),
            patch(
                "dashboard.st.radio",
                return_value="Sector Breadth",
            ) as radio,
            patch("dashboard._render_market_observatory_view") as render_view,
        ):
            _render_market_page.__wrapped__()

        radio.assert_called_once_with(
            "Observatory view",
            MARKET_OBSERVATORY_VIEWS,
            horizontal=True,
            label_visibility="collapsed",
            key="market_observatory_view",
        )
        render_view.assert_called_once_with("Sector Breadth", ctx)


class TestDashboardReadModels:
    def test_unrealized_summary_discloses_missing_marks(self) -> None:
        total, missing = _summarize_unrealized_pnl(
            [{"Unrealized P&L": 12.5}, {"Unrealized P&L": None}],
            [{"Net Spread P&L": -2.5}, {"Net Spread P&L": None}],
        )

        assert total == 10.0
        assert missing == 2

    def test_open_position_rows_preserve_exact_lifecycle_identity(self) -> None:
        ctx = _context(display_state={
            "open_positions": {
                "AAPL": "sma_crossover",
                "spread-uid": "credit_spread",
            },
            "positions_detail": {
                "AAPL": {
                    "position_uid": "position-uid",
                    "qty": 2.0,
                    "avg_entry_price": 100.0,
                    "unrealized_pnl": 10.0,
                },
            },
        })

        rows = _open_position_rows(ctx)

        assert len(rows) == 1
        assert rows[0]["Lifecycle ID"] == "position-uid"
        assert rows[0]["Cost Basis"] == 200.0
        assert rows[0]["Unrealized %"] == 5.0

    def test_offline_or_stale_snapshot_is_actionable(self) -> None:
        ctx = _context(state={
            "running": False,
            "timestamp": "2020-01-01T00:00:00+00:00",
        })
        with patch("dashboard._load_health_state", return_value=None):
            items = _attention_items(ctx)

        messages = [message for _, message in items]
        assert "Engine is offline." in messages
        assert any("snapshot is stale" in message for message in messages)

    def test_health_alarm_is_promoted_to_overview_attention(self) -> None:
        ctx = _context()
        with patch(
            "dashboard._load_health_state",
            return_value={
                "rsi_reversion": {
                    "negative_weeks": 3,
                    "last_verdict": "NEGATIVE",
                }
            },
        ):
            items = _attention_items(ctx)

        assert (
            "error",
            "rsi_reversion: 3 consecutive negative-signal weeks.",
        ) in items
