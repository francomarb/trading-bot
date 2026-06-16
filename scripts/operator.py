"""Operator control CLI (Operator Controls Phase A PR-2).

Reads bot state and writes operator commands to the durable queue
defined in `engine.operator_queue`. The CLI itself NEVER calls Alpaca
and NEVER mutates the engine directly — it writes a row to the
`operator_commands` table, then the running engine drains the queue
on its per-cycle poll. See `docs/operator_controls_proposal.md` §4
for the design rationale.

Subcommands (Phase A):

  status               — running state + cycle + halt + open positions
  positions            — list open lifecycle rows (equity only in Phase A)
  show-position <uid>  — full lifecycle metadata + linked trades
  commands [--limit N] — recent operator command audit trail
  halt                 — write a sticky halt command (requires --confirm halt)
  resume-after-halt    — clear sticky halt (requires --confirm resume)

Local-only by design (proposal §6). The CLI is the bot OS user's
authority boundary — anyone with shell access on this machine can run
it. Write commands all require `--reason` and `--confirm` so a typo
cannot fire a destructive action.

Phase B/C subcommands (pause-entries, reduce-position, etc.) are NOT
present here. They would be added in their own PR following the
proposal §5 sequencing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Iterable

# Path bootstrap so `python scripts/operator.py` works from any cwd
# without an editable install. Mirrors the pattern in scripts/gonogo.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402
from engine.lifecycle import PositionLifecycleStore  # noqa: E402
from engine.operator_queue import (  # noqa: E402
    OperatorCommandStore,
    new_command_uid,
)


# ── Connection / store helpers ────────────────────────────────────────


def _open_db(path: str) -> sqlite3.Connection:
    """Open the trade DB read/write.

    The CLI is the only writer for `operator_commands` rows, so we
    don't go through `TradeLogger` (which would also run all the
    other migrations). We just require the file to exist — if the
    engine has never run, the schema isn't there yet and the CLI
    refuses to operate.
    """
    if not os.path.exists(path):
        sys.stderr.write(
            f"error: trade DB not found at {path}\n"
            "the engine must run at least once to create the schema.\n"
        )
        sys.exit(2)
    return sqlite3.connect(path)


def _operator_command_store(conn: sqlite3.Connection) -> OperatorCommandStore:
    # Sanity-check that the engine has migrated the operator_commands
    # table. A clearer error than "no such table" on the first INSERT.
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='operator_commands'"
    ).fetchone()
    if not has:
        sys.stderr.write(
            "error: operator_commands table missing — start the bot once "
            "on a build that includes Operator Controls Phase A PR-2 to "
            "run the migration.\n"
        )
        sys.exit(2)
    return OperatorCommandStore(conn)


def _lifecycle_store(conn: sqlite3.Connection) -> PositionLifecycleStore:
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='position_lifecycle'"
    ).fetchone()
    if not has:
        sys.stderr.write(
            "error: position_lifecycle table missing — start the bot once "
            "on a build that includes Operator Controls Phase A to run "
            "the migration.\n"
        )
        sys.exit(2)
    return PositionLifecycleStore(conn)


def _load_engine_state(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"warning: could not read {path}: {exc}\n"
        )
        return None


def _requested_by() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


# ── Rendering helpers ────────────────────────────────────────────────


def _short(uid: str | None, width: int = 18) -> str:
    if not uid:
        return "-"
    return uid if len(uid) <= width else uid[: width - 1] + "…"


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    # Strip subsecond precision for display.
    return ts.replace("T", " ")[:19]


def _print_table(headers: list[str], rows: Iterable[list[str]]) -> None:
    """Minimal column-aligned table printer. stdlib-only to avoid
    pulling in tabulate / rich for a one-shot CLI."""
    materialized = list(rows)
    if not materialized:
        print("(none)")
        return
    widths = [len(h) for h in headers]
    for row in materialized:
        for i, cell in enumerate(row):
            if len(str(cell)) > widths[i]:
                widths[i] = len(str(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in materialized:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


# ── Subcommand: status ──────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    state = _load_engine_state(args.state_path)
    conn = _open_db(args.db)
    lifecycle = _lifecycle_store(conn)
    queue = _operator_command_store(conn)

    open_rows = lifecycle.get_open()
    pending = queue.count_pending()

    if state is None:
        print(f"engine state: ({args.state_path} not present — bot may not be running)")
        print(f"open lifecycle positions: {len(open_rows)}")
        print(f"operator queue pending:   {pending}")
        return 0

    print(f"bot timestamp:      {_fmt_ts(state.get('timestamp'))}")
    print(f"running:            {state.get('running')}")
    print(f"cycle count:        {state.get('cycle_count')}")
    print(f"regime:             {state.get('regime')}")
    print(f"equity:             ${state.get('equity', 0):,.2f}"
          if isinstance(state.get("equity"), (int, float)) else
          f"equity:             {state.get('equity')}")
    print(f"daily P&L:          ${state.get('daily_pnl', 0):,.2f}"
          if isinstance(state.get("daily_pnl"), (int, float)) else
          f"daily P&L:          {state.get('daily_pnl')}")
    risk = state.get("risk_controls") or {}
    halted = risk.get("is_halted", False)
    print(f"halt status:        {'HALTED' if halted else 'normal'}"
          + (f"  reason={risk.get('halt_reason')!r}" if halted else ""))
    # Phase B — soft pauses surfaced alongside halt. Fields may be
    # absent on pre-Phase-B snapshots; tolerate either shape.
    entries_paused = risk.get("entries_paused", False)
    print(f"entries paused:     {'YES' if entries_paused else 'no'}"
          + (f"  reason={risk.get('entries_paused_reason')!r}" if entries_paused else ""))
    paused_strats = risk.get("paused_strategies") or {}
    if paused_strats:
        print(f"paused strategies:  {len(paused_strats)}")
        for name in sorted(paused_strats):
            meta = paused_strats[name] or {}
            print(f"  - {name}: reason={meta.get('reason')!r}")
    else:
        print(f"paused strategies:  (none)")
    stream = state.get("stream_health") or {}
    if stream:
        print(f"stream healthy:     {stream.get('healthy')} (generation={stream.get('generation')})")
    print(f"open positions:     {len(state.get('open_positions') or [])} "
          f"(lifecycle table: {len(open_rows)})")
    print(f"operator queue:     {pending} pending command(s)")
    return 0


# ── Subcommand: positions ──────────────────────────────────────────


def cmd_positions(args: argparse.Namespace) -> int:
    conn = _open_db(args.db)
    lifecycle = _lifecycle_store(conn)
    state = _load_engine_state(args.state_path)

    # Join lifecycle rows with snapshot positions_detail by owner_key.
    # `positions_detail` is a dict keyed by the engine's owner_key
    # (ticker for single-leg, spread UUID for spreads). Values carry
    # qty / avg_entry_price / market_value / unrealized_pnl. An older
    # version of this code iterated positions_detail as if it were a
    # list — that crashed on live state because iterating a dict
    # yields keys (str), not entries.
    snapshot_by_owner: dict[str, dict] = {}
    if state:
        raw = state.get("positions_detail") or {}
        if isinstance(raw, dict):
            for owner, entry in raw.items():
                if isinstance(entry, dict):
                    snapshot_by_owner[str(owner)] = entry

    rows = []
    for row in lifecycle.get_open():
        snap = snapshot_by_owner.get(row.owner_key, {})
        qty = snap.get("qty") if snap else row.current_qty
        if qty is None:
            qty = row.current_qty
        market_value = snap.get("market_value")
        upnl = snap.get("unrealized_pnl")
        rows.append([
            _short(row.position_uid, 28),
            row.symbol,
            row.strategy,
            row.status,
            f"{qty}" if qty is not None else "-",
            f"${row.avg_entry_price:.2f}" if row.avg_entry_price else "-",
            f"${market_value:,.2f}" if isinstance(market_value, (int, float)) else "-",
            f"${upnl:+,.2f}" if isinstance(upnl, (int, float)) else "-",
            _fmt_ts(row.created_at),
        ])

    _print_table(
        ["POSITION_UID", "SYMBOL", "STRATEGY", "STATUS",
         "QTY", "AVG_ENTRY", "MARKET_VALUE", "UPNL", "OPENED_AT"],
        rows,
    )
    return 0


# ── Subcommand: show-position ──────────────────────────────────────


def cmd_show_position(args: argparse.Namespace) -> int:
    conn = _open_db(args.db)
    lifecycle = _lifecycle_store(conn)

    row = lifecycle.get_by_position_uid(args.position_uid)
    if row is None:
        sys.stderr.write(f"error: no lifecycle row for {args.position_uid}\n")
        return 1

    print(f"position_uid:       {row.position_uid}")
    print(f"status:             {row.status}")
    print(f"symbol:             {row.symbol}")
    print(f"owner_key:          {row.owner_key}")
    print(f"strategy:           {row.strategy}")
    print(f"position_type:      {row.position_type}")
    print(f"entry_qty:          {row.entry_qty}")
    print(f"current_qty:        {row.current_qty}")
    print(f"avg_entry_price:    {row.avg_entry_price}")
    print(f"net_realized_pnl:   {row.net_realized_pnl}")
    print(f"created_at:         {_fmt_ts(row.created_at)}")
    print(f"first_fill_at:      {_fmt_ts(row.first_fill_at)}")
    print(f"last_fill_at:       {_fmt_ts(row.last_fill_at)}")
    print(f"closed_at:          {_fmt_ts(row.closed_at)}")
    print(f"entry_order_id:     {row.entry_order_id or '-'}")
    print(f"entry_client_oid:   {row.entry_client_order_id or '-'}")
    if row.metadata:
        print(f"metadata:           {json.dumps(row.metadata)}")

    if row.legs:
        print()
        print(f"legs ({len(row.legs)}):")
        _print_table(
            ["SYMBOL", "SIDE", "QTY", "AVG_ENTRY"],
            [[leg.symbol, leg.side, str(leg.qty),
              f"${leg.avg_entry_price:.2f}" if leg.avg_entry_price else "-"]
             for leg in row.legs],
        )

    # Linked trades by position_uid.
    trades = conn.execute(
        "SELECT timestamp, side, qty, symbol, avg_fill_price, status, reason "
        "FROM trades WHERE position_uid = ? ORDER BY timestamp ASC",
        (row.position_uid,),
    ).fetchall()
    if trades:
        print()
        print(f"trades ({len(trades)}):")
        _print_table(
            ["TIME", "SIDE", "QTY", "SYMBOL", "AVG_FILL", "STATUS", "REASON"],
            [[_fmt_ts(t[0]), t[1], str(t[2]), t[3],
              f"${t[4]:.2f}" if t[4] is not None else "-",
              t[5] or "-", t[6] or "-"]
             for t in trades],
        )

    # Linked operator commands by target_position_uid (Phase C will
    # populate this; Phase A's `halt` does not target a position).
    cmds = conn.execute(
        "SELECT created_at, action, status, reason "
        "FROM operator_commands WHERE target_position_uid = ? "
        "ORDER BY created_at ASC",
        (row.position_uid,),
    ).fetchall()
    if cmds:
        print()
        print(f"operator commands ({len(cmds)}):")
        _print_table(
            ["TIME", "ACTION", "STATUS", "REASON"],
            [[_fmt_ts(c[0]), c[1], c[2], c[3]] for c in cmds],
        )

    return 0


# ── Subcommand: commands ───────────────────────────────────────────


def cmd_commands(args: argparse.Namespace) -> int:
    conn = _open_db(args.db)
    queue = _operator_command_store(conn)
    rows = queue.recent(limit=args.limit)
    _print_table(
        ["TIME", "ACTION", "STATUS", "BY", "COMMAND_UID", "REASON"],
        [[_fmt_ts(r.created_at), r.action, r.status,
          r.requested_by or "-",
          _short(r.command_uid, 36),
          r.reason]
         for r in rows],
    )
    return 0


# ── Subcommand: halt ───────────────────────────────────────────────


def cmd_halt(args: argparse.Namespace) -> int:
    if args.confirm != "halt":
        sys.stderr.write(
            "error: halt requires --confirm halt\n"
            "  this prevents an accidental keystroke from blocking the bot.\n"
        )
        return 2
    if not (args.reason and args.reason.strip()):
        sys.stderr.write("error: halt requires --reason \"<text>\"\n")
        return 2

    conn = _open_db(args.db)
    queue = _operator_command_store(conn)
    uid = new_command_uid()
    queue.insert(
        command_uid=uid,
        action="halt",
        reason=args.reason,
        requested_by=_requested_by(),
    )
    print(f"queued halt: {uid}")
    print(f"reason:      {args.reason}")
    print(
        f"engine heartbeat drains the queue every "
        f"~{settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS}s; "
        f"expiry after {settings.OPERATOR_COMMAND_EXPIRY_SECONDS}s if "
        "the bot is stalled."
    )
    return 0


# ── Subcommand: resume-after-halt ─────────────────────────────────


def cmd_resume_after_halt(args: argparse.Namespace) -> int:
    if args.confirm != "resume":
        sys.stderr.write(
            "error: resume-after-halt requires --confirm resume\n"
            "  resume runs a full broker reconciliation before lifting halt.\n"
        )
        return 2
    if not (args.reason and args.reason.strip()):
        sys.stderr.write("error: resume-after-halt requires --reason \"<text>\"\n")
        return 2

    conn = _open_db(args.db)
    queue = _operator_command_store(conn)
    uid = new_command_uid()
    queue.insert(
        command_uid=uid,
        action="resume-after-halt",
        reason=args.reason,
        requested_by=_requested_by(),
    )
    print(f"queued resume-after-halt: {uid}")
    print(f"reason:                   {args.reason}")
    print(
        "engine will re-reconcile against the broker before clearing the halt. "
        "if reconciliation does not yield NORMAL mode the command will be "
        "rejected — use `operator.py commands` to confirm."
    )
    return 0


# ── Subcommand: pause-entries / resume-entries (Phase B) ─────────


def _enqueue_simple_action(args, *, action: str, expected_confirm: str) -> int:
    """Shared helper for the four soft-control subcommands.

    Each requires `--reason "<text>"` and `--confirm <expected>` —
    mirroring the halt pattern so a typo can't fire a state change.
    """
    if args.confirm != expected_confirm:
        sys.stderr.write(
            f"error: {action} requires --confirm {expected_confirm}\n"
            "  this prevents an accidental keystroke from changing state.\n"
        )
        return 2
    if not (args.reason and args.reason.strip()):
        sys.stderr.write(f"error: {action} requires --reason \"<text>\"\n")
        return 2

    conn = _open_db(args.db)
    queue = _operator_command_store(conn)
    uid = new_command_uid()
    queue.insert(
        command_uid=uid,
        action=action,
        reason=args.reason,
        requested_by=_requested_by(),
        target_strategy=getattr(args, "strategy", None),
    )
    print(f"queued {action}: {uid}")
    print(f"reason:      {args.reason}")
    target_strategy = getattr(args, "strategy", None)
    if target_strategy:
        print(f"strategy:    {target_strategy}")
    print(
        f"engine heartbeat drains the queue every "
        f"~{settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS}s."
    )
    return 0


def cmd_pause_entries(args: argparse.Namespace) -> int:
    return _enqueue_simple_action(args, action="pause-entries", expected_confirm="pause")


def cmd_resume_entries(args: argparse.Namespace) -> int:
    return _enqueue_simple_action(args, action="resume-entries", expected_confirm="resume")


def cmd_pause_strategy(args: argparse.Namespace) -> int:
    if not (args.strategy and args.strategy.strip()):
        sys.stderr.write("error: pause-strategy requires --strategy <name>\n")
        return 2
    return _enqueue_simple_action(args, action="pause-strategy", expected_confirm="pause")


def cmd_resume_strategy(args: argparse.Namespace) -> int:
    if not (args.strategy and args.strategy.strip()):
        sys.stderr.write("error: resume-strategy requires --strategy <name>\n")
        return 2
    return _enqueue_simple_action(args, action="resume-strategy", expected_confirm="resume")


# ── Subcommand: destructive position controls (Phase C) ─────────


def _enqueue_destructive_action(
    args, *, action: str, params: dict | None = None,
) -> int:
    """Shared helper for the 3 Phase C destructive subcommands.

    Per proposal §6, destructive commands require the operator to type
    the FIRST 10 HEX CHARS of the position_uid as the --confirm token.
    A generic "yes" / "close" string would be too easy to fire by
    accident on the wrong position; matching the uid's short form
    forces the operator to actually look at the target before
    confirming.
    """
    uid = args.position_uid or ""
    if not uid.startswith("pos_"):
        sys.stderr.write(
            f"error: {action} requires position_uid in the 'pos_<hex>' form\n"
        )
        return 2
    if not (args.reason and args.reason.strip()):
        sys.stderr.write(f"error: {action} requires --reason \"<text>\"\n")
        return 2
    expected_token = uid.removeprefix("pos_")[:10]
    if args.confirm != expected_token:
        sys.stderr.write(
            f"error: {action} requires --confirm {expected_token}\n"
            "  type the first 10 hex chars of the position_uid to confirm.\n"
            "  this prevents firing against the wrong position by accident.\n"
        )
        return 2

    conn = _open_db(args.db)
    queue = _operator_command_store(conn)
    cmd_uid = new_command_uid()
    queue.insert(
        command_uid=cmd_uid,
        action=action,
        reason=args.reason,
        requested_by=_requested_by(),
        target_position_uid=uid,
        params=params,
    )
    print(f"queued {action}: {cmd_uid}")
    print(f"target:      {uid}")
    print(f"reason:      {args.reason}")
    if params:
        print(f"params:      {params}")
    print(
        f"engine heartbeat drains the queue every "
        f"~{settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS}s; use "
        f"`operator.py commands` to see the result."
    )
    return 0


def cmd_close_position(args: argparse.Namespace) -> int:
    return _enqueue_destructive_action(args, action="close-position")


def cmd_reduce_position(args: argparse.Namespace) -> int:
    try:
        pct = float(args.pct)
    except (TypeError, ValueError):
        sys.stderr.write(f"error: --pct must be numeric; got {args.pct!r}\n")
        return 2
    if not (0 < pct < 100):
        sys.stderr.write(
            f"error: --pct must be in (0, 100); got {pct}\n"
        )
        return 2
    return _enqueue_destructive_action(
        args, action="reduce-position", params={"pct": pct},
    )


def cmd_cancel_position_orders(args: argparse.Namespace) -> int:
    return _enqueue_destructive_action(args, action="cancel-position-orders")


# ── argparse wiring ────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operator.py",
        description="Operator control CLI for the trading bot (Phase A).",
    )
    parser.add_argument(
        "--db",
        default=settings.TRADE_LOG_DB,
        help=f"Path to the trade DB (default: {settings.TRADE_LOG_DB})",
    )
    parser.add_argument(
        "--state-path",
        default=settings.STATE_SNAPSHOT_PATH,
        help=(
            f"Path to engine_state.json (default: {settings.STATE_SNAPSHOT_PATH})"
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("status", help="show running state, cycle, halt, positions count")

    sub.add_parser("positions", help="list open lifecycle positions")

    sp_show = sub.add_parser("show-position", help="show full lifecycle metadata")
    sp_show.add_argument("position_uid", help="full pos_<hex> identifier")

    sp_cmds = sub.add_parser("commands", help="recent operator commands audit")
    sp_cmds.add_argument(
        "--limit", type=int, default=20, help="max rows to show (default: 20)"
    )

    sp_halt = sub.add_parser("halt", help="engage sticky kill switch")
    sp_halt.add_argument("--reason", required=True, help="why are you halting?")
    sp_halt.add_argument(
        "--confirm", default="",
        help="must be literally `halt` to confirm — prevents a typo from halting the bot",
    )

    sp_resume = sub.add_parser(
        "resume-after-halt", help="lift sticky kill switch (engine re-reconciles first)"
    )
    sp_resume.add_argument("--reason", required=True, help="why are you resuming?")
    sp_resume.add_argument(
        "--confirm", default="",
        help="must be literally `resume` to confirm — prevents an accidental resume",
    )

    # Phase B — soft entry pauses.
    sp_pe = sub.add_parser(
        "pause-entries",
        help="block new entries (exits/stops continue)",
    )
    sp_pe.add_argument("--reason", required=True, help="why are you pausing entries?")
    sp_pe.add_argument(
        "--confirm", default="",
        help="must be literally `pause` to confirm",
    )

    sp_re = sub.add_parser(
        "resume-entries",
        help="unblock new entries (no reconciliation required)",
    )
    sp_re.add_argument("--reason", required=True, help="why are you resuming entries?")
    sp_re.add_argument(
        "--confirm", default="",
        help="must be literally `resume` to confirm",
    )

    sp_ps = sub.add_parser(
        "pause-strategy",
        help="block new entries for one strategy only",
    )
    sp_ps.add_argument("--strategy", required=True, help="strategy name (e.g. sma_crossover)")
    sp_ps.add_argument("--reason", required=True, help="why are you pausing the strategy?")
    sp_ps.add_argument(
        "--confirm", default="",
        help="must be literally `pause` to confirm",
    )

    sp_rs = sub.add_parser(
        "resume-strategy",
        help="unblock new entries for one strategy",
    )
    sp_rs.add_argument("--strategy", required=True, help="strategy name (e.g. sma_crossover)")
    sp_rs.add_argument("--reason", required=True, help="why are you resuming the strategy?")
    sp_rs.add_argument(
        "--confirm", default="",
        help="must be literally `resume` to confirm",
    )

    # Phase C — destructive position controls. All three accept a
    # position_uid argument and require --confirm <first-10-hex> of
    # that uid, so a typo can't fire against the wrong position.
    sp_cp = sub.add_parser(
        "close-position",
        help="fully close one lifecycle by position_uid",
    )
    sp_cp.add_argument("position_uid", help="full pos_<hex> identifier")
    sp_cp.add_argument("--reason", required=True, help="why are you closing?")
    sp_cp.add_argument(
        "--confirm", default="",
        help="must equal the first 10 hex chars of position_uid",
    )

    sp_rp = sub.add_parser(
        "reduce-position",
        help="partially close one lifecycle (--pct)",
    )
    sp_rp.add_argument("position_uid", help="full pos_<hex> identifier")
    sp_rp.add_argument(
        "--pct", required=True, type=float,
        help="percentage of current position qty to reduce (0 < pct < 100)",
    )
    sp_rp.add_argument("--reason", required=True, help="why are you reducing?")
    sp_rp.add_argument(
        "--confirm", default="",
        help="must equal the first 10 hex chars of position_uid",
    )

    sp_co = sub.add_parser(
        "cancel-position-orders",
        help="cancel non-terminal stop/exit orders for one position",
    )
    sp_co.add_argument("position_uid", help="full pos_<hex> identifier")
    sp_co.add_argument("--reason", required=True, help="why are you canceling?")
    sp_co.add_argument(
        "--confirm", default="",
        help="must equal the first 10 hex chars of position_uid",
    )

    return parser


_DISPATCH = {
    "status": cmd_status,
    "positions": cmd_positions,
    "show-position": cmd_show_position,
    "commands": cmd_commands,
    "halt": cmd_halt,
    "resume-after-halt": cmd_resume_after_halt,
    "pause-entries": cmd_pause_entries,
    "resume-entries": cmd_resume_entries,
    "pause-strategy": cmd_pause_strategy,
    "resume-strategy": cmd_resume_strategy,
    "close-position": cmd_close_position,
    "reduce-position": cmd_reduce_position,
    "cancel-position-orders": cmd_cancel_position_orders,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH[args.action]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
