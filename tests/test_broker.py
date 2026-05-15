"""
Unit tests for execution/broker.py.

The broker is offline-tested against a mock REST client that mimics the
shape of Alpaca's TradingClient. The tests pin the contract:

  - `place_order` REJECTS any non-`RiskDecision` argument (the risk gate is
    structural, not advisory).
  - submit_order kwargs are built correctly: oto class, stop_loss leg,
    rounded prices, market vs limit type, client_order_id present.
  - Polling returns FILLED / PARTIAL / TIMEOUT / REJECTED / CANCELED in the
    right shapes.
  - cancel_order returns True on success, False on APIError.
  - close_position uses MARKET regardless of strategy preference and refuses
    if no position exists.
  - sync_with_broker bundles account + positions + open orders.
  - get_positions normalises Alpaca's position shape into Position.
  - retry wrapper retries 429 / 5xx / network, raises on 4xx, gives up after
    max_attempts.

Tests use `time.sleep` patches so the suite stays fast even when polling
loops are exercised.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alpaca.common.exceptions import APIError

from execution.broker import (
    AlpacaBroker,
    BrokerError,
    OrderResult,
    OrderStatus,
)
from execution.options_executor import SpreadLeg
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from risk.manager import (
    AccountState,
    Position,
    RiskDecision,
    Side,
)
from strategies.base import OrderType


# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeAPIError(APIError):
    """APIError subclass with a controllable status_code for tests."""

    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self._test_status = status_code

    @property
    def status_code(self):
        return self._test_status


def _api_error(status_code: int, message: str = "boom") -> _FakeAPIError:
    """Build an APIError with a usable .status_code property."""
    return _FakeAPIError(status_code, message)


def _decision(
    *,
    symbol: str = "AAPL",
    qty: int = 10,
    entry: float = 100.0,
    stop: float = 96.0,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
    strategy: str = "sma_crossover",
) -> RiskDecision:
    return RiskDecision(
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        entry_reference_price=entry,
        stop_price=stop,
        strategy_name=strategy,
        reason="test",
        order_type=order_type,
        limit_price=limit_price,
    )


def _alpaca_order(
    *,
    id: str = "ord-1",
    status: str = "filled",
    filled_qty: float = 10,
    filled_avg_price: float | None = 100.5,
    symbol: str = "AAPL",
    side: str = "buy",
    qty: float = 10,
    type: str = "market",
    limit_price: str | None = None,
    stop_price: str | None = None,
    submitted_at: str = "2026-04-15T14:30:00Z",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        status=status,
        filled_qty=str(filled_qty),
        filled_avg_price=str(filled_avg_price) if filled_avg_price is not None else None,
        symbol=symbol,
        side=side,
        qty=str(qty),
        type=type,
        limit_price=limit_price,
        stop_price=stop_price,
        submitted_at=submitted_at,
    )


def _stream_update(
    *,
    order_id: str = "ord-1",
    client_order_id: str | None = None,
    event: str = "fill",
    filled_qty: float = 10.0,
    filled_avg_price: float = 100.5,
) -> SimpleNamespace:
    return SimpleNamespace(
        event=SimpleNamespace(value=event),
        qty=filled_qty,
        price=filled_avg_price,
        order=SimpleNamespace(
            id=order_id,
            client_order_id=client_order_id,
            filled_qty=str(filled_qty),
            filled_avg_price=str(filled_avg_price),
            symbol="AAPL",
        ),
    )


def _broker_with_mock(api: MagicMock) -> AlpacaBroker:
    return AlpacaBroker(client=api, max_attempts=3, base_delay=0.0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Patch time.sleep in the broker module to keep tests instant."""
    monkeypatch.setattr("execution.broker.time.sleep", lambda *_: None)


# ── place_order: the risk-gate contract ──────────────────────────────────────


class TestPlaceOrderContract:
    def test_rejects_non_RiskDecision(self):
        api = MagicMock()
        broker = _broker_with_mock(api)
        with pytest.raises(TypeError, match="RiskDecision"):
            broker.place_order({"symbol": "AAPL", "qty": 1})  # type: ignore[arg-type]
        api.submit_order.assert_not_called()

    def test_rejects_none(self):
        api = MagicMock()
        broker = _broker_with_mock(api)
        with pytest.raises(TypeError):
            broker.place_order(None)  # type: ignore[arg-type]


class TestBrokerConnections:
    def test_close_connections_closes_underlying_session(self):
        api = MagicMock()
        api._session = MagicMock()

        broker = _broker_with_mock(api)
        broker.close_connections()

        api._session.close.assert_called_once_with()


# ── place_order: kwargs built correctly ──────────────────────────────────────


