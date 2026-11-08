"""Standalone earnings ledger CLI. Run --help; commands never touch the bot."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger
from pydantic import ValidationError

from config import settings
from data.earnings_observation import (
    Citation,
    Contamination,
    Facts,
    Label,
    Schedule,
    load_registry,
)
from reporting.earnings_observation_reader import read_earnings
from reporting.earnings_observations import EarningsStore


def parser() -> argparse.ArgumentParser:
    """Build explicit operator commands; JSON inputs carry typed business data."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=settings.EARNINGS_OBSERVATION_DB_PATH)
    p.add_argument("--registry", default=settings.EARNINGS_REGISTRY_PATH)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the dedicated ledger")
    cycle = sub.add_parser("cycle", help="freeze a named reporting round")
    cycle.add_argument("cycle_id")
    cycle.add_argument("--prospective-cutoff", required=True)
    cycle.add_argument("--note", required=True)
    source = sub.add_parser(
        "source", help="record a public citation or explicit unavailability"
    )
    source.add_argument("--json", required=True)
    register = sub.add_parser(
        "register", help="register one event and its expected reaction jobs"
    )
    register.add_argument("--cycle", required=True)
    register.add_argument("--issuer", required=True)
    register.add_argument("--period", required=True)
    register.add_argument("--json", required=True, help="schedule JSON")
    register.add_argument("--retrospective", action="store_true")
    for command in ("schedule", "facts", "label", "flag"):
        command_parser = sub.add_parser(
            command, help=f"append cited {command} knowledge"
        )
        command_parser.add_argument("event_id")
        command_parser.add_argument("--json", required=True)
        command_parser.add_argument("--reason")
    sub.add_parser(
        "collect", help="one bounded pass; requires EARNINGS_OBSERVATIONS_ENABLED=true"
    )
    recollect = sub.add_parser(
        "recollect", help="request corrected marks; retains previous revisions"
    )
    recollect.add_argument("event_id")
    recollect.add_argument("--reason", required=True)
    sec = sub.add_parser(
        "sec-source", help="record metadata for one exact recent SEC accession"
    )
    sec.add_argument("--issuer", required=True)
    sec.add_argument("--cik", required=True)
    sec.add_argument("--accession", required=True)
    sec.add_argument("--paraphrase", required=True)
    export = sub.add_parser("export", help="read-only JSON projection to stdout")
    views = export.add_mutually_exclusive_group(required=True)
    views.add_argument("--as-of", help="UTC-aware historical cutoff")
    views.add_argument(
        "--corrected", action="store_true", help="latest known revisions now"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Execute one operation; safe error summaries omit raw provider responses."""
    args = parser().parse_args(argv)
    try:
        if args.command == "export":
            cutoff = (
                datetime.now(timezone.utc)
                if args.corrected
                else datetime.fromisoformat(args.as_of)
            )
            result = read_earnings(args.db, cutoff=cutoff, corrected=args.corrected)
        else:
            if (
                args.command in {"collect", "sec-source"}
                and not settings.EARNINGS_OBSERVATIONS_ENABLED
            ):
                raise ValueError(
                    "collection disabled; set EARNINGS_OBSERVATIONS_ENABLED=true"
                )
            store = EarningsStore(args.db)
            payload = (
                json.loads(Path(args.json).read_text())
                if hasattr(args, "json")
                else None
            )
            if args.command == "init":
                result = {"state": "initialized"}
            elif args.command == "cycle":
                result = {
                    "cycle_id": store.add_cycle(
                        args.cycle_id,
                        prospective_cutoff=datetime.fromisoformat(
                            args.prospective_cutoff
                        ),
                        note=args.note,
                    )
                }
            elif args.command == "source":
                result = {
                    "source_id": store.add_source(Citation.model_validate(payload))
                }
            elif args.command == "register":
                result = {
                    "event_id": store.register(
                        load_registry(args.registry),
                        args.cycle,
                        args.issuer,
                        args.period,
                        Schedule.model_validate(payload),
                        retrospective=args.retrospective,
                    )
                }
            elif args.command in {"schedule", "facts", "label", "flag"}:
                model = {
                    "schedule": Schedule,
                    "facts": Facts,
                    "label": Label,
                    "flag": Contamination,
                }[args.command]
                result = {
                    "revision_id": store.revise(
                        args.event_id, model.model_validate(payload), reason=args.reason
                    )
                }
            elif args.command == "collect":
                from data.earnings_provider import EarningsCalendar, EarningsSIPProvider
                from reporting.earnings_collector import collect_once

                result = collect_once(store, EarningsCalendar(), EarningsSIPProvider())
            elif args.command == "recollect":
                result = {"plan_id": store.recollect(args.event_id, reason=args.reason)}
            else:
                from data.earnings_provider import SECMetadataProvider

                issuer = load_registry(args.registry).issuer(args.issuer)
                source = SECMetadataProvider(settings.EARNINGS_SEC_USER_AGENT).citation(
                    cik=args.cik,
                    accession=args.accession,
                    issuer_id=issuer.issuer_id,
                    symbol=issuer.symbol,
                    now=store.now(),
                    paraphrase=args.paraphrase,
                )
                result = {"source_id": store.add_source(source)}
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except ValidationError as exc:
        fields = [
            ".".join(str(part) for part in error["loc"]) + ":" + error["type"]
            for error in exc.errors(include_input=False, include_url=False)
        ]
        logger.error("earnings input validation failed: {}", ", ".join(fields))
        return 2
    except Exception as exc:
        # Pydantic error bodies may echo operator inputs: don't print them.
        if type(exc) is ValueError:
            logger.error("earnings command rejected: {}", exc)
        else:
            logger.error(
                "earnings command failed ({}); check input schema and configuration",
                type(exc).__name__,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
