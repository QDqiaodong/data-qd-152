"""Command line interface for the synthetic paper-ballot audit activity.

Subcommands: start, draw, record, status.  Every successful invocation prints
one JSON status object on stdout; any explainable failure exits with code 2
and a clear reason on stderr, leaving the activity file untouched.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import audit

DESCRIPTION = "Synthetic paper-ballot audit against one fixed v2 count snapshot"


def _print(payload) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start", help="create a new audit activity")
    start_p.add_argument("--report", required=True)
    start_p.add_argument("--manifest", required=True)
    start_p.add_argument("--seed", required=True)
    start_p.add_argument("--risk", required=True)
    start_p.add_argument("--max-draws", required=True)
    start_p.add_argument("--session", required=True)

    draw_p = sub.add_parser("draw", help="issue K more draws from the fixed stream")
    draw_p.add_argument("--session", required=True)
    draw_p.add_argument("--count", required=True)

    record_p = sub.add_parser("record", help="record hand-read observations")
    record_p.add_argument("--session", required=True)
    record_p.add_argument("--source", required=True)

    status_p = sub.add_parser("status", help="show the current audit status")
    status_p.add_argument("--session", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            try:
                max_draws = int(args.max_draws)
            except ValueError as error:
                raise audit.AuditError("--max-draws must be an integer") from error
            payload = audit.start(
                args.report, args.manifest, args.seed, args.risk,
                max_draws, args.session,
            )
        elif args.command == "draw":
            try:
                count = int(args.count)
            except ValueError as error:
                raise audit.AuditError("--count must be an integer") from error
            payload = audit.draw(args.session, count)
        elif args.command == "record":
            payload = audit.record(args.session, args.source)
        else:  # status
            payload = audit.status(args.session)
    except audit.AuditError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    _print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
