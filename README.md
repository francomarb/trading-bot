# Trading Bot

A modular, strategy-agnostic algorithmic trading bot built in Python. Four strategies running simultaneously in Alpaca paper trading, with a full go/no-go framework for live capital deployment.

## Stack

| Component | Technology |
|---|---|
| Language | Python 3.12 |
| Broker | Alpaca Markets (paper trading → live) |
| SDK | alpaca-py (official) |
| Data | pandas |
| Indicators | Hand-rolled (SMA, EMA, ATR, RSI, ADX, Bollinger Bands, Donchian) |
| Backtesting | vectorbt |
| Options Pricing | blackscholes (Black-Scholes Delta + price) |
| Sector Data | yfinance (sector resolver, VIX) |
| Dashboard | streamlit + plotly |
| Trade Log | SQLite (`data/trades.db` paper / `data/trades_live.db` live) |
| Logging | loguru |

## Strategies

| Strategy | Type | Order Type | Sleeve | Status |
|---|---|---|---|---|
| SMA Crossover | Trend-following | Market | 45% | **Active — Paper Trading** |
| RSI Reversion | Mean-reversion | Limit | 25% | **Active — Paper Trading** |
| Donchian Breakout | Trend continuation | Market | 25% | **Active — Paper Trading** |
| SPY Options RSI Reversion | Options mean-reversion | Limit (OCC) | 5% | **Active — Paper Trading** |

See [docs/strategies.md](docs/strategies.md) for full signal logic, parameters, and exit guards.

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

The 50-trade threshold is a statistical live-readiness gate. With four active
strategies the combined trade rate is higher, but daily-bar trend strategies
(SMA, Donchian) still generate trades slowly. Use `backtest/reconcile.py` and
operational stability alongside the trade-count gate.

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

See [PLAN.md](PLAN.md) for the current phase, completed items, and remaining blockers before the live flip.

## Environment Variables

Stored in `config/.env` (never committed):

```
# Paper credentials (default)
ALPACA_API_KEY=your_paper_key
ALPACA_SECRET_KEY=your_paper_secret

# Live credentials (only used when LIVE_TRADING=true)
ALPACA_API_KEY_LIVE=your_live_key
ALPACA_SECRET_KEY_LIVE=your_live_secret

# Runtime flags
LIVE_TRADING=false          # Set true only after preflight.py exits 0
DRY_RUN=false               # Log orders without submitting
LIVE_SIZE_MULTIPLIER=0.25   # Scale live position sizes to 25% at launch
```
