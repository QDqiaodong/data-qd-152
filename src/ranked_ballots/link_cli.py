"""Command line interface for the scan/v2-ledger capacity reconciliation.

``ranked-ballots-link --report V2_JSON --scans JSON --output JSON`` validates
both inputs, derives every compatibility relation, runs one global b-matching
check and atomically publishes one deterministic JSON payload.  Explainable
failures exit 2 with a reason on stderr and leave any existing output untouched;
a legal-but-unmatchable input is a success and is reported as ``infeasible``
with an independently checkable Hall witness.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import save
from .link import LinkError, link_records

DESCRIPTION = "Reconcile partial scan transcriptions against one fixed v2 ballot ledger"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--report", required=True, help="fixed v2 report JSON")
    parser.add_argument("--scans", required=True, help="scan transcription JSON")
    parser.add_argument("--output", required=True, help="reconciliation result JSON")
    args = parser.parse_args(argv)
    try:
        output = Path(args.output)
        if Path(args.report).resolve() == output.resolve() \
                or Path(args.scans).resolve() == output.resolve():
            raise LinkError("input and output paths must differ")
        payload = link_records(args.report, args.scans)
        save(args.output, payload)
    except (LinkError, ValueError, TypeError, KeyError, OSError,
            OverflowError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