class TestSubmitOrderKwargs:
    def test_market_order_uses_oto_with_stop_loss(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="filled")
        api.get_order_by_id.return_value = _alpaca_order(status="filled")
        broker = _broker_with_mock(api)

        result = broker.place_order(_decision(stop=95.5), poll_timeout=0.1)

        assert result.status is OrderStatus.FILLED
        # alpaca-py: submit_order receives a request object as first positional arg.
        req = api.submit_order.call_args.args[0]
        assert req.symbol == "AAPL"
        assert req.qty == 10
        assert req.side.value == "buy"
        assert req.type.value == "market"
        assert req.order_class.value == "oto"
        assert req.stop_loss.stop_price == 95.5
        assert not hasattr(req, "limit_price") or getattr(req, "limit_price", None) is None
        assert req.client_order_id.startswith("sma_crossover-")
        assert req.time_in_force.value == "gtc"

    def test_limit_order_includes_limit_price(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.return_value = _alpaca_order(status="accepted", filled_qty=0)
        broker = _broker_with_mock(api)

        broker.place_order(
            _decision(order_type=OrderType.LIMIT, limit_price=99.123),
            poll_timeout=0.0,
            poll_interval=0.0,
        )
        req = api.submit_order.call_args.args[0]
        assert req.type.value == "limit"
        assert req.limit_price == 99.12  # rounded to 2dp

    def test_atr_computed_stop_price_rounded_to_2dp(self):
        """ATR-based stops produce long decimals (e.g. entry - k*ATR).
        Alpaca rejects prices with more than 2 decimal places, so the
        broker must round before submission."""
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="filled")
        api.get_order_by_id.return_value = _alpaca_order(status="filled")
        broker = _broker_with_mock(api)

        # Simulate a raw ATR-computed stop: 150.00 - 2.0 * 5.8137 = 138.3726
        raw_stop = 150.00 - 2.0 * 5.8137  # 138.3726
        broker.place_order(_decision(entry=150.0, stop=raw_stop), poll_timeout=0.1)

        req = api.submit_order.call_args.args[0]
        assert req.stop_loss.stop_price == 138.37  # rounded, not 138.3726

    def test_repair_stop_uses_simple_gtc_sell_stop(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(
            status="accepted",
            side="sell",
            type="stop",
            stop_price="95.5",
            qty=10,
        )
        broker = _broker_with_mock(api)

        result = broker.place_protective_stop(
            symbol="AAPL",
            qty=10,
            stop_price=95.5,
            client_order_id_prefix="sma-repair",
        )

        req = api.submit_order.call_args.args[0]
        assert req.symbol == "AAPL"
        assert req.qty == 10
        assert req.side.value == "sell"
        assert req.type.value == "stop"
        assert req.time_in_force.value == "gtc"
        assert req.stop_price == 95.5
        assert req.client_order_id.startswith("sma-repair-")
        assert result.side is Side.SELL
        assert result.stop_price == 95.5

    def test_stream_submit_path_watches_binds_and_unwatches(self):
        api = MagicMock()
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = False
        stream.watch.return_value = stream_event
        api.submit_order.return_value = _alpaca_order(
            status="accepted",
            id="ord-1",
        )
        api.get_order_by_id.return_value = _alpaca_order(
            status="filled",
            id="ord-1",
            filled_qty=10,
            filled_avg_price=100.5,
        )
        broker = AlpacaBroker(
            client=api,
            max_attempts=3,
            base_delay=0.0,
            stream_manager=stream,
        )

        broker.place_order(_decision(), poll_timeout=0.1, poll_interval=0.0)

        watched_client_id = stream.watch.call_args.args[0]
        assert watched_client_id.startswith("sma_crossover-")
        stream.bind_submitted_order.assert_called_once()
        bind_kwargs = stream.bind_submitted_order.call_args.kwargs
        assert bind_kwargs["client_order_id"] == watched_client_id
        assert bind_kwargs["order_id"] == "ord-1"
        assert stream.unwatch.call_args.args == ("ord-1",)

    def test_stream_terminal_update_builds_result_and_cleans_up_watch(self):
        api = MagicMock()
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = True
        stream.watch.return_value = stream_event
        api.submit_order.return_value = _alpaca_order(status="accepted", id="ord-1")
        stream.get_update.return_value = _stream_update(
            order_id="ord-1",
            client_order_id="sma_crossover-abc123",
            event="fill",
            filled_qty=10.0,
            filled_avg_price=100.75,
        )
        broker = AlpacaBroker(
            client=api,
            max_attempts=3,
            base_delay=0.0,
            stream_manager=stream,
        )

        result = broker.place_order(_decision(), poll_timeout=0.1, poll_interval=0.0)

        assert result.status is OrderStatus.FILLED
        assert result.avg_fill_price == 100.75
        api.get_order_by_id.assert_not_called()
        stream.unwatch.assert_called_once_with("ord-1")

    def test_fractional_path_uses_same_watch_bind_flow(self):
        api = MagicMock()
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = False
        stream.watch.return_value = stream_event
        entry_order = _alpaca_order(id="entry-1", status="accepted", qty=8.5)
        filled_order = _alpaca_order(
            id="entry-1",
            status="filled",
            qty=8.5,
            filled_qty=8.5,
            filled_avg_price=100.5,
        )
        stop_order = _alpaca_order(id="stop-1", status="accepted", qty=8)
        api.submit_order.side_effect = [entry_order, stop_order]
        api.get_order_by_id.return_value = filled_order

        broker = AlpacaBroker(
            client=api,
            max_attempts=3,
            base_delay=0.0,
            stream_manager=stream,
        )
        broker.place_order(_decision(qty=8.5), poll_timeout=0.1, poll_interval=0.0)

        watched_client_id = stream.watch.call_args.args[0]
        assert watched_client_id.startswith("sma_crossover-frac-")
        stream.bind_submitted_order.assert_called_once_with(
            client_order_id=watched_client_id,
            order_id="entry-1",
        )
        stream.unwatch.assert_called_once_with("entry-1")


# ── place_order: terminal-state mapping ──────────────────────────────────────


class TestPlaceOrderTerminalStates:
    def test_filled_returns_filled_with_avg_price(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="filled")
        api.get_order_by_id.return_value = _alpaca_order(
            status="filled", filled_qty=10, filled_avg_price=100.42
        )
        result = _broker_with_mock(api).place_order(_decision(), poll_timeout=0.0)
        assert result.status is OrderStatus.FILLED
        assert result.filled_qty == 10
        assert result.avg_fill_price == pytest.approx(100.42)
        assert result.is_terminal

    def test_rejected_status_at_submit_returns_rejected(self):
        api = MagicMock()
        api.submit_order.side_effect = _api_error(422, "buying power")
        result = _broker_with_mock(api).place_order(_decision(), poll_timeout=0.0)
        assert result.status is OrderStatus.REJECTED
        assert result.order_id is None
        assert "buying power" in result.message

    def test_rejected_status_after_submit(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.return_value = _alpaca_order(
            status="rejected", filled_qty=0, filled_avg_price=None
        )
        result = _broker_with_mock(api).place_order(_decision(), poll_timeout=0.0)
        assert result.status is OrderStatus.REJECTED
        assert result.filled_qty == 0
        assert result.avg_fill_price is None

    def test_canceled_status(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.return_value = _alpaca_order(
            status="canceled", filled_qty=0, filled_avg_price=None
        )
        result = _broker_with_mock(api).place_order(_decision(), poll_timeout=0.0)
        assert result.status is OrderStatus.CANCELED

    def test_timeout_with_no_fills_returns_TIMEOUT(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.return_value = _alpaca_order(
            status="accepted", filled_qty=0, filled_avg_price=None
        )
        result = _broker_with_mock(api).place_order(
            _decision(), poll_timeout=0.0, poll_interval=0.0
        )
        assert result.status is OrderStatus.TIMEOUT
        assert result.filled_qty == 0

    def test_timeout_with_partial_returns_PARTIAL(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.return_value = _alpaca_order(
            status="partially_filled", filled_qty=4, filled_avg_price=100.1
        )
        result = _broker_with_mock(api).place_order(
            _decision(qty=10), poll_timeout=0.0, poll_interval=0.0
        )
        assert result.status is OrderStatus.PARTIAL
        assert result.filled_qty == 4
        assert result.requested_qty == 10

    def test_polling_eventually_sees_fill(self):
        # First poll = pending, second = filled.
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.side_effect = [
            _alpaca_order(status="accepted", filled_qty=0, filled_avg_price=None),
            _alpaca_order(status="filled", filled_qty=10, filled_avg_price=100.5),
        ]
        result = _broker_with_mock(api).place_order(
            _decision(), poll_timeout=5.0, poll_interval=0.0
        )
        assert result.status is OrderStatus.FILLED
        assert api.get_order_by_id.call_count == 2


# ── _build_result_from_stream ─────────────────────────────────────────────────


class TestBuildResultFromStream:
    """_build_result_from_stream must use cumulative order fields, not per-event fields.

    update.qty  = quantity filled in this single execution chunk.
    update.price = price for this single execution chunk.
    update.order.filled_qty        = cumulative total across all partial fills.
    update.order.filled_avg_price  = VWAP across all partial fills.
    Using the per-event fields undercounts position size on multi-execution orders.
    """

    def _make_update(self, event: str, chunk_qty: float, chunk_price: float,
                     cum_qty: float, cum_avg_price: float, order_id: str = "ord-1"):
        order = SimpleNamespace(
            id=order_id,
            filled_qty=str(cum_qty),
            filled_avg_price=str(cum_avg_price),
        )
        update = SimpleNamespace(
            event=SimpleNamespace(value=event),
            qty=chunk_qty,
            price=chunk_price,
            order=order,
        )
        return update

    def test_fill_uses_cumulative_qty_not_chunk(self):
        # 3 partial fills of 4, 2, 0.31 then final chunk of 8 → total 14.31
        update = self._make_update(
            event="fill", chunk_qty=8.0, chunk_price=227.50,
            cum_qty=14.31, cum_avg_price=227.10,
        )
        result = AlpacaBroker._build_result_from_stream(update, "AAPL", 14.31)
        assert result.filled_qty == 14.31
        assert result.status is OrderStatus.FILLED

    def test_fill_uses_cumulative_avg_price_not_chunk_price(self):
        # chunk price is the last execution only; avg should reflect VWAP
        update = self._make_update(
            event="fill", chunk_qty=2.0, chunk_price=190.00,
            cum_qty=15.0, cum_avg_price=185.50,
        )
        result = AlpacaBroker._build_result_from_stream(update, "AMZN", 15.0)
        assert result.avg_fill_price == 185.50

    def test_chunk_qty_alone_would_undercount(self):
        # Demonstrates what the bug looked like: chunk=8 != cumulative=14.31
        update = self._make_update(
            event="fill", chunk_qty=8.0, chunk_price=227.50,
            cum_qty=14.31, cum_avg_price=227.10,
        )
        result = AlpacaBroker._build_result_from_stream(update, "AAPL", 14.31)
        assert result.filled_qty != float(update.qty), (
            "filled_qty must not equal the per-chunk qty"
        )


# ── cancel_order ─────────────────────────────────────────────────────────────


class TestCancelOrder:
    def test_success_returns_true(self):
        api = MagicMock()
        api.cancel_order_by_id.return_value = None
        assert _broker_with_mock(api).cancel_order("ord-1") is True
        api.cancel_order_by_id.assert_called_once_with("ord-1")

    def test_failure_returns_false_not_raises(self):
        api = MagicMock()
        api.cancel_order_by_id.side_effect = _api_error(404, "not found")
        assert _broker_with_mock(api).cancel_order("nope") is False


# ── close_position ───────────────────────────────────────────────────────────


class TestClosePosition:
    def test_closes_existing_position_with_market(self):
        api = MagicMock()
        # get_positions called inside close_position
        api.get_all_positions.return_value = [
            SimpleNamespace(
                symbol="AAPL", qty="10", avg_entry_price="100", market_value="1010"
            )
        ]
        api.close_position.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.return_value = _alpaca_order(
            status="filled", filled_qty=10, filled_avg_price=101.0
        )
        result = _broker_with_mock(api).close_position("AAPL", poll_timeout=0.0)
        assert result.status is OrderStatus.FILLED
        # Note: close_position uses Alpaca's close_position endpoint, which
        # always submits as market — there's no `type=` for us to check;
        # the contract is "we never call submit_order for hard exits".
        api.submit_order.assert_not_called()

    def test_no_position_raises_BrokerError(self):
        api = MagicMock()
        api.get_all_positions.return_value = []
        with pytest.raises(BrokerError, match="no open position"):
            _broker_with_mock(api).close_position("AAPL")

    def test_close_cancels_sibling_orders_first(self):
        """The OTO stop_loss leg holds the shares — Alpaca otherwise rejects
        close as 'insufficient qty available'. Hard exits must not fail
        because of an attached stop."""
        api = MagicMock()
        api.get_all_positions.return_value = [
            SimpleNamespace(
                symbol="AAPL", qty="10", avg_entry_price="100", market_value="1010"
            )
        ]
        # One sibling stop order on AAPL, one unrelated MSFT order.
        api.get_orders.return_value = [
            SimpleNamespace(
                id="aapl-stop", symbol="AAPL", side="sell", qty="10",
                type="stop", status="open", limit_price=None, stop_price="95",
                submitted_at="2026-04-15T14:30:00Z",
            ),
            SimpleNamespace(
                id="msft-1", symbol="MSFT", side="buy", qty="1",
                type="limit", status="open", limit_price="100", stop_price=None,
                submitted_at="2026-04-15T14:30:00Z",
            ),
        ]
        api.cancel_order_by_id.return_value = None
        api.close_position.return_value = _alpaca_order(status="accepted")
        api.get_order_by_id.return_value = _alpaca_order(
            status="filled", filled_qty=10, filled_avg_price=101.0
        )
        result = _broker_with_mock(api).close_position("AAPL", poll_timeout=0.0)
        assert result.status is OrderStatus.FILLED
        # Only the AAPL sibling was canceled, not the unrelated MSFT order.
        api.cancel_order_by_id.assert_called_once_with("aapl-stop")


# ── Read-side: positions + sync ──────────────────────────────────────────────


class TestReadSide:
    def test_get_positions_normalises_shape(self):
        api = MagicMock()
        api.get_all_positions.return_value = [
            SimpleNamespace(
                symbol="AAPL", qty="3", avg_entry_price="100.5", market_value="305.10"
            ),
            SimpleNamespace(
                symbol="MSFT", qty="2", avg_entry_price="400", market_value="810"
            ),
        ]
        positions = _broker_with_mock(api).get_positions()
        assert set(positions.keys()) == {"AAPL", "MSFT"}
        assert positions["AAPL"] == Position(
            symbol="AAPL", qty=3, avg_entry_price=100.5, market_value=305.10
        )

    def test_get_positions_preserves_fractional_qty(self):
        api = MagicMock()
        api.get_all_positions.return_value = [
            SimpleNamespace(
                symbol="PWR", qty="5.54", avg_entry_price="727.67", market_value="4083.26"
            ),
        ]
        positions = _broker_with_mock(api).get_positions()
        assert positions["PWR"] == Position(
            symbol="PWR",
            qty=5.54,
            avg_entry_price=727.67,
            market_value=4083.26,
        )

    def test_sync_bundles_account_positions_and_orders(self):
        api = MagicMock()
        api.get_account.return_value = SimpleNamespace(equity="100000", cash="50000")
        api.get_all_positions.return_value = [
            SimpleNamespace(
                symbol="AAPL", qty="1", avg_entry_price="100", market_value="101"
            )
        ]
        api.get_orders.return_value = [
            SimpleNamespace(
                id="o1", symbol="AAPL", side="buy", qty="1", type="limit",
                status="open", limit_price="99.5", stop_price=None,
                submitted_at="2026-04-15T14:30:00Z",
            )
        ]
        api.get_account.return_value.last_equity = "99_700".replace("_", "")
        snap = _broker_with_mock(api).sync_with_broker(session_start_equity=99_000.0)
        assert isinstance(snap.account, AccountState)
        assert snap.account.equity == 100_000.0
        assert snap.account.session_start_equity == 99_000.0
        assert snap.account.previous_close_equity == 99_700.0
        assert "AAPL" in snap.account.open_positions
        assert len(snap.open_orders) == 1
        assert snap.open_orders[0].order_id == "o1"
        assert snap.open_orders[0].order_type is OrderType.LIMIT
        assert snap.open_orders[0].limit_price == 99.5

    def test_get_account_defaults_session_start_to_current_equity(self):
        api = MagicMock()
        api.get_account.return_value = SimpleNamespace(
            equity="50000",
            cash="50000",
            last_equity="49750",
        )
        api.get_all_positions.return_value = []
        acct = _broker_with_mock(api).get_account()
        assert acct.session_start_equity == acct.equity == 50_000.0
        assert acct.previous_close_equity == 49_750.0


# ── Retry wrapper ────────────────────────────────────────────────────────────


class TestRetry:
    def test_retries_on_429_then_succeeds(self):
        api = MagicMock()
        api.submit_order.side_effect = [_api_error(429), _alpaca_order(status="filled")]
        api.get_order_by_id.return_value = _alpaca_order(status="filled")
        result = _broker_with_mock(api).place_order(_decision(), poll_timeout=0.0)
        assert result.status is OrderStatus.FILLED
        assert api.submit_order.call_count == 2

    def test_retries_on_503(self):
        api = MagicMock()
        api.get_all_positions.side_effect = [
            _api_error(503),
            _api_error(502),
            [],
        ]
        positions = _broker_with_mock(api).get_positions()
        assert positions == {}
        assert api.get_all_positions.call_count == 3

    def test_4xx_other_than_429_raises_immediately(self):
        api = MagicMock()
        api.get_all_positions.side_effect = _api_error(403, "forbidden")
        with pytest.raises(APIError):
            _broker_with_mock(api).get_positions()
        assert api.get_all_positions.call_count == 1

    def test_gives_up_after_max_attempts(self):
        api = MagicMock()
        api.get_all_positions.side_effect = _api_error(429)
        broker = AlpacaBroker(client=api, max_attempts=2, base_delay=0.0)
        with pytest.raises(APIError):
            broker.get_positions()
        assert api.get_all_positions.call_count == 2

    def test_network_error_retried(self):
        api = MagicMock()
        api.get_all_positions.side_effect = [ConnectionError("boom"), []]
        result = _broker_with_mock(api).get_positions()
        assert result == {}
        assert api.get_all_positions.call_count == 2


# ── OrderResult contract ─────────────────────────────────────────────────────


class TestOrderResult:
    def test_terminal_states(self):
        common = dict(
            order_id="o", symbol="AAPL", requested_qty=1, filled_qty=0,
            avg_fill_price=None, raw_status=None,
        )
        for s in [
            OrderStatus.FILLED,
            OrderStatus.PARTIAL,
            OrderStatus.REJECTED,
            OrderStatus.CANCELED,
            OrderStatus.TIMEOUT,
        ]:
            assert OrderResult(status=s, **common).is_terminal is True
        for s in [OrderStatus.ACCEPTED, OrderStatus.PENDING]:
            assert OrderResult(status=s, **common).is_terminal is False


# ── Fractional share sizing (10.G6) ──────────────────────────────────────────


class TestFractionalOrders:
    """Tests for the fractional-share path in place_order / _place_fractional_order.

    Routing rule: if math.floor(decision.qty) != decision.qty, the fractional
    path is taken (DAY entry + standalone GTC stop). Whole-share qty always
    takes the original OTO GTC path, regardless of FRACTIONAL_ENABLED.
    """

    def test_fractional_market_uses_day_tif_no_oto(self):
        """Fractional qty routes to _place_fractional_order: DAY TIF, no OTO."""
        api = MagicMock()
        entry_order = _alpaca_order(id="entry-1", status="accepted", qty=8.5)
        filled_order = _alpaca_order(
            id="entry-1", status="filled", qty=8.5,
            filled_qty=8.5, filled_avg_price=100.5,
        )
        stop_order = _alpaca_order(id="stop-1", status="accepted", qty=8)
        api.submit_order.side_effect = [entry_order, stop_order]
        api.get_order_by_id.return_value = filled_order

        broker = _broker_with_mock(api)
        result = broker.place_order(_decision(qty=8.5), poll_timeout=0.1)

        assert result.status is OrderStatus.FILLED
        assert api.submit_order.call_count == 2

        # First call: DAY market entry — no OTO, no stop leg.
        entry_req = api.submit_order.call_args_list[0].args[0]
        assert entry_req.time_in_force.value == "day"
        assert entry_req.qty == 8.5
        assert not hasattr(entry_req, "order_class") or getattr(entry_req, "order_class", None) is None
        assert not hasattr(entry_req, "stop_loss") or getattr(entry_req, "stop_loss", None) is None

    def test_fractional_submits_standalone_gtc_stop_after_fill(self):
        """After fill: second submit_order is a GTC stop for floor(qty) whole shares."""
        api = MagicMock()
        entry_order = _alpaca_order(id="entry-2", status="accepted", qty=8.5)
        filled_order = _alpaca_order(
            id="entry-2", status="filled", qty=8.5,
            filled_qty=8.5, filled_avg_price=100.5,
        )
        stop_order = _alpaca_order(id="stop-2", status="accepted", qty=8)
        api.submit_order.side_effect = [entry_order, stop_order]
        api.get_order_by_id.return_value = filled_order

        broker = _broker_with_mock(api)
        broker.place_order(_decision(qty=8.5, stop=96.0), poll_timeout=0.1)

        stop_req = api.submit_order.call_args_list[1].args[0]
        assert stop_req.qty == 8               # floor(8.5)
        assert stop_req.time_in_force.value == "gtc"
        assert stop_req.type.value == "stop"
        assert stop_req.stop_price == 96.0
        assert stop_req.side.value == "sell"

    def test_fractional_sub_one_share_no_stop_submitted(self):
        """When floor(qty) == 0 (qty < 1), no stop order is submitted."""
        api = MagicMock()
        entry_order = _alpaca_order(id="entry-3", status="accepted", qty=0.5)
        filled_order = _alpaca_order(
            id="entry-3", status="filled", qty=0.5,
            filled_qty=0.5, filled_avg_price=100.5,
        )
        api.submit_order.return_value = entry_order
        api.get_order_by_id.return_value = filled_order

        broker = _broker_with_mock(api)
        result = broker.place_order(_decision(qty=0.5), poll_timeout=0.1)

        assert result.status is OrderStatus.FILLED
        # Only the entry was submitted — no stop (floor(0.5) == 0).
        assert api.submit_order.call_count == 1

    def test_whole_share_uses_oto_path_unchanged(self):
        """Whole-share qty (floor(qty) == qty) always takes the OTO GTC path."""
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(status="filled")
        api.get_order_by_id.return_value = _alpaca_order(status="filled")
        broker = _broker_with_mock(api)

        result = broker.place_order(_decision(qty=10), poll_timeout=0.1)

        assert result.status is OrderStatus.FILLED
        assert api.submit_order.call_count == 1   # single OTO — no second call
        req = api.submit_order.call_args.args[0]
        assert req.order_class.value == "oto"
        assert req.stop_loss.stop_price == 96.0

    def test_fractional_dry_run_returns_filled_without_submit(self):
        """DRY_RUN: fractional path logs and returns FILLED without hitting API."""
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=3, base_delay=0.0, dry_run=True)

        result = broker.place_order(_decision(qty=8.5), poll_timeout=0.1)

        assert result.status is OrderStatus.FILLED
        api.submit_order.assert_not_called()

    def test_fractional_confirmation_failure_returns_unknown_with_order_id(self):
        """Accepted fractional entry + confirm failure returns UNKNOWN, not raise."""
        api = MagicMock()
        entry_order = _alpaca_order(id="entry-4", status="accepted", qty=8.5)
        api.submit_order.return_value = entry_order
        api.get_order_by_id.side_effect = ConnectionError("socket dropped")

        broker = _broker_with_mock(api)
        result = broker.place_order(_decision(qty=8.5), poll_timeout=0.1)

        assert result.status is OrderStatus.UNKNOWN
        assert result.order_id == "entry-4"
        assert "socket dropped" in result.message
        assert api.submit_order.call_count == 1


# ── Options path: DRY_RUN guard ──────────────────────────────────────────────


def _occ_decision(entry: float = 10.0) -> RiskDecision:
    """RiskDecision for an OCC option symbol."""
    return RiskDecision(
        symbol="SPY260516C00520000",
        side=Side.BUY,
        qty=2,
        entry_reference_price=entry,
        stop_price=7.5,
        strategy_name="spy_options_reversion",
        reason="test",
        order_type=OrderType.LIMIT,
        limit_price=entry,
    )


class TestOptionsDryRun:
    def test_dry_run_skips_option_worker_and_returns_filled(self):
        """DRY_RUN=True must short-circuit before OptionsExecutionWorker is started."""
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=True)

        result = broker.place_order(_occ_decision())

        api.submit_order.assert_not_called()
        assert result.status is OrderStatus.FILLED
        assert result.raw_status == "dry_run"
        assert result.filled_qty == 2
        assert result.avg_fill_price == 10.0

    def test_live_mode_dispatches_worker(self):
        """Without DRY_RUN, an OCC LIMIT decision must start the background worker."""
        from unittest.mock import patch
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=False)

        with patch("execution.broker.OptionsExecutionWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            result = broker.place_order(_occ_decision())

        mock_worker_cls.assert_called_once()
        mock_worker_cls.return_value.start.assert_called_once()
        assert result.status is OrderStatus.ACCEPTED
        api.submit_order.assert_not_called()  # worker handles submission, not broker directly


# ── place_spread_order / close_spread_order — MLEG (11.28) ───────────────────

_SHORT_OCC = "SPY260620P00580000"
_LONG_OCC = "SPY260620P00570000"


def _open_spread_legs() -> list[SpreadLeg]:
    return [
        SpreadLeg(occ_symbol=_SHORT_OCC, side=Side.SELL, opening=True),
        SpreadLeg(occ_symbol=_LONG_OCC, side=Side.BUY, opening=True),
    ]


class TestPlaceSpreadOrder:
    def test_submits_mleg_request_and_returns_accepted(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(id="combo-1", status="accepted")
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=False)

        # Negative limit = net credit required (Alpaca MLEG sign convention).
        result = broker.place_spread_order(
            legs=_open_spread_legs(),
            qty=2,
            limit_price=-3.256,
            strategy_name="credit_spread",
        )

        req = api.submit_order.call_args.args[0]
        assert req.order_class is AlpacaOrderClass.MLEG
        assert req.qty == 2
        assert req.limit_price == -3.26
        assert len(req.legs) == 2
        assert result.status is OrderStatus.ACCEPTED
        assert result.order_id == "combo-1"
        # OrderResult.symbol is the short (SELL) leg — the spread's defining leg.
        assert result.symbol == _SHORT_OCC

    def test_dry_run_skips_submit_and_returns_synthetic_fill(self):
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=True)

        result = broker.place_spread_order(
            legs=_open_spread_legs(),
            qty=1,
            limit_price=-3.25,
            strategy_name="credit_spread",
        )

        api.submit_order.assert_not_called()
        assert result.status is OrderStatus.FILLED
        assert result.raw_status == "dry_run"
        assert result.avg_fill_price == -3.25
        assert result.symbol == _SHORT_OCC

    def test_api_error_returns_rejected(self):
        api = MagicMock()
        api.submit_order.side_effect = _api_error(422, "MLEG not permitted")
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=False)

        result = broker.place_spread_order(
            legs=_open_spread_legs(),
            qty=1,
            limit_price=3.25,
            strategy_name="credit_spread",
        )

        assert result.status is OrderStatus.REJECTED
        assert result.order_id is None
        assert "MLEG not permitted" in result.message

    def test_single_leg_input_rejected_without_submitting(self):
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=False)

        result = broker.place_spread_order(
            legs=[SpreadLeg(occ_symbol=_SHORT_OCC, side=Side.SELL)],
            qty=1,
            limit_price=3.25,
            strategy_name="credit_spread",
        )

        api.submit_order.assert_not_called()
        assert result.status is OrderStatus.REJECTED
        assert "≥ 2 legs" in result.message

    def test_close_spread_order_reverses_legs_to_flatten_the_spread(self):
        api = MagicMock()
        api.submit_order.return_value = _alpaca_order(id="combo-close", status="accepted")
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=False)

        # Pass the *opening* legs (short SELL_TO_OPEN + long BUY_TO_OPEN).
        # close_spread_order must reverse each side so the combo actually
        # flattens: short BUY_TO_CLOSE + long SELL_TO_CLOSE.
        result = broker.close_spread_order(
            legs=_open_spread_legs(),
            qty=1,
            limit_price=1.10,
            strategy_name="credit_spread",
        )

        req = api.submit_order.call_args.args[0]
        from alpaca.trading.enums import OrderSide, PositionIntent
        by_symbol = {leg.symbol: leg for leg in req.legs}
        # Short leg: sold to open → bought to close.
        assert by_symbol[_SHORT_OCC].side is OrderSide.BUY
        assert by_symbol[_SHORT_OCC].position_intent is PositionIntent.BUY_TO_CLOSE
        # Long leg: bought to open → sold to close.
        assert by_symbol[_LONG_OCC].side is OrderSide.SELL
        assert by_symbol[_LONG_OCC].position_intent is PositionIntent.SELL_TO_CLOSE
        assert result.status is OrderStatus.ACCEPTED


