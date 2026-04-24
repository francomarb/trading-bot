"""
Pre-flight checklist for live trading (Phase 10.B2).

Run this script and confirm it exits 0 before setting LIVE_TRADING=true.
Every check is explicit so failures point directly to what needs fixing.

Usage:
    LIVE_TRADING=true GONOGO_APPROVED=yes python scripts/preflight.py
"""

from __future__ import annotations

import os
import sys


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"  OK    {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def run() -> int:
    """Run all pre-flight checks. Returns 0 if all pass, 1 if any fail."""
    from config import settings

    print("=" * 60)
    print("Pre-flight checklist — live trading")
    print("=" * 60)

    failures = 0

    # 1. LIVE_TRADING flag must be explicitly set.
    if not settings.LIVE_TRADING:
        _fail("LIVE_TRADING is not set to true — run with LIVE_TRADING=true")
        failures += 1
    else:
        _ok("LIVE_TRADING=true")

    # 2. Live API credentials must be present.
    if not settings._ALPACA_API_KEY_LIVE:
        _fail("ALPACA_API_KEY_LIVE is not set in config/.env")
        failures += 1
    else:
        _ok("ALPACA_API_KEY_LIVE present")

    if not settings._ALPACA_SECRET_KEY_LIVE:
        _fail("ALPACA_SECRET_KEY_LIVE is not set in config/.env")
        failures += 1
    else:
        _ok("ALPACA_SECRET_KEY_LIVE present")

    # 3. ALPACA_PAPER must be False when LIVE_TRADING is True.
    if settings.ALPACA_PAPER:
        _fail(
            "ALPACA_PAPER is True but LIVE_TRADING is also True — "
            "these are mutually exclusive. Check config/settings.py derivation."
        )
        failures += 1
    else:
        _ok("ALPACA_PAPER=False (derived correctly from LIVE_TRADING)")

    # 4. Slippage drift kill switch must be enabled before live.
    if not settings.SLIPPAGE_DRIFT_ENABLED:
        _fail(
            "SLIPPAGE_DRIFT_ENABLED is False — enable it in config/settings.py "
            "after calibrating slippage from paper fills (≥ SLIPPAGE_DRIFT_MIN_SAMPLES)"
        )
        failures += 1
    else:
        _ok("SLIPPAGE_DRIFT_ENABLED=True")

    # 5. Hard dollar loss cap must be ≤ $1,000 for initial live launch.
    cap = settings.HARD_DOLLAR_LOSS_CAP
    if cap > 1_000.0:
        _fail(
            f"HARD_DOLLAR_LOSS_CAP=${cap:,.2f} — set to ≤ $1,000 "
            "for the initial live launch. Raise it deliberately after proving stability."
        )
        failures += 1
    else:
        _ok(f"HARD_DOLLAR_LOSS_CAP=${cap:,.2f} (≤ $1,000)")

    # 6. Go/no-go approval must be confirmed.
    gonogo = os.getenv("GONOGO_APPROVED", "").strip().lower()
    if gonogo not in ("yes", "true", "1"):
        _fail(
            "GONOGO_APPROVED env var not set — run scripts/gonogo.py first "
            "and set GONOGO_APPROVED=yes if the result is GO"
        )
        failures += 1
    else:
        _ok("GONOGO_APPROVED=yes")

    # 7. Live trade DB must be separate from paper DB.
    if settings.TRADE_LOG_DB == settings.TRADE_LOG_DB_PAPER:
        _fail(
            f"TRADE_LOG_DB is the paper DB ({settings.TRADE_LOG_DB_PAPER}) — "
            "LIVE_TRADING=true should route to TRADE_LOG_DB_LIVE"
        )
        failures += 1
    else:
        _ok(f"TRADE_LOG_DB → {settings.TRADE_LOG_DB} (live DB)")

    # 8. Broker connectivity (must have live credentials at this point).
    if failures == 0:
        try:
            from execution.broker import AlpacaBroker
            broker = AlpacaBroker()
            snap = broker.sync_with_broker()
            equity = snap.account.equity
            _ok(f"broker connectivity — equity=${equity:,.2f}")
        except Exception as e:
            _fail(f"broker connectivity failed: {e}")
            failures += 1
    else:
        _warn("skipping broker connectivity check — fix the above failures first")

    print("=" * 60)
    if failures == 0:
        print("All checks passed — safe to run with LIVE_TRADING=true")
    else:
        print(f"{failures} check(s) failed — do NOT go live", file=sys.stderr)

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
