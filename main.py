"""
Trading Bot — top-level entry point.

This file delegates to `forward_test.py`, which is the canonical runtime
entrypoint for paper and forward-test runs.

    python main.py          # same as: python forward_test.py

For the lower-level engine without full reporting wired up:

    python -m engine.trader

Phase 1 note: this file originally only printed the mode and exited.
That stub has been replaced with this delegation. See PLAN.md for history.
"""

import forward_test

if __name__ == "__main__":
    forward_test.main()
