"""Durable ranked-ballot archive with immutable counting snapshots.

Persistence layout (one election per store directory, local filesystem)::

    <store>/
        archive.sqlite3        # SQLite WAL database (ledger, batches, snapshots, tasks)
        archive.sqlite3-wal/-shm
        reports/
            revision-<r>.json  # immutable, portable v2 reports
            .revision-<r>.*.tmp  # crash residue (never referenced, safe to leave)

Concurrency model: SQLite in WAL mode with explicit ``BEGIN IMMEDIATE`` write
transactions. Long running work (IRV counting, report encoding, file writes)
happens *outside* write transactions so ``apply`` is never blocked by an
export. Snapshot capture and task registration commit together, which is the
only point at which an export id can become visible.

Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .common import ident, number, rows, read as read_strict_json
from .model import analyze

DB_NAME = "archive.sqlite3"
REPORTS_DIR = "reports"
SCHEMA_VERSION = 2
MAX_CANDIDATES = 100
MAX_BALLOTS = 20000
MAX_CHANGES = 20000
MAX_WEIGHT = 10**9
BUSY_TIMEOUT_MS = 30000

# Test-only coordination. Never set in normal operation:
#   RB_ARCHIVE_BARRIER       directory holding "<point>.wait" flags
#   RB_ARCHIVE_CRASH_POINT   point name at which the process SIGKILLs itself
_BARRIER_ENV = "RB_ARCHIVE_BARRIER"
_CRASH_ENV = "RB_ARCHIVE_CRASH_POINT"


class ArchiveError(Exception):
    """Expected, explainable archive failure. The CLI maps this to exit 2."""


# --------------------------------------------------------------------------- #
# Canonical encoding and small helpers
# --------------------------------------------------------------------------- #

def canonical_bytes(obj) -> bytes:
    """Canonical JSON: UTF-8, ensure_ascii=False, sort_keys, no whitespace, LF."""
    text = json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return (text + "\n").encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _test_point(name: str) -> None:
    """Test barrier / crash injection hook (production runs are no-ops).

    A "<name>.wait" flag pauses every process that reaches the point; each
    process drops a numbered "<name>.arrived-<pid>" marker so the test can
    wait for an exact number of processes without sleeping.
    """
    barrier_dir = os.environ.get(_BARRIER_ENV)
    if barrier_dir:
        base = Path(barrier_dir)
        flag = base / (name + ".wait")
        if flag.exists():
            (base / f"{name}.arrived-{os.getpid()}").touch()
            (base / (name + ".ready")).touch()
            deadline = time.time() + 60.0
            while flag.exists():
                if time.time() > deadline:
                    raise ArchiveError("test barrier timeout at " + name)
                time.sleep(0.05)
    if os.environ.get(_CRASH_ENV) == name:
        os.kill(os.getpid(), 9)


def _report_relpath(revision: int) -> str:
    return f"{REPORTS_DIR}/revision-{revision}.json"


def _read_strict(source):
    """Read up to 8 MB of strict JSON; return (raw bytes, parsed object).

    Mirrors common.read: duplicate keys and non-finite constants are rejected.
    The raw bytes are needed for the request-level SHA256 idempotency key.
    """
    with Path(source).open("rb") as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ArchiveError("JSON exceeds 8 MB")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ArchiveError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def invalid(value):
        raise ArchiveError("non-finite JSON number: " + value)

    return raw, json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def _rollback_safe(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------- #
# Connection and schema
# --------------------------------------------------------------------------- #

def _connect(store) -> sqlite3.Connection:
    db = Path(store) / DB_NAME
    if not db.exists():
        raise ArchiveError(f"not an archive store (missing {DB_NAME}): {store}")
    if db.is_symlink():
        raise ArchiveError("database path must not be a symlink: " + str(db))
    conn = sqlite3.connect(str(db), timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=" + str(BUSY_TIMEOUT_MS))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_SCHEMA = """
