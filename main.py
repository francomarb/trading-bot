"""
Trading Bot - Entry Point
Phase 1: Connection test only.
More to come phase by phase.
"""

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

def main():
    print("Trading Bot starting...")
    print(f"Connecting to: {ALPACA_BASE_URL}")

if __name__ == "__main__":
    main()
