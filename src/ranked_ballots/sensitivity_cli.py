"""Command line interface for candidate-withdrawal sensitivity analysis.

``ranked-ballots-sensitivity --source JSON --output JSON`` reads the strict v1
``{candidates, ballots}`` contract, computes the existing ``analyze`` baseline
and one counterfactual scenario per candidate, then atomically publishes one
deterministic canonical JSON object.  Any read, validation, scenario, count or
write failure exits 2 with a reason on stderr and leaves any existing output
bytes untouched; the output path must differ from the input and must not be a
symlink.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .sensitivity import sensitivity_report

DESCRIPTION = "Candidate-withdrawal sensitivity analysis over ranked ballot groups"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        # Stream publication keeps peak memory bounded at the maximum input
        # size; stdout then mirrors the exact bytes written to the output file.
        sensitivity_report(args.source, args.output, return_report=False)
        with open(args.output, "rb") as stream:
            shutil.copyfileobj(stream, sys.stdout.buffer)
        sys.stdout.buffer.flush()
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