# ── dispatch_spread_order / drain_spread_fills — async MLEG (11.29 PR 3b) ─────


class TestDispatchSpreadOrder:
    def test_live_mode_dispatches_worker_and_returns_accepted(self):
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=False)
        legs = _open_spread_legs()

        with patch("execution.broker.SpreadExecutionWorker") as mock_worker_cls:
            mock_worker_cls.return_value = MagicMock()
            result = broker.dispatch_spread_order(
                legs=legs, qty=1, limit_price=-1.45,
                strategy_name="credit_spread", position_id="pos-1",
            )

        mock_worker_cls.assert_called_once()
        mock_worker_cls.return_value.start.assert_called_once()
        assert result.status is OrderStatus.ACCEPTED
        assert result.symbol == _SHORT_OCC          # short leg is representative
        # Worker handles submission — broker does not submit directly.
        api.submit_order.assert_not_called()
        # Nothing drained yet — the worker reports asynchronously.
        assert broker.drain_spread_fills() == []

    def test_dry_run_queues_synthetic_fill_and_returns_accepted(self):
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=True)

        with patch("execution.broker.SpreadExecutionWorker") as mock_worker_cls:
            result = broker.dispatch_spread_order(
                legs=_open_spread_legs(), qty=1, limit_price=-1.45,
                strategy_name="credit_spread", position_id="pos-1",
            )

        mock_worker_cls.assert_not_called()  # dry run never starts a worker
        assert result.status is OrderStatus.ACCEPTED
        assert result.raw_status == "dry_run"
        drained = broker.drain_spread_fills()
        assert len(drained) == 1
        position_id, strategy_name, closing, status, filled_qty, avg_price, order_id = drained[0]
        assert position_id == "pos-1"
        assert strategy_name == "credit_spread"
        assert closing is False  # an open
        assert status == "filled"
        assert filled_qty == 1.0
        assert avg_price == pytest.approx(-1.45)

    def test_closing_dispatch_reverses_legs_and_tags_close(self):
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=True)
        result = broker.dispatch_spread_order(
            legs=_open_spread_legs(), qty=1, limit_price=1.10,
            strategy_name="credit_spread", position_id="pos-1", closing=True,
        )
        assert result.status is OrderStatus.ACCEPTED
        drained = broker.drain_spread_fills()
        position_id, strategy_name, closing, status, filled_qty, avg_price, order_id = drained[0]
        assert closing is True
        assert status == "filled"

    def test_drain_spread_fills_returns_and_clears(self):
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=True)
        broker.dispatch_spread_order(
            legs=_open_spread_legs(), qty=1, limit_price=-1.45,
            strategy_name="credit_spread", position_id="pos-1",
        )
        first = broker.drain_spread_fills()
        assert len(first) == 1
        # Second drain is empty — the queue was cleared.
        assert broker.drain_spread_fills() == []

    def test_on_fill_callback_tags_fill_with_position_id(self):
        api = MagicMock()
        broker = AlpacaBroker(client=api, max_attempts=1, base_delay=0.0, dry_run=False)
        captured = {}

        def _capture_worker(*, on_fill, **kwargs):
            captured["on_fill"] = on_fill
            return MagicMock()

        with patch("execution.broker.SpreadExecutionWorker", side_effect=_capture_worker):
            broker.dispatch_spread_order(
                legs=_open_spread_legs(), qty=2, limit_price=-1.45,
                strategy_name="credit_spread", position_id="pos-99",
            )
        # Simulate the worker reporting a fill.
        captured["on_fill"]("filled", 2.0, 1.50, "alpaca-combo-1")
        drained = broker.drain_spread_fills()
        assert drained == [
            ("pos-99", "credit_spread", False, "filled", 2.0, 1.50, "alpaca-combo-1")
        ]
