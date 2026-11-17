"""Subprocess worker for archive crash/barrier tests.

This is a test-only entry point. Coordination happens through environment
variables consumed by archive._test_point; production commands take no
timing parameters.

Usage:
    _archive_worker.py crash-export  STORE EXPORT_ID POINT
    _archive_worker.py barrier-export STORE EXPORT_ID POINT BARRIER_DIR
    _archive_worker.py apply         STORE SOURCE
    _archive_worker.py export        STORE EXPORT_ID
    _archive_worker.py recover       STORE
    _archive_worker.py status        STORE
"""
import json
import os
import sys

sys.path.insert(0, os.environ["RB_PYTHONPATH"])

from ranked_ballots import archive  # noqa: E402


def main(argv):
    command = argv[1]
    try:
        if command in ("crash-export", "barrier-export"):
            store, export_id, point = argv[2], argv[3], argv[4]
            if command == "crash-export":
                os.environ[archive._CRASH_ENV] = point
            else:
                os.environ[archive._BARRIER_ENV] = argv[5]
            payload = archive.export_report(store, export_id)
        elif command == "apply":
            payload = archive.apply_batch(argv[2], argv[3])
        elif command == "export":
            payload = archive.export_report(argv[2], argv[3])
        elif command == "recover":
            payload = archive.recover(argv[2])
        elif command == "status":
            payload = archive.store_status(argv[2])
        else:  # pragma: no cover - test harness error
            print("unknown worker command: " + command, file=sys.stderr)
            return 2
    except archive.ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