CREATE TABLE meta(
    id INTEGER PRIMARY KEY CHECK(id = 1),
    head_revision INTEGER NOT NULL,
    published_revision INTEGER
);
CREATE TABLE candidates(
    position INTEGER NOT NULL PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE
);
CREATE TABLE ballots(
    ballot_id TEXT NOT NULL PRIMARY KEY,
    ranking TEXT NOT NULL,
    weight INTEGER NOT NULL
);
CREATE TABLE batches(
    batch_id TEXT NOT NULL PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    request_bytes BLOB NOT NULL,
    parent_revision INTEGER NOT NULL,
    revision INTEGER NOT NULL UNIQUE
);
CREATE TABLE snapshots(
    revision INTEGER NOT NULL PRIMARY KEY,
    ledger_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL
);
CREATE TABLE exports(
    export_id TEXT NOT NULL PRIMARY KEY,
    revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','ready'))
);
CREATE TABLE report_artifacts(
    revision INTEGER NOT NULL PRIMARY KEY,
    report_sha256 TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #

def init_store(store, source) -> dict:
    """Create a brand new archive. The candidate list is fixed forever after."""
    store_path = Path(store)
    if store_path.exists() and not store_path.is_dir():
        raise ArchiveError("store path exists and is not a directory: " + str(store_path))
    db_path = store_path / DB_NAME
    if db_path.exists() or db_path.is_symlink():
        raise ArchiveError("archive already exists, init refuses to overwrite: " + str(db_path))
    try:
        data, _ = read_strict_json(source)
        # Reuse the exact v1 input contract (limits, identifiers, unknown fields).
        analyze(data)
        candidate_list = sorted(ident(c) for c in data["candidates"])
        ballot_records = []
        for ballot in data["ballots"]:
            ballot_records.append(
                (
                    ident(ballot["id"]),
                    [ident(c) for c in ballot["ranking"]],
                    number(ballot["weight"], 1, MAX_WEIGHT),
                )
            )
        ballot_records.sort(key=lambda item: item[0])

        store_path.mkdir(parents=True, exist_ok=True)
        (store_path / REPORTS_DIR).mkdir(exist_ok=True)
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            # PRAGMA journal_mode commits on its own; finish pragmas before the txn.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            # Individual execute() calls: executescript() would auto-commit.
            for statement in (s.strip() for s in _SCHEMA.split(";")):
                if statement:
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(id, head_revision, published_revision) VALUES(1, 0, NULL)"
            )
            conn.executemany(
                "INSERT INTO candidates(position, candidate_id) VALUES(?, ?)",
                list(enumerate(candidate_list)),
            )
            conn.executemany(
                "INSERT INTO ballots(ballot_id, ranking, weight) VALUES(?, ?, ?)",
                [
                    (bid, json.dumps(ranking, ensure_ascii=False), weight)
                    for bid, ranking, weight in ballot_records
                ],
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback_safe(conn)
            conn.close()
            raise
        conn.close()
        _fsync_dir(store_path)
    except ArchiveError:
        raise
    except (ValueError, OSError, sqlite3.Error) as error:
        raise ArchiveError("init failed: " + str(error)) from error
    return {
        "store": str(store_path),
        "head_revision": 0,
        "published": None,
        "candidate_count": len(candidate_list),
        "ballot_count": len(ballot_records),
    }


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def _validate_changes(obj):
    if not isinstance(obj, dict) or set(obj) != {"batch_id", "expected_revision", "changes"}:
        raise ArchiveError(
            "apply source must have exactly the fields batch_id, expected_revision, changes"
        )
    try:
        batch_id = ident(obj["batch_id"])
        expected = obj["expected_revision"]
        if type(expected) is not int or expected < 0:
            raise ArchiveError(
                "expected_revision must be a non-negative integer (booleans are rejected)"
            )
        change_list = rows(obj["changes"], MAX_CHANGES)
        if not change_list:
            raise ArchiveError("changes must contain between 1 and 20000 items")
        normalized = []
        seen_ids = set()
        for change in change_list:
            if not isinstance(change, dict):
                raise ArchiveError("each change must be an object")
            op = change.get("op")
            if op == "put":
                if set(change) != {"op", "id", "ranking", "weight"}:
                    raise ArchiveError("put change must have exactly op, id, ranking, weight")
                bid = ident(change["id"])
                ranking = [ident(c) for c in rows(change["ranking"], MAX_CANDIDATES)]
                if len(set(ranking)) != len(ranking):
                    raise ArchiveError("duplicate candidate in ranking of ballot " + bid)
                weight = number(change["weight"], 1, MAX_WEIGHT)
                normalized.append(("put", bid, ranking, weight))
            elif op == "delete":
                if set(change) != {"op", "id"}:
                    raise ArchiveError("delete change must have exactly op, id")
                bid = ident(change["id"])
                normalized.append(("delete", bid))
            else:
                raise ArchiveError("unknown change op: " + repr(op))
            if bid in seen_ids:
                raise ArchiveError("ballot id appears more than once in one batch: " + bid)
            seen_ids.add(bid)
    except ArchiveError:
        raise
    except (ValueError, TypeError, KeyError) as error:
        raise ArchiveError("invalid apply source: " + str(error)) from error
    return batch_id, expected, normalized


def _load_working_set(conn):
    work = {}
    for bid, ranking_json, weight in conn.execute(
        "SELECT ballot_id, ranking, weight FROM ballots"
    ):
        work[bid] = (json.loads(ranking_json), weight)
    return work


def _simulate_changes(conn, normalized):
    candidate_set = {
        row[0] for row in conn.execute("SELECT candidate_id FROM candidates")
    }
    work = _load_working_set(conn)
    for item in normalized:
        if item[0] == "put":
            _, bid, ranking, weight = item
            for candidate in ranking:
                if candidate not in candidate_set:
                    raise ArchiveError(
                        "ranking references candidate not on the fixed candidate list: " + candidate
                    )
            work[bid] = (ranking, weight)
        else:
            _, bid = item
            if bid not in work:
                raise ArchiveError("delete references a ballot id that does not exist: " + bid)
            del work[bid]
    if len(work) > MAX_BALLOTS:
        raise ArchiveError("ballot limit of 20000 would be exceeded by this batch")
    return work


def _replay_row(conn, batch_id):
    return conn.execute(
        "SELECT batch_id, request_sha256, parent_revision, revision "
        "FROM batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()


def apply_batch(store, source) -> dict:
    """Atomically apply one correction batch; replays return the original receipt."""
    try:
        raw, obj = _read_strict(source)
        request_sha = _sha256_bytes(raw)
        batch_id, expected_revision, normalized = _validate_changes(obj)

        conn = _connect(store)
        try:
            # Replay / idempotency check precedes the baseline (expected_revision) check.
            row = _replay_row(conn, batch_id)
            if row is not None:
                if row[1] != request_sha:
                    raise ArchiveError(
                        "batch_id " + batch_id + " was already used with different request bytes"
                    )
                return {
                    "batch_id": row[0],
                    "request_sha256": row[1],
                    "parent_revision": row[2],
                    "revision": row[3],
                }

            try:
                conn.execute("BEGIN IMMEDIATE")
                # Re-check inside the write lock to cover racing processes.
                row = _replay_row(conn, batch_id)
                if row is not None:
                    conn.execute("ROLLBACK")
                    if row[1] != request_sha:
                        raise ArchiveError(
                            "batch_id " + batch_id
                            + " was already used with different request bytes"
                        )
                    return {
                        "batch_id": row[0],
                        "request_sha256": row[1],
                        "parent_revision": row[2],
                        "revision": row[3],
                    }
                current_head = conn.execute(
                    "SELECT head_revision FROM meta WHERE id = 1"
                ).fetchone()[0]
                if expected_revision != current_head:
                    conn.execute("ROLLBACK")
                    raise ArchiveError(
                        f"expected_revision {expected_revision} is stale; "
                        f"current head_revision is {current_head}"
                    )
                working_set = _simulate_changes(conn, normalized)
                _test_point("apply_before_commit")
                conn.execute("DELETE FROM ballots")
                conn.executemany(
                    "INSERT INTO ballots(ballot_id, ranking, weight) VALUES(?, ?, ?)",
                    [
                        (bid, json.dumps(ranking, ensure_ascii=False), weight)
                        for bid, (ranking, weight) in working_set.items()
                    ],
                )
                new_revision = current_head + 1
                conn.execute(
                    "INSERT INTO batches(batch_id, request_sha256, request_bytes, "
                    "parent_revision, revision) VALUES(?, ?, ?, ?, ?)",
                    (batch_id, request_sha, raw, current_head, new_revision),
                )
                conn.execute(
                    "UPDATE meta SET head_revision = ? WHERE id = 1", (new_revision,)
                )
                conn.execute("COMMIT")
            except BaseException:
                _rollback_safe(conn)
                raise
            return {
                "batch_id": batch_id,
                "request_sha256": request_sha,
                "parent_revision": current_head,
                "revision": new_revision,
            }
        finally:
            conn.close()
    except ArchiveError:
        raise
    except (ValueError, OSError, sqlite3.Error) as error:
        raise ArchiveError("apply failed: " + str(error)) from error


# --------------------------------------------------------------------------- #
# Snapshot capture and report installation
# --------------------------------------------------------------------------- #

def _build_ledger(conn) -> dict:
    candidates = [
        row[0]
        for row in conn.execute(
            "SELECT candidate_id FROM candidates ORDER BY position"
        )
    ]
    ballots = []
    for bid, ranking_json, weight in conn.execute(
        "SELECT ballot_id, ranking, weight FROM ballots ORDER BY ballot_id"
    ):
        ballots.append(
            {"id": bid, "ranking": json.loads(ranking_json), "weight": weight}
        )
    return {"candidates": candidates, "ballots": ballots}


def _capture_snapshot(conn, revision) -> tuple[dict, str]:
    """Insert (or reuse) the immutable snapshot for a revision inside a write txn."""
    ledger = _build_ledger(conn)
    ledger_bytes = canonical_bytes(ledger)
    snapshot_sha = _sha256_bytes(ledger_bytes)
    conn.execute(
        "INSERT OR IGNORE INTO snapshots(revision, ledger_json, snapshot_sha256) "
        "VALUES(?, ?, ?)",
        (revision, ledger_bytes.decode("utf-8"), snapshot_sha),
    )
    existing = conn.execute(
        "SELECT ledger_json, snapshot_sha256 FROM snapshots WHERE revision = ?",
        (revision,),
    ).fetchone()
    if existing[1] != snapshot_sha:
        raise ArchiveError(
            f"snapshot already captured for revision {revision} with different bytes"
        )
    return ledger, snapshot_sha


def _install_report(store, revision, report_bytes) -> tuple[Path, str]:
    reports_dir = Path(store) / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    dest = reports_dir / f"revision-{revision}.json"
    if dest.is_symlink():
        raise ArchiveError("report destination must not be a symlink: " + str(dest))
    digest = _sha256_bytes(report_bytes)
    if dest.exists():
        # Immutable once installed: only exact byte-for-byte reuse is allowed.
        current = dest.read_bytes()
        if current != report_bytes:
            raise ArchiveError(
                f"immutable report {dest} already exists with different bytes"
            )
        return dest, digest
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".revision-{revision}.", suffix=".tmp", dir=str(reports_dir)
    )
    try:
        # Normal portable file permissions rather than mkstemp's 0600.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(report_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        _test_point("during_file_write")
        os.replace(tmp_name, dest)
        _fsync_dir(reports_dir)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return dest, digest


def _verify_report_file(store, revision) -> str:
    path = Path(store) / _report_relpath(revision)
    if path.is_symlink():
        raise ArchiveError("report path must not be a symlink: " + str(path))
    if not path.is_file():
        raise ArchiveError("referenced report is missing: " + str(path))
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _expected_report_digest(conn, revision):
    row = conn.execute(
        "SELECT report_sha256 FROM report_artifacts WHERE revision = ?", (revision,)
    ).fetchone()
    return None if row is None else row[0]


def _ensure_report_digest(conn, store, revision, allow_rebuild) -> str:
    """Verify a referenced report against its recorded digest.

    A present symlink or any byte divergence from the recorded digest is a
    hard failure. A missing file is rebuilt from the pinned snapshot only when
    allow_rebuild is set (recover/export), never during a plain status read.
    """
    path = Path(store) / _report_relpath(revision)
    if path.is_symlink():
        raise ArchiveError("report path must not be a symlink: " + str(path))
    expected = _expected_report_digest(conn, revision)
    if expected is None:
        raise ArchiveError(f"no report artifact registered for revision {revision}")
    if not path.is_file():
        if not allow_rebuild:
            raise ArchiveError("referenced report is missing: " + str(path))
        digest = _rebuild_missing_report(conn, store, revision)
        if digest != expected:
            raise ArchiveError(
                f"rebuilt report for revision {revision} does not match recorded digest"
            )
        return digest
    actual = _verify_report_file(store, revision)
    if actual != expected:
        raise ArchiveError(
            f"report {path} is corrupt: expected sha256 {expected}, found {actual}"
        )
    return actual


def _rebuild_missing_report(conn, store, revision) -> str:
    """Recreate a *missing* report from its pinned snapshot.

    Only a missing file is rebuilt; any present-but-diverging file is an
    immutable-file conflict and stays rejected.
    """
    row = conn.execute(
        "SELECT ledger_json, snapshot_sha256 FROM snapshots WHERE revision = ?",
        (revision,),
    ).fetchone()
    if row is None:
        raise ArchiveError(f"no snapshot registered for revision {revision}")
    ledger = json.loads(row[0])
    snapshot_sha = row[1]
    if _sha256_bytes(canonical_bytes(ledger)) != snapshot_sha:
        raise ArchiveError(f"stored snapshot digest mismatch for revision {revision}")
    report_bytes = _build_report_bytes(revision, ledger, snapshot_sha)
    _, digest = _install_report(store, revision, report_bytes)
    return digest


def _build_report_bytes(revision, ledger, snapshot_sha) -> bytes:
    # analyze() receives exactly the original {candidates, ballots} contract.
    report = {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "candidates": ledger["candidates"],
        "ballots": ledger["ballots"],
        "snapshot_sha256": snapshot_sha,
        "result": analyze(ledger),
    }
    return canonical_bytes(report)


def _finalize_task(store, conn, export_id, revision) -> dict:
    """Write (or reuse) the immutable report and mark the task ready, all recoverably."""
    row = conn.execute(
        "SELECT ledger_json, snapshot_sha256 FROM snapshots WHERE revision = ?",
        (revision,),
    ).fetchone()
    if row is None:  # pragma: no cover - defensive
        raise ArchiveError(f"no snapshot registered for revision {revision}")
    ledger = json.loads(row[0])
    snapshot_sha = row[1]
    if _sha256_bytes(canonical_bytes(ledger)) != snapshot_sha:
        raise ArchiveError(f"stored snapshot digest mismatch for revision {revision}")

    report_bytes = _build_report_bytes(revision, ledger, snapshot_sha)
    _, digest = _install_report(store, revision, report_bytes)
    _test_point("after_file_install")

    try:
        conn.execute("BEGIN IMMEDIATE")
        # The immutable artifact digest is part of the same commit as the
        # ready state and the publish pointer.
        recorded = _expected_report_digest(conn, revision)
        if recorded is None:
            conn.execute(
                "INSERT INTO report_artifacts(revision, report_sha256) VALUES(?, ?)",
                (revision, digest),
            )
        elif recorded != digest:
            conn.execute("ROLLBACK")
            raise ArchiveError(
                f"immutable report digest for revision {revision} conflicts "
                "with the recorded artifact"
            )
        state = conn.execute(
            "SELECT state FROM exports WHERE export_id = ?", (export_id,)
        ).fetchone()
        if state is None:  # pragma: no cover - defensive
            conn.execute("ROLLBACK")
            raise ArchiveError("export task vanished: " + export_id)
        if state[0] != "ready":
            conn.execute(
                "UPDATE exports SET state = 'ready' WHERE export_id = ?", (export_id,)
            )
            published = conn.execute(
                "SELECT published_revision FROM meta WHERE id = 1"
            ).fetchone()[0]
            # Publish pointer only moves strictly forward, and only after the
            # complete report file is in place and verified.
            if published is None or revision > published:
                conn.execute(
                    "UPDATE meta SET published_revision = ? WHERE id = 1", (revision,)
                )
            _test_point("before_publish_commit")
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
    except BaseException:
        _rollback_safe(conn)
        raise
    return {
        "export_id": export_id,
        "revision": revision,
        "state": "ready",
        "report_path": _report_relpath(revision),
        "report_sha256": digest,
    }


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #

def export_report(store, export_id) -> dict:
    """Register (once) an export at the current head, then complete its report."""
    try:
        export_id = ident(export_id)
        conn = _connect(store)
        try:
            row = conn.execute(
                "SELECT revision, state FROM exports WHERE export_id = ?", (export_id,)
            ).fetchone()
            if row is None:
                try:
                    # Barrier point sits before acquiring the write lock so
                    # contending processes can both be observed by the test.
                    _test_point("before_register_commit")
                    conn.execute("BEGIN IMMEDIATE")
                    # Racing process may win registration of the same export id.
                    row = conn.execute(
                        "SELECT revision, state FROM exports WHERE export_id = ?",
                        (export_id,),
                    ).fetchone()
                    if row is None:
                        head = conn.execute(
                            "SELECT head_revision FROM meta WHERE id = 1"
                        ).fetchone()[0]
                        _capture_snapshot(conn, head)
                        conn.execute(
                            "INSERT INTO exports(export_id, revision, state) "
                            "VALUES(?, ?, 'pending')",
                            (export_id, head),
                        )
                        _test_point("in_register_txn")
                        conn.execute("COMMIT")
                        row = (head, "pending")
                    else:
                        conn.execute("ROLLBACK")
                except BaseException:
                    _rollback_safe(conn)
                    raise
            revision, state = row
            # No transaction or write lock is held here: applies and other
            # revisions' exports can proceed while this report is built.
            _test_point("after_register")
            if state == "ready":
                digest = _ensure_report_digest(conn, store, revision, allow_rebuild=True)
                return {
                    "export_id": export_id,
                    "revision": revision,
                    "state": "ready",
                    "report_path": _report_relpath(revision),
                    "report_sha256": digest,
                }
            return _finalize_task(store, conn, export_id, revision)
        finally:
            conn.close()
    except ArchiveError:
        raise
    except (ValueError, OSError, sqlite3.Error) as error:
        raise ArchiveError("export failed: " + str(error)) from error


# --------------------------------------------------------------------------- #
# status and recover
# --------------------------------------------------------------------------- #

def _status_locked(conn, store, allow_rebuild=False) -> dict:
    """Build status from one consistent read snapshot, verifying every file.

    With allow_rebuild=False (status), a missing file is a hard failure.
    Recovery passes allow_rebuild=True so missing files are regenerated from
    the pinned snapshot; corrupt or symlinked files are never rebuilt.
    """
    head, published_revision = conn.execute(
        "SELECT head_revision, published_revision FROM meta WHERE id = 1"
    ).fetchone()
    tasks = []
    for export_id, revision, state in conn.execute(
        "SELECT export_id, revision, state FROM exports ORDER BY export_id"
    ):
        entry = {"export_id": export_id, "revision": revision, "state": state}
        if state == "ready":
            entry["report_path"] = _report_relpath(revision)
            entry["report_sha256"] = _ensure_report_digest(
                conn, store, revision, allow_rebuild
            )
        tasks.append(entry)
    published = None
    if published_revision is not None:
        published = {
            "revision": published_revision,
            "report_path": _report_relpath(published_revision),
            "report_sha256": _ensure_report_digest(
                conn, store, published_revision, allow_rebuild
            ),
        }
    return {"head_revision": head, "published": published, "tasks": tasks}


def store_status(store) -> dict:
    try:
        conn = _connect(store)
        try:
            conn.execute("BEGIN")
            try:
                result = _status_locked(conn, store)
                conn.execute("COMMIT")
            except BaseException:
                _rollback_safe(conn)
                raise
            return result
        finally:
            conn.close()
    except ArchiveError:
        raise
    except (ValueError, OSError, sqlite3.Error) as error:
        raise ArchiveError("status failed: " + str(error)) from error


def recover(store) -> dict:
    """Complete every registered-but-unfinished export; never roll back applies."""
    try:
        conn = _connect(store)
        try:
            pending = conn.execute(
                "SELECT export_id, revision FROM exports WHERE state = 'pending' "
                "ORDER BY export_id"
            ).fetchall()
            recovered = [
                _finalize_task(store, conn, export_id, revision)
                for export_id, revision in pending
            ]
            # Integrity check across all ready/published references; missing
            # files may be rebuilt, corruption is never repaired silently.
            conn.execute("BEGIN")
            try:
                status = _status_locked(conn, store, allow_rebuild=True)
                conn.execute("COMMIT")
            except BaseException:
                _rollback_safe(conn)
                raise
            return {"recovered": recovered, "status": status}
        finally:
            conn.close()
    except ArchiveError:
        raise
    except (ValueError, OSError, sqlite3.Error) as error:
        raise ArchiveError("recover failed: " + str(error)) from error
