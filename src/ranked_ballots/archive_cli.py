"""Command line interface for the durable ranked-ballot archive.

Subcommands: init, apply, export, status, recover. Every successful invocation
prints one JSON object on stdout; any explainable failure exits with code 2
and a clear reason on stderr.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import archive

DESCRIPTION = "Durable ranked-ballot archive with immutable counting snapshots"


def _print(payload) -> None:
    print(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="create a new archive store")
    init_p.add_argument("--store", required=True)
    init_p.add_argument("--source", required=True)

    apply_p = sub.add_parser("apply", help="apply one correction batch")
    apply_p.add_argument("--store", required=True)
    apply_p.add_argument("--source", required=True)

    export_p = sub.add_parser("export", help="register/complete an immutable report")
    export_p.add_argument("--store", required=True)
    export_p.add_argument("--export-id", required=True)

    status_p = sub.add_parser("status", help="show head, published pointer and tasks")
    status_p.add_argument("--store", required=True)

    recover_p = sub.add_parser("recover", help="finish all registered pending exports")
    recover_p.add_argument("--store", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            payload = archive.init_store(args.store, args.source)
        elif args.command == "apply":
            payload = archive.apply_batch(args.store, args.source)
        elif args.command == "export":
            payload = archive.export_report(args.store, args.export_id)
        elif args.command == "status":
            payload = archive.store_status(args.store)
        else:  # recover
            payload = archive.recover(args.store)
    except archive.ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    _print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
