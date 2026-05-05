"""
Forward-test launcher (Phase 10).

Starts the trading engine on paper with full reporting wired up for a
multi-week forward test. Run with:

    python forward_test.py

The bot runs continuously until SIGINT (Ctrl+C). On shutdown it writes
a daily P&L report for the session. After the multi-week run, use
`backtest/reconcile.py` to compare paper fills against backtest predictions.

Reconcile after the run:

    python -c "
    from backtest.reconcile import Reconciler
    from config import settings
    from strategies.sma_crossover import SMACrossover
    r = Reconciler(
        SMACrossover(20, 50),
        list(settings.SMA_WATCHLIST),
        'YYYY-MM-DD',
        'YYYY-MM-DD',
    )
    result = r.run()
    r.write_report(result)
    print('GO' if result.go else 'NO-GO', result.reasons)
    "
"""

from __future__ import annotations

import sys
import subprocess
from datetime import datetime, timezone
from collections import defaultdict

from loguru import logger

from config import settings
from engine.trader import EngineConfig, TradingEngine
from execution.broker import AlpacaBroker
from execution.stream import StreamManager
from regime.detector import MarketRegime, RegimeDetector
from risk.allocator import SleeveAllocator
from reporting.alerts import AlertDispatcher, LogFileBackend, TelegramAlertBackend, TelegramCommandListener
from reporting.logger import TradeLogger, install_json_sink
from reporting.pnl import PnLTracker
from risk.manager import RiskManager
from data.watchlists import StaticWatchlistSource
from sector.gauge import SectorMomentumGauge
from sector.resolver import SectorResolver
from strategies.base import StrategySlot
from strategies.donchian_breakout import DonchianBreakout
from strategies.filters.common import CompositeEdgeFilter
from strategies.filters.donchian_breakout import DonchianEdgeFilter
from strategies.filters.rsi_reversion import RSIEdgeFilter
from strategies.filters.sector_momentum import SectorMomentumFilter
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.rsi_reversion import RSIReversion
from strategies.sma_crossover import SMACrossover


# ── Logging ──────────────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
        "{message}"
    ),
    level="INFO",
)
logger.add("logs/forward_test.log", rotation="10 MB", retention="90 days", level="DEBUG")


def _build_sector_heat_snapshot(
    *,
    gauge: SectorMomentumGauge,
    resolver: SectorResolver,
    slots: list[StrategySlot],
) -> dict:
    """Build a session-stable sector heat payload for the dashboard snapshot."""
    details = {
        sector: gauge.get_details(sector)
        for sector in sorted(settings.SECTOR_ETFS)
    }
    counts = {"hot": 0, "neutral": 0, "cold": 0}
    sectors_payload: dict[str, dict] = {}
    symbol_map: dict[str, list[dict[str, object]]] = defaultdict(list)
    unmapped: list[dict[str, object]] = []

    for sector, detail in details.items():
        counts[detail.classification.value] += 1
        sectors_payload[sector] = {
            "etf_ticker": detail.etf_ticker,
            "score": int(detail.score),
            "classification": detail.classification.value,
            "above_sma200": bool(detail.above_sma200),
            "above_sma50": bool(detail.above_sma50),
            "golden_cross": bool(detail.golden_cross),
            "dist_sma50_pct": float(detail.dist_sma50_pct),
            "vol_confirm": bool(detail.vol_confirm),
            "last_close": float(detail.last_close) if detail.last_close is not None else None,
        }

    for slot in slots:
        strategy_name = slot.strategy.name
        for symbol in slot.active_symbols():
            sector = resolver.resolve(symbol)
            item = {"symbol": symbol, "strategy": strategy_name}
            if sector is None:
                unmapped.append(item)
            else:
                symbol_map[sector].append(item)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "sectors": sectors_payload,
        "symbol_map": {sector: sorted(items, key=lambda x: (x["symbol"], x["strategy"]))
                       for sector, items in sorted(symbol_map.items())},
        "unmapped": sorted(unmapped, key=lambda x: (x["symbol"], x["strategy"])),
    }


