"""Command line interface for first-choice suppression planning.

``ranked-ballots-disclosure --source JSON --output JSON`` reads the strict
``{precincts, protect, optional}`` contract, chooses the minimal additional
suppression plan, and atomically publishes one deterministic public summary
report.  Explainable failures (including an unsatisfiable plan) exit 2 with a
reason on stderr, never reveal a cell value, and leave any existing output
bytes untouched; the output path must differ from the input and must not be a
symlink.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .disclosure import disclosure_report

DESCRIPTION = "Suppression planning for a first-choice public summary table"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = disclosure_report(args.source, args.output)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False,
                     allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
