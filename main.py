"""
Trading Bot - Entry Point
Phase 1: Connection test only.
More to come phase by phase.
"""

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER

def main():
    mode = "paper" if ALPACA_PAPER else "LIVE"
    print(f"Trading Bot starting... (mode: {mode})")

if __name__ == "__main__":
    main()
