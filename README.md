# Trading Bot

A modular, strategy-agnostic algorithmic trading bot built in Python. Six strategy sleeves are running simultaneously in Alpaca paper trading, with per-strategy graduation criteria for possible live inclusion.

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
| SMA Crossover | Trend-following | Market | 30% equity | **Active — Paper Trading** |
| RSI Reversion | Mean-reversion | Limit | 15% equity | **Active — Paper Trading** |
| Donchian Breakout | Trend continuation | Stop-limit | 15% equity | **Active — Paper Trading** |
| Leveraged Trend | Benchmark-confirmed trend | Market | 25% equity | **Active — Paper Trading** |
| SPY Options RSI Reversion | Options mean-reversion | Limit (OCC) | 5% isolated options | **Active — Paper Trading** |
| Credit Spread | Short-premium options | Limit MLEG | 10% isolated options | **Active — Paper Trading** |

See [docs/strategies.md](docs/strategies.md) for full signal logic, parameters, and exit guards.

## Architecture

```
Engine (live loop) → Data Layer → Indicators + Strategies → Risk Manager → Broker → Reporting
```

The engine runs multiple strategy slots, each with its own symbol universe. Risk and execution are shared across all slots, with an allocator that splits deployable capital into an 85% equity pool and a 15% isolated-options pool. Trades and operational evidence are recorded for later per-strategy graduation review.

See [docs/architecture.md](docs/architecture.md) for the full architecture guide.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure API keys
cp config/.env.example config/.env
# Edit config/.env with your Alpaca API key and secret

# 3. Run tests
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

## Strategy Graduation

Paper strategies are evaluated individually for possible live inclusion. No
strategy is preselected, and the operator makes the final decision from the
documented profitability, risk, execution, and operational evidence. The former
portfolio-wide `gonogo.py` checker was retired because it could not represent
the bot's current strategy and position types. See [PLAN.md](PLAN.md) and the
[architecture guide](docs/architecture.md) for the graduation criteria and the
tracked replacement report.

Fifty closed trades is a target for sample depth, not an automatic universal
gate. Slower strategies may be reviewed with fewer outcomes only when the
smaller sample and untested conditions are explicit. Reconciliation, strategy
health, execution quality, and operational reliability remain separate parts
of the evidence package.

## Testing

```bash
# Run all unit tests
pytest

# With coverage
pytest --cov=strategies --cov=indicators --cov=reporting --cov-report=term-missing

# Legacy paper integration checks (hit Alpaca paper — run manually)
python scripts/legacy_verify/phase9_verify.py
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