def _git_version() -> str:
    """Return a concise git identity for the running bot code."""
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--tags"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main() -> None:
    logger.info("=" * 60)
    logger.info("Forward Test — Paper Trading (Phase 10)")
    logger.info("=" * 60)
    logger.info(f"bot version: {_git_version()}")
    logger.info(f"python={sys.version.split()[0]} paper={settings.ALPACA_PAPER}")

    # JSON structured log.
    install_json_sink()

    # ── Capital sleeve allocator (Phase 10.F1) ──────────────────────────
    # Enforces per-strategy gross-notional budgets so SMA cannot starve RSI
    # (or vice versa). Weights are 50/50 until ≥4 weeks of combined paper
    # data justify a rebalance. Idle sleeve capital stays locked.
    allocator = SleeveAllocator(
        allocations=settings.STRATEGY_ALLOCATIONS,
        total_gross_pct=settings.MAX_GROSS_EXPOSURE_PCT,
        min_trade_notional=settings.MIN_TRADE_NOTIONAL,
        dd_threshold=settings.STRATEGY_SLEEVE_DD_THRESHOLD,
    )

    # ── Regime detector (Phase 10.F2) ───────────────────────────────────
    # Classifies SPY into BEAR / VOLATILE / TRENDING / RANGING once per
    # cycle with a 10-minute TTL cache. Each slot's allowed_regimes gates
    # new entries; exits are never blocked.
    regime = RegimeDetector()

    # ── Sector momentum gauge ──────────────────────────────────────────
    # Maps stock → sector via yfinance metadata (cached in JSON), then
    # scores each sector ETF as HOT / NEUTRAL / COLD.  Strategies choose
    # how to act on the information via sector_entry_policy.
    sector_resolver = SectorResolver(
        valid_sectors=set(settings.SECTOR_ETFS),
    )
    all_symbols = list(dict.fromkeys(
        settings.SMA_WATCHLIST + settings.RSI_WATCHLIST + settings.DONCHIAN_WATCHLIST
    ))
    sector_resolver.hydrate(all_symbols)

    sector_gauge = SectorMomentumGauge(sector_etfs=settings.SECTOR_ETFS)

    # ── Strategy slots ──────────────────────────────────────────────────
    slots = [
        StrategySlot(
            strategy=SMACrossover(
                fast=20, slow=50,
                edge_filter=CompositeEdgeFilter([
                    SMAEdgeFilter(),
                    SectorMomentumFilter(
                        gauge=sector_gauge, resolver=sector_resolver,
                        sector_entry_policy="warn",
                    ),
                ]),
            ),
            watchlist_source=StaticWatchlistSource(
                list(settings.SMA_WATCHLIST), name="sma"
            ),
            # SMA crossover works in both trending and ranging markets;
            # blocked only in BEAR and VOLATILE where edge and risk/reward
            # degrade significantly.
            allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
        ),
        StrategySlot(
            strategy=RSIReversion(
                period=14, oversold=30, overbought=70,
                edge_filter=CompositeEdgeFilter([
                    RSIEdgeFilter(),
                    SectorMomentumFilter(
                        gauge=sector_gauge, resolver=sector_resolver,
                        sector_entry_policy="block",
                        score_threshold=-3,
                    ),
                ]),
            ),
            watchlist_source=StaticWatchlistSource(
                list(settings.RSI_WATCHLIST), name="rsi"
            ),
            # RSI reversion works in both trending and ranging markets;
            # blocked in BEAR (stocks can keep falling past oversold) and
            # VOLATILE (fear-driven overshoots are unpredictable and the
            # snap-back timing is unreliable). The RSI edge filter adds a
            # second layer: SPY > 200 SMA AND SPY > 50 SMA, so BEAR is
            # double-blocked. Sector momentum: COLD sectors BLOCK entries
            # (mean-reversion in a cold sector = cluster risk).
            allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
        ),
        StrategySlot(
            strategy=DonchianBreakout(
                entry_window=30, exit_window=15,
                edge_filter=CompositeEdgeFilter([
                    DonchianEdgeFilter(),
                    SectorMomentumFilter(
                        gauge=sector_gauge, resolver=sector_resolver,
                        sector_entry_policy="warn",
                    ),
                ]),
            ),
            watchlist_source=StaticWatchlistSource(
                list(settings.DONCHIAN_WATCHLIST), name="donchian"
            ),
            # Donchian breakout is a pure trend-continuation strategy.
            # Literature is unanimous: restrict to TRENDING only.
            # RANGING → every N-day high is a false breakout that reverses;
            # BEAR    → blocked by regime detector (no long entries in downtrend);
            # VOLATILE → erratic price action produces whipsaws with wide ATR stops.
            # Backtest validation: Mid-range (30/15), Sharpe +0.85, 32-name
            # AI/Bigtech universe, 4y window ending 2026-04-28 (2× ATR stops).
            # Sleeve: 0.25 weight, max 5 concurrent positions.
            allowed_regimes=frozenset({MarketRegime.TRENDING}),
        ),
    ]

    # Risk manager with production settings (shared across all slots).
    risk = RiskManager()

    # WebSocket stream for real-time fill/stop-out detection (Phase 10.E1).
    stream = StreamManager(
        api_key=settings.ALPACA_API_KEY or "",
        secret_key=settings.ALPACA_SECRET_KEY or "",
        paper=settings.ALPACA_PAPER,
    )

    # Broker (paper) — stream wired for stream-first fill detection.
    broker = AlpacaBroker(stream_manager=stream)

    # Reporting.
    trade_logger = TradeLogger()
    pnl_tracker = PnLTracker()

    # Pluggable alert backends — Telegram is opt-in via env vars.
    alert_backends = [LogFileBackend()]
    telegram_backend: TelegramAlertBackend | None = None
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        telegram_backend = TelegramAlertBackend(
            settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID
        )
        alert_backends.append(telegram_backend)
        logger.info("Telegram alert backend enabled")
    alerts = AlertDispatcher(backends=alert_backends)

    # Engine config (engine-level only; symbols/timeframes live on slots).
    config = EngineConfig(
        history_lookback_days=settings.ENGINE_HISTORY_LOOKBACK_DAYS,
        cycle_interval_seconds=settings.ENGINE_CYCLE_INTERVAL_SECONDS,
        max_bar_age_multiplier=settings.ENGINE_MAX_BAR_AGE_MULTIPLIER,
        market_hours_only=True,
        cancel_orders_on_shutdown=settings.ENGINE_CANCEL_ORDERS_ON_SHUTDOWN,
    )

    engine = TradingEngine(
        slots=slots,
        risk=risk,
        broker=broker,
        config=config,
        trade_logger=trade_logger,
        pnl_tracker=pnl_tracker,
        alerts=alerts,
        stream_manager=stream,
        regime_detector=regime,
        allocator=allocator,
    )
    engine._sector_heat = _build_sector_heat_snapshot(
        gauge=sector_gauge,
        resolver=sector_resolver,
        slots=slots,
    )

    slot_desc = ", ".join(
        f"{s.strategy.name}({s.active_symbols()})" for s in slots
    )
    logger.info(f"slots: {slot_desc}")
    logger.info(f"cycle={config.cycle_interval_seconds}s")
    logger.info("starting engine — Ctrl+C to stop")
    logger.info("after multi-week run, use backtest/reconcile.py to evaluate")

    # Start Telegram command listener if configured (runs as daemon thread).
    if (
        telegram_backend is not None
        and settings.TELEGRAM_COMMANDS_ENABLED
    ):
        cmd_listener = TelegramCommandListener(telegram_backend)
        cmd_listener.start(engine)
        logger.info("Telegram command listener started (/status, /halt)")

    try:
        engine.start()
    finally:
        # Write a daily summary on shutdown.
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            snap = broker.sync_with_broker()
            summary = pnl_tracker.generate_daily_summary(
                day=today,
                session_start_equity=engine._session_start_equity or snap.account.equity,
                session_end_equity=snap.account.equity,
            )
            pnl_tracker.write_daily_report(summary)
            # Fire EOD summary alert (Telegram + log).
            total_trades = sum(s.trade_count for s in summary.strategies.values())
            total_wins = sum(s.wins for s in summary.strategies.values())
            overall_win_rate = total_wins / total_trades if total_trades > 0 else 0.0
            alerts.eod_summary(
                daily_pnl=summary.realized_pnl,
                trade_count=total_trades,
                win_rate=overall_win_rate,
            )
        except Exception as e:
            logger.error(f"shutdown P&L report failed: {e}")

        logger.info("forward test session ended")


if __name__ == "__main__":
    main()
