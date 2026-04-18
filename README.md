# Trading Bot

Algorithmic trading bot built with Python 3.11, Alpaca API, pandas, pandas-ta, and vectorbt.

## Stack
- **Broker**: Alpaca (paper trading first)
- **Data**: pandas + pandas-ta
- **Backtesting**: vectorbt

## Setup
1. `pip install -r requirements.txt`
2. Copy `config/.env.example` to `config/.env` and add your Alpaca keys
3. Run `python phase1_connect.py` to verify your connection

## Phases
- [x] Phase 1: Environment & broker connection
- [ ] Phase 2: Data pipeline
- [ ] Phase 3: Strategy #1 (EMA crossover) + backtest
- [ ] Phase 4: Risk manager & execution layer
- [ ] Phase 5: Live trading & monitoring
