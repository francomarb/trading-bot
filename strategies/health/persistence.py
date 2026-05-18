"""
Verdict-persistence state for the Strategy Health & Edge Monitor.

`data/health_state.json` holds the small, slow-changing state the
EdgeAssessor (11.10d) needs to track across weekly runs — specifically
the 3-week persistence requirement for NEGATIVE verdicts per design §9.

Per design §12.4 there's a strict separation:
  - `data/health_state.json` → small verdict-persistence state ONLY
    (this module)
  - `strategy_lifecycle_counters` SQLite table → historical/queryable
    counter data (`strategies/health/lifecycle.py`)

Why JSON for this and SQLite for that:
  - State here is one row per strategy (~5 rows total), rewritten
    in place each week. No history. Operator-readable. JSON wins.
  - Counters are growing/queryable historical data. SQLite wins.

Atomic-write pattern (tmp + os.replace) mirrors the engine's state
snapshot at engine/trader.py:2957-2960 so a crashed write never leaves
a partial/corrupt file — the operator either sees the previous good
state or the new good state.

Schema (schema_version=1):

  {
    "schema_version": 1,
    "donchian_breakout": {
      "negative_weeks": 2,
      "last_check": "2026-05-17",
      "last_verdict": "NEGATIVE"
    },
    ...
  }

PERSISTENCE_SCHEMA_VERSION is bumped if the structure changes
materially. Forward-compatibility: unknown keys are silently ignored
so an older bot can read a newer state file (with new fields treated
as missing).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

PERSISTENCE_SCHEMA_VERSION = 1


# ── Per-strategy persistence state ─────────────────────────────────────


@dataclass(frozen=True)
class PersistenceState:
    """One strategy's verdict-persistence state across weekly runs.

    `negative_weeks` is the count of consecutive weekly checks where the
    Edge verdict has been NEGATIVE. The silent-killer alarm
    (`STRATEGY_EDGE_LOSS`) fires when this reaches 3 (design §9).

    `last_check` is the ISO date of the most recent assessment (any
    verdict). `last_verdict` is the verdict string from that check
    (`"POSITIVE"`, `"NEGATIVE"`, `"BELOW_BENCHMARK"`, `"UNDETERMINED"`).

    Frozen for immutability — mutations go through `apply_verdict` which
    returns a new state. This keeps the state machine pure and testable.
    """

    negative_weeks: int = 0
    last_check: str = ""  # ISO date "YYYY-MM-DD"
    last_verdict: str = ""

    def apply_verdict(
        self,
        new_verdict: str,
        check_date: date | str,
    ) -> "PersistenceState":
        """Return the next state after observing `new_verdict` on `check_date`.

        State machine (per design §9 persistence rule):
          - NEGATIVE → increment negative_weeks
          - Any other verdict → reset negative_weeks to 0
          - last_check + last_verdict always update to the new values

        Idempotent on the same date: re-applying the same verdict on the
        same check_date returns the identical state (no double-increment).
        """
        if isinstance(check_date, date):
            iso = check_date.isoformat()
        else:
            iso = str(check_date)
            # Validate the format upfront — silently accepting "garbage" would
            # poison downstream date math.
            datetime.fromisoformat(iso)

        if iso == self.last_check and new_verdict == self.last_verdict:
            # Idempotent — same verdict on the same day is a no-op.
            return self

        if new_verdict == "NEGATIVE":
            next_weeks = self.negative_weeks + 1
        else:
            next_weeks = 0

        return PersistenceState(
            negative_weeks=next_weeks,
            last_check=iso,
            last_verdict=new_verdict,
        )


# ── HealthStateFile (top-level container) ──────────────────────────────


@dataclass(frozen=True)
class HealthStateFile:
    """The full contents of `data/health_state.json`.

    `states` maps strategy_name → PersistenceState. Strategies not yet
    seen by any assessor are absent from the dict; consumers should
    treat absence as a fresh `PersistenceState()` (default zero-state).
    """

    schema_version: int = PERSISTENCE_SCHEMA_VERSION
    states: dict[str, PersistenceState] = field(default_factory=dict)

    def get_or_default(self, strategy: str) -> PersistenceState:
        """Return the state for `strategy`, or a default zero-state."""
        return self.states.get(strategy, PersistenceState())

    def with_updated(
        self, strategy: str, state: PersistenceState
    ) -> "HealthStateFile":
        """Return a new HealthStateFile with `strategy`'s state replaced."""
        new_states = dict(self.states)
        new_states[strategy] = state
        return HealthStateFile(
            schema_version=self.schema_version,
            states=new_states,
        )

    def to_json(self) -> str:
        """Pretty-printed JSON suitable for human inspection.

        Structure: top-level keys are `schema_version` + each strategy
        name. Per-strategy values are the PersistenceState dict.
        """
        payload: dict[str, Any] = {"schema_version": self.schema_version}
        for strategy, state in sorted(self.states.items()):
            payload[strategy] = asdict(state)
        return json.dumps(payload, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "HealthStateFile":
        """Parse JSON, tolerating unknown fields (forward-compat)."""
        data = json.loads(text)
        # Pull schema_version out; anything else is a per-strategy block.
        schema = int(data.pop("schema_version", PERSISTENCE_SCHEMA_VERSION))
        states: dict[str, PersistenceState] = {}
        # Known fields on PersistenceState — anything outside this set is
        # ignored (forward-compat for future schema additions).
        known_fields = {f.name for f in PersistenceState.__dataclass_fields__.values()}
        for strategy, blob in data.items():
            if not isinstance(blob, dict):
                # Skip stray top-level fields that aren't strategy blobs.
                continue
            filtered = {k: v for k, v in blob.items() if k in known_fields}
            states[strategy] = PersistenceState(**filtered)
        return cls(schema_version=schema, states=states)


# ── Public I/O API ─────────────────────────────────────────────────────


def default_state_path() -> Path:
    """Default location for `health_state.json` (one per bot install).

    Lives alongside the other operator-readable data files
    (data/envelopes/, data/health_reports/).
    """
    return Path(__file__).resolve().parents[2] / "data" / "health_state.json"


def load_state(path: str | Path | None = None) -> HealthStateFile:
    """Read the state file, returning a fresh empty file if missing.

    Tolerates missing file (first run after install) — returns a default
    empty HealthStateFile rather than raising. Operator-friendly.

    On malformed JSON: raises. The assessor caller is the right place
    to decide whether to fall back to zero state or refuse to proceed.
    """
    p = Path(path) if path else default_state_path()
    if not p.exists():
        return HealthStateFile()
    return HealthStateFile.from_json(p.read_text())


def save_state(state: HealthStateFile, path: str | Path | None = None) -> None:
    """Atomic-write the state to disk via tmp file + os.replace.

    Same pattern as engine/trader.py:2957-2960 — a crashed/interrupted
    write never leaves a partial file because os.replace is atomic on
    POSIX and on modern Windows. The operator either sees the previous
    good state or the new good state, never garbage.

    Creates parent directories if needed (operator-friendly first-run
    behavior).
    """
    p = Path(path) if path else default_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(state.to_json())
    os.replace(tmp, p)
