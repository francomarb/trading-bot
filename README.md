# Trading Bot

A modular, strategy-agnostic algorithmic trading bot built in Python. Currently paper trading on Alpaca with two complementary strategies and a full go/no-go framework for live capital deployment.

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
| SMA Crossover | Trend-following | Market | Paper Trading |
| RSI Reversion | Mean-reversion | Limit | Paper Trading |

Running both simultaneously provides regime diversification — SMA profits in trends, RSI profits in ranges.

See [docs/strategies.md](docs/strategies.md) for full signal logic and parameters.

## Architecture

```
Engine (live loop) → Data Layer → Indicators + Strategies → Risk Manager → Broker → Reporting
```

The engine runs multiple strategy slots, each with its own symbol universe. Risk and execution are shared across all slots. Every trade is logged to SQLite and evaluated against go/no-go thresholds before live deployment.

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

## Testing

```bash
# Run all 352 unit tests
pytest

# With coverage
pytest --cov=strategies --cov=indicators --cov=reporting --cov-report=term-missing

# Integration checks (hits Alpaca paper — run manually)
python phase9_verify.py
```

## Project Status

- Phases 1-9 complete (data, strategies, backtesting, risk, execution, reporting)
- Phase 9.5 complete (forward-test infrastructure, reconciliation)
- Architecture alignment complete (SDK migration, SQLite, metrics, go/no-go)
- Currently running paper trading — awaiting 4+ weeks of data for go/no-go evaluation

## Environment Variables

Stored in `config/.env` (never committed):

```
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_PAPER=true
```
