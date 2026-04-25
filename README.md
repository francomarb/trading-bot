# Trading Bot

A modular, strategy-agnostic algorithmic trading bot built in Python. Currently paper trading on Alpaca with the SMA crossover strategy and a full go/no-go framework for live capital deployment.

## Stack

| Component | Technology |
|---|---|
| Language | Python 3.12 |
| Broker | Alpaca Markets (paper trading → live) |
| SDK | alpaca-py (official) |
| Data | pandas |
| Indicators | Hand-rolled (SMA, EMA, ATR, RSI) |
| Backtesting | vectorbt |
| Trade Log | SQLite (`data/trades.db`) |
| Logging | loguru |

## Strategies

| Strategy | Type | Order Type | Status |
|---|---|---|---|
| SMA Crossover | Trend-following | Market | **Active — Paper Trading** |
| RSI Reversion | Mean-reversion | Limit | Implemented, not yet active |

Only SMA crossover is currently running. RSI Reversion is implemented and backtested but will not be activated until the current SMA paper run has stabilized and Phase 10 safety work is complete. Phase 10 now requires fixed per-strategy capital allocation before SMA + RSI run together, so one strategy cannot consume the other's sleeve. After Phase 10, SMA + RSI must run together in Alpaca paper mode for at least 2 weeks, target 4 weeks, before any live GO/NO-GO decision.

See [docs/strategies.md](docs/strategies.md) for full signal logic and parameters.

## Architecture

```
Engine (live loop) → Data Layer → Indicators + Strategies → Risk Manager → Broker → Reporting
```

The engine runs multiple strategy slots, each with its own symbol universe. Risk and execution are shared across all slots, and Phase 10 adds a portfolio allocation layer so each strategy has an explicit capital sleeve. Every trade is logged to SQLite and evaluated against go/no-go thresholds before live deployment.

See [docs/architecture.md](docs/architecture.md) for the full architecture guide.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure API keys
cp config/.env.example config/.env
# Edit config/.env with your Alpaca API key and secret

# 3. Verify connection
python phase1_connect.py

# 4. Run tests
pytest
```

## Running

```bash
# Start the bot in a tmux session (with caffeinate on macOS)
./start_bot.sh

# Or run directly
python forward_test.py

# Attach to the running session
tmux attach -t bot

# Stop gracefully
tmux send-keys -t bot C-c
```

## Go/No-Go Checker

Before deploying with live capital, run the go/no-go checker against paper trading results:

```bash
python scripts/gonogo.py              # human-readable report
python scripts/gonogo.py --json       # machine-readable output
```

Thresholds (from [architecture.md](docs/architecture.md)):

| Metric | Threshold |
|---|---|
| Minimum trades | >= 50 |
| Trading span | >= 4 weeks |
| Sharpe Ratio | > 1.0 |
| Max Drawdown | < 15% |
| Profit Factor | > 1.3 |
| Win Rate | > 45% |
| Avg Win / Avg Loss | > 1.5 |

For the SMA-only Phase 10 paper run, 50 closed trades is unlikely on daily bars.
Use `backtest/reconcile.py` and operational stability as the primary stabilization
gates for that run. The final live GO/NO-GO gate is the post-Phase-10 SMA + RSI
paper run; the 50-trade threshold remains a stricter live-readiness/statistical
gate for that combined system.

## Testing

```bash
# Run all unit tests
pytest

# With coverage
pytest --cov=strategies --cov=indicators --cov=reporting --cov-report=term-missing

# Integration checks (hits Alpaca paper — run manually)
python phase9_verify.py
```

## Project Status

- Phases 1–9 and 9.5 complete (data, strategies, backtesting, risk, execution, reporting, reconciliation)
- **Phase 10 in progress** — live-readiness hardening (510 tests passing)
  - ✅ Live config separation, pre-flight checklist, WatchlistSource abstraction
  - ✅ Durable position ownership from trade DB, startup reconciliation, external-close detection
  - ✅ WebSocket order/fill streaming via `TradingStream` (stream-first, REST fallback)
  - ✅ `LIVE_SIZE_MULTIPLIER`, `DRY_RUN` mode
  - ⬜ Slippage kill switch calibration (needs ≥10 real fills)
  - ⬜ SMA + RSI edge filters, capital allocation, regime gating, RSI paper activation
- Currently running SMA crossover paper trading; RSI activates after Phase 10 portfolio layer is complete

## Environment Variables

Stored in `config/.env` (never committed):

```
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_PAPER=true
```
