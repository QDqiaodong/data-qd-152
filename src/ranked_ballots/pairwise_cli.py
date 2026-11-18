"""Command line interface for the independent pairwise preference analysis.

``ranked-ballots-pairwise --source JSON --output JSON`` reads the strict v1
``{candidates, ballots}`` contract, accumulates the complete pairwise matrix
and atomically publishes one deterministic JSON object.  Any input,
validation or computation failure exits 2 with a reason on stderr and leaves
any existing output bytes untouched; the output path must differ from the
input and must not be a symlink.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import read, save
from .pairwise import pairwise_analysis

DESCRIPTION = "Independent pairwise preference analysis over ranked ballot groups"


def run(source):
    data, _digest = read(source)
    return pairwise_analysis(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.source.resolve() == args.output.resolve():
            raise ValueError("input and output must differ")
        report = run(args.source)
        save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
