"""Tests for the durable archive: API, strict CLI, concurrency and crash recovery.

Concurrency and crash tests use real independent processes. Coordination uses
filesystem barriers (RB_ARCHIVE_BARRIER) and crash points (RB_ARCHIVE_CRASH_POINT)
exposed only for tests; the production CLI has no timing parameters.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURE = ROOT / "fixtures" / "input.json"
WORKER = Path(__file__).resolve().parent / "_archive_worker.py"

sys.path.insert(0, str(SRC))
from ranked_ballots import (  # noqa: E402
    ArchiveError,
    analyze,
    apply_batch,
    canonical_bytes,
    export_report,
    init_store,
    recover,
    store_status,
)


def write_json(path, obj, raw=None):
    Path(path).write_text(raw if raw is not None else json.dumps(obj), encoding="utf-8")


def make_store(tmp, source_obj=None):
    store = Path(tmp) / "store"
    source = Path(tmp) / "source.json"
    if source_obj is None:
        shutil.copy(FIXTURE, source)
    else:
        write_json(source, source_obj)
    init_store(store, source)
    return store, source


def batch(tmp, name, batch_id, expected, changes):
    path = Path(tmp) / (name + ".json")
    write_json(path, {"batch_id": batch_id, "expected_revision": expected, "changes": changes})
    return path


def put(bid, ranking, weight):
    return {"op": "put", "id": bid, "ranking": ranking, "weight": weight}


def delete(bid):
    return {"op": "delete", "id": bid}


def verify_report(path, expected_revision, expected_candidates):
    raw = Path(path).read_bytes()
    assert raw.endswith(b"\n")
    report = json.loads(raw)
    self_snapshot = canonical_bytes(
        {"candidates": report["candidates"], "ballots": report["ballots"]}
    )
    assert hashlib.sha256(self_snapshot).hexdigest() == report["snapshot_sha256"]
    assert analyze({"candidates": report["candidates"], "ballots": report["ballots"]}) == report[
        "result"
    ]
    assert report["schema_version"] == 2
    assert report["revision"] == expected_revision
    assert report["candidates"] == sorted(expected_candidates)
    assert [b["id"] for b in report["ballots"]] == sorted(b["id"] for b in report["ballots"])
    # IDs and timestamps never leak into the report bytes.
    assert b"export" not in raw and b"timestamp" not in raw
    return report


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_creates_fixed_ledger(self):
        store, _ = make_store(self.tmp)
        result = init_store  # imported name sanity
        self.assertIsNotNone(result)
        status = store_status(store)
        self.assertEqual(status["head_revision"], 0)
        self.assertIsNone(status["published"])
        self.assertEqual(status["tasks"], [])
        self.assertTrue((store / "archive.sqlite3").exists())
        self.assertTrue((store / "reports").is_dir())

    def test_init_refuses_existing_store(self):
        store, source = make_store(self.tmp)
        with self.assertRaises(ArchiveError):
            init_store(store, source)

    def test_init_inherits_v1_contract(self):
        bad = Path(self.tmp) / "bad.json"
        for body in ["{}", '{"a":1,"a":2}', "NaN", ""]:
            write_json(bad, None, raw=body)
            with self.assertRaises(ArchiveError):
                init_store(Path(self.tmp) / ("s" + str(len(body))), bad)
        write_json(
            bad,
            {"candidates": ["A", "A"], "ballots": []},
        )
        with self.assertRaises(ArchiveError):
            init_store(Path(self.tmp) / "dup", bad)

    def test_candidate_list_is_fixed(self):
        store, _ = make_store(self.tmp)
        src = batch(self.tmp, "x", "x1", 0, [put("n1", ["ZZZ"], 1)])
        with self.assertRaises(ArchiveError):
            apply_batch(store, src)
        self.assertEqual(store_status(store)["head_revision"], 0)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, _ = make_store(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_put_replace_delete_and_revision_advance(self):
        b1 = batch(self.tmp, "b1", "b1", 0, [put("one", ["A", "C"], 4)])
        receipt = apply_batch(self.store, b1)
        self.assertEqual(receipt["parent_revision"], 0)
        self.assertEqual(receipt["revision"], 1)
        self.assertEqual(receipt["batch_id"], "b1")
        self.assertEqual(receipt["request_sha256"], hashlib.sha256(b1.read_bytes()).hexdigest())
        self.assertEqual(store_status(self.store)["head_revision"], 1)

        b2 = batch(self.tmp, "b2", "b2", 1, [delete("two"), put("four", ["B"], 9)])
        receipt2 = apply_batch(self.store, b2)
        self.assertEqual(receipt2["revision"], 2)
        # Replacements that do not change business values still create a revision.
        b3 = batch(self.tmp, "b3", "b3", 2, [put("one", ["A", "C"], 4)])
        receipt3 = apply_batch(self.store, b3)
        self.assertEqual(receipt3["revision"], 3)

    def test_replay_same_bytes_returns_original_receipt(self):
        b1 = batch(self.tmp, "b1", "b1", 0, [put("one", ["A"], 1)])
        first = apply_batch(self.store, b1)
        again = apply_batch(self.store, b1)
        self.assertEqual(first, again)
        self.assertEqual(store_status(self.store)["head_revision"], 1)

    def test_same_id_different_bytes_conflicts_without_booking(self):
        b1 = batch(self.tmp, "b1", "b1", 0, [put("one", ["A"], 1)])
        apply_batch(self.store, b1)
        conflict = batch(self.tmp, "c1", "b1", 1, [put("one", ["B"], 1)])
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, conflict)
        # Replay ordering: the original bytes still return the original receipt,
        # even though expected_revision inside them is now stale.
        self.assertEqual(apply_batch(self.store, b1)["revision"], 1)
        self.assertEqual(store_status(self.store)["head_revision"], 1)
        with sqlite3.connect(self.store / "archive.sqlite3") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)

    def test_stale_expected_revision(self):
        b1 = batch(self.tmp, "b1", "b1", 0, [put("one", ["A"], 1)])
        apply_batch(self.store, b1)
        stale = batch(self.tmp, "st", "st", 0, [put("x", ["A"], 1)])
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, stale)

    def test_boolean_revision_rejected(self):
        src = batch(self.tmp, "bool", "bool", True, [put("x", ["A"], 1)])
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, src)

    def test_unknown_delete_and_duplicate_change_are_atomic(self):
        bad = batch(
            self.tmp,
            "bad",
            "bad",
            0,
            [put("new1", ["A"], 1), delete("ghost"), put("new2", ["B"], 1)],
        )
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, bad)
        dup = batch(
            self.tmp,
            "dup",
            "dup",
            0,
            [put("new1", ["A"], 1), put("new1", ["B"], 1)],
        )
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, dup)
        self.assertEqual(store_status(self.store)["head_revision"], 0)
        with sqlite3.connect(self.store / "archive.sqlite3") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ballots").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)

    def test_illegal_batch_in_mixed_change_changes_nothing(self):
        bad = batch(
            self.tmp,
            "mix",
            "mix",
            0,
            [put("new1", ["A"], 1), delete("ghost")],
        )
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, bad)
        self.assertEqual(store_status(self.store)["head_revision"], 0)

    def test_strict_envelope_and_change_shapes(self):
        cases = [
            {"batch_id": "z", "expected_revision": 0, "changes": [], "extra": 1},
            {"batch_id": "z", "expected_revision": -1, "changes": [put("a", ["A"], 1)]},
            {"batch_id": "z", "expected_revision": 0, "changes": [{"op": "frob", "id": "a"}]},
            {"batch_id": "z", "expected_revision": 0, "changes": [{"op": "delete"}]},
            {"batch_id": "z", "expected_revision": 0, "changes": [{"op": "put", "id": "a",
                                                                    "ranking": ["A"], "weight": 0}]},
            {"batch_id": "z", "expected_revision": 0, "changes": [{"op": "delete", "id": "a",
                                                                    "extra": 2}]},
        ]
        for i, obj in enumerate(cases):
            path = Path(self.tmp) / f"shape{i}.json"
            write_json(path, obj)
            with self.assertRaises(ArchiveError):
                apply_batch(self.store, path)

    def test_duplicate_json_key_rejected(self):
        path = Path(self.tmp) / "dupkey.json"
        write_json(
            path,
            None,
            raw='{"batch_id":"z","expected_revision":0,"changes":['
            '{"op":"put","id":"a","ranking":[],"weight":1}],"changes":[]}',
        )
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, path)

    def test_effective_ballot_limit(self):
        # Fixture already holds 3 ballots; fill to 20000, then one more must fail.
        fills = [put(f"n{i}", ["A"], 1) for i in range(20000 - 3)]
        ok = batch(self.tmp, "fill", "fill", 0, fills)
        receipt = apply_batch(self.store, ok)
        self.assertEqual(receipt["revision"], 1)
        over = batch(self.tmp, "over", "over", 1, [put("onemore", ["A"], 1)])
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, over)
        self.assertEqual(store_status(self.store)["head_revision"], 1)

    def test_weight_boundary(self):
        edge = batch(self.tmp, "edge", "edge", 0, [put("one", ["A"], 10**9)])
        self.assertEqual(apply_batch(self.store, edge)["revision"], 1)
        too_big = batch(self.tmp, "big", "big", 1, [put("two", ["A"], 10**9 + 1)])
        with self.assertRaises(ArchiveError):
            apply_batch(self.store, too_big)

    def test_empty_store_round_trip(self):
        empty_dir = Path(self.tmp) / "emptysrc"
        empty_dir.mkdir()
        source = empty_dir / "in.json"
        write_json(source, {"candidates": ["A", "B"], "ballots": []})
        store = Path(self.tmp) / "emptystore"
        init_store(store, source)
        add = batch(self.tmp, "add", "add", 0, [put("only", ["B"], 5)])
        self.assertEqual(apply_batch(store, add)["revision"], 1)
        gone = batch(self.tmp, "gone", "gone", 1, [delete("only")])
        self.assertEqual(apply_batch(store, gone)["revision"], 2)
        report = export_report(store, "e")
        self.assertEqual(report["revision"], 2)
        verify_report(Path(self.tmp) / "emptystore" / report["report_path"], 2, ["A", "B"])


class ResultSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, _ = make_store(
            self.tmp,
            {"candidates": ["A", "B", "C"], "ballots": []},
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _apply(self, name, rev, changes):
        return apply_batch(self.store, batch(self.tmp, name, name, rev, changes))

    def _export(self, export_id, rev):
        info = export_report(self.store, export_id)
        path = Path(self.tmp) / "store" / info["report_path"]
        return verify_report(path, rev, ["A", "B", "C"])

    def test_transfer_exhaustion_and_winner_change(self):
        # Round 1: A=3, B=3, C=2 -> C eliminated; C ballots transfer to A -> A wins.
        self._apply(
            "b0",
            0,
            [
                put("a1", ["A"], 2),
                put("a2", ["A", "B"], 1),
                put("b1", ["B"], 3),
                put("c1", ["C", "A"], 2),
            ],
        )
        r0 = self._export("e0", 1)
        self.assertEqual(r0["result"]["winner"], "A")
        self.assertEqual(r0["result"]["rounds"][0]["eliminated"], "C")
        for row in r0["result"]["rounds"]:
            self.assertEqual(
                sum(row["tallies"].values()) + row["exhausted_weight"],
                r0["result"]["total_weight"],
            )

        # Correction: C voters lose their second choice -> those ballots exhaust;
        # B then wins 3 vs 3? A=3 (a1+a2), B=3 -> tie, A eliminated lexicographically,
        # B wins. Make it decisive instead: exhaust one A ballot too.
        self._apply(
            "b1",
            1,
            [
                put("c1", ["C"], 2),
                put("a2", [], 1),  # exhausted ballot
            ],
        )
        r1 = self._export("e1", 2)
        self.assertEqual(r1["result"]["winner"], "B")
        self.assertEqual(r1["result"]["total_weight"], r0["result"]["total_weight"])
        # The earlier report file is untouched and still describes revision 1.
        old = json.loads((Path(self.tmp) / "store" / "reports" / "revision-1.json").read_text())
        self.assertEqual(old["revision"], 1)
        self.assertEqual(old["snapshot_sha256"], r0["snapshot_sha256"])

    def test_all_exhausted(self):
        self._apply("b0", 0, [put("x", [], 4)])
        report = self._export("e", 1)
        self.assertIsNone(report["result"]["winner"])
        self.assertEqual(report["result"]["rounds"][0]["outcome"], "no_continuing_ballots")


class ExportAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, _ = make_store(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_registers_then_publishes(self):
        info = export_report(self.store, "run-1")
        self.assertEqual(info["state"], "ready")
        self.assertEqual(info["revision"], 0)
        self.assertEqual(info["report_path"], "reports/revision-0.json")
        status = store_status(self.store)
        self.assertEqual(status["published"]["revision"], 0)
        self.assertEqual(status["published"]["report_sha256"], info["report_sha256"])
        task = status["tasks"][0]
        self.assertEqual(task["export_id"], "run-1")
        self.assertEqual(task["state"], "ready")

    def test_two_export_ids_same_revision_share_bytes(self):
        i1 = export_report(self.store, "id-a")
        i2 = export_report(self.store, "id-b")
        self.assertEqual(i1["report_sha256"], i2["report_sha256"])
        report_files = list((self.store / "reports").glob("revision-*.json"))
        self.assertEqual(len(report_files), 1)

    def test_published_pointer_is_monotonic(self):
        export_report(self.store, "old")  # revision 0 published
        apply_batch(self.store, batch(self.tmp, "b1", "b1", 0, [put("n", ["A"], 1)]))
        export_report(self.store, "new")  # revision 1 published
        # Completing an old-revision export id afterwards must not roll back.
        apply_batch(self.store, batch(self.tmp, "b2", "b2", 1, [put("m", ["A"], 1)]))
        # A fresh id now captures rev 2; replaying "old" stays pinned at rev 0.
        export_report(self.store, "current")
        old_again = export_report(self.store, "old")
        self.assertEqual(old_again["revision"], 0)
        self.assertEqual(store_status(self.store)["published"]["revision"], 2)

    def test_status_sorted_tasks_and_damage_detection(self):
        export_report(self.store, "zeta")
        export_report(self.store, "alpha")
        status = store_status(self.store)
        self.assertEqual([t["export_id"] for t in status["tasks"]], ["alpha", "zeta"])
        target = self.store / "reports" / "revision-0.json"
        original = target.read_bytes()
        target.write_bytes(original + b" ")
        with self.assertRaises(ArchiveError):
            store_status(self.store)
        with self.assertRaises(ArchiveError):
            recover(self.store)
        target.write_bytes(original)  # exact restore is fine, no silent rewrite happened

    def test_symlink_destination_rejected(self):
        link = self.store / "reports" / "revision-0.json"
        link.parent.mkdir(parents=True, exist_ok=True)
        outside = Path(self.tmp) / "outside.json"
        link.symlink_to(outside)
        with self.assertRaises(ArchiveError):
            export_report(self.store, "e")
        self.assertFalse(outside.exists())

    def test_immutable_file_conflict_rejected(self):
        export_report(self.store, "e")
        target = self.store / "reports" / "revision-0.json"
        target.write_bytes(b'{"different": true}\n')
        with self.assertRaises(ArchiveError):
            recover(self.store)
        with self.assertRaises(ArchiveError):
            export_report(self.store, "e2")

    def test_report_is_canonical_encoding(self):
        info = export_report(self.store, "e")
        raw = (self.store / info["report_path"]).read_bytes()
        report = json.loads(raw)
        self.assertEqual(
            raw,
            canonical_bytes(
                {
                    "schema_version": 2,
                    "revision": 0,
                    "candidates": report["candidates"],
                    "ballots": report["ballots"],
                    "snapshot_sha256": report["snapshot_sha256"],
                    "result": report["result"],
                }
            ),
        )

    def test_report_verifies_with_nothing_but_its_file(self):
        # Another verifier only receives the single JSON file, no database.
        info = export_report(self.store, "e")
        portable = Path(self.tmp) / "moved-report.json"
        shutil.copyfile(self.store / info["report_path"], portable)
        raw = portable.read_bytes()
        self.assertEqual(raw.decode("utf-8").encode("utf-8"), raw)  # strict UTF-8
        report = json.loads(raw)
        self.assertEqual(set(report), {
            "schema_version", "revision", "candidates", "ballots",
            "snapshot_sha256", "result",
        })
        ledger_bytes = canonical_bytes(
            {"candidates": report["candidates"], "ballots": report["ballots"]}
        )
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(), report["snapshot_sha256"])
        self.assertEqual(
            analyze({"candidates": report["candidates"], "ballots": report["ballots"]}),
            report["result"],
        )
        self.assertEqual(oct(portable.stat().st_mode & 0o777), "0o644")


# --------------------------------------------------------------------------- #
# Real independent processes
# --------------------------------------------------------------------------- #

class ProcessMixin:
    def worker(self, *args, env_extra=None, expect_rc=0):
        env = dict(os.environ)
        env["RB_PYTHONPATH"] = str(SRC)
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, "-I", str(WORKER), *[str(a) for a in args]],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stderr + proc.stdout)
        return proc


class ConcurrencyTests(unittest.TestCase, ProcessMixin):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, _ = make_store(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parallel_applies_one_wins_and_replay_returns_revision(self):
        b1 = batch(self.tmp, "p1", "p1", 0, [put("n1", ["A"], 1)])
        b2 = batch(self.tmp, "p2", "p2", 0, [put("n2", ["B"], 1)])
        procs = [
            subprocess.Popen(
                [sys.executable, "-I", str(WORKER), "apply", self.store, src],
                env={**os.environ, "RB_PYTHONPATH": str(SRC)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for src in (b1, b2)
        ]
        captured = [p.communicate() for p in procs]
        results = [p.returncode for p in procs]
        self.assertEqual(sorted(results), [0, 2])
        winner_index = next(i for i, p in enumerate(procs) if p.returncode == 0)
        receipt = json.loads(captured[winner_index][0])
        self.assertEqual(receipt["revision"], 1)
        winner_src = (b1, b2)[winner_index]
        # Replaying the winning batch in a fresh process returns the same revision.
        again = self.worker("apply", self.store, winner_src)
        self.assertEqual(json.loads(again.stdout)["revision"], 1)
        # The losing batch, once corrected to the new baseline, can still apply.
        loser_src = (b2, b1)[winner_index]
        loser = json.loads(Path(loser_src).read_text())
        loser["expected_revision"] = 1
        fixed = Path(self.tmp) / "fixed.json"
        write_json(fixed, loser)
        self.assertEqual(json.loads(self.worker("apply", self.store, fixed).stdout)["revision"], 2)
        self.assertEqual(store_status(self.store)["head_revision"], 2)

    def test_illegal_parallel_batch_leaves_no_trace(self):
        good = batch(self.tmp, "g", "g", 0, [put("n1", ["A"], 1)])
        bad = batch(self.tmp, "bad", "bad", 0, [delete("ghost")])
        procs = [
            subprocess.Popen(
                [sys.executable, "-I", str(WORKER), "apply", self.store, src],
                env={**os.environ, "RB_PYTHONPATH": str(SRC)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for src in (good, bad)
        ]
        captured = [p.communicate() for p in procs]
        results = sorted(p.returncode for p in procs)
        self.assertEqual(results, [0, 2])
        self.assertTrue(captured[0][0] or captured[1][0])  # one success receipt
        self.assertEqual(store_status(self.store)["head_revision"], 1)
        with sqlite3.connect(self.store / "archive.sqlite3") as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 1)

    def test_export_does_not_block_apply_and_revision_r1_publishes_first(self):
        """Link acceptance test: barriers only, no sleeps."""
        barrier_dir = Path(self.tmp) / "barrier"
        barrier_dir.mkdir()
        wait_flag = barrier_dir / "after_register.wait"
        wait_flag.touch()
        exporter = subprocess.Popen(
            [sys.executable, "-I", str(WORKER), "barrier-export",
             self.store, "r0", "after_register", barrier_dir],
            env={**os.environ, "RB_PYTHONPATH": str(SRC)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait until the r0 export has registered its snapshot and task.
        ready_flag = barrier_dir / "after_register.ready"
        deadline = time.time() + 30
        while not ready_flag.exists():
            if exporter.poll() is not None:
                err = exporter.stderr.read()
                exporter.stdout.close()
                exporter.stderr.close()
                self.fail(err)
            time.sleep(0.02)
            if time.time() > deadline:
                self.fail("exporter never reached barrier")
        status = store_status(self.store)
        self.assertEqual(status["head_revision"], 0)
        self.assertEqual(status["tasks"][0]["state"], "pending")

        nxt = batch(self.tmp, "nxt", "nxt", 0, [put("n1", ["A"], 7)])
        apply_rc = self.worker("apply", self.store, nxt)
        self.assertEqual(json.loads(apply_rc.stdout)["revision"], 1)
        self.assertEqual(store_status(self.store)["head_revision"], 1)

        # Another revision completes and publishes first.
        pub = self.worker("export", self.store, "r1")
        self.assertEqual(json.loads(pub.stdout)["revision"], 1)
        self.assertEqual(store_status(self.store)["published"]["revision"], 1)

        # Release the paused r0 export; it finishes against its old snapshot.
        wait_flag.unlink()
        self.assertEqual(exporter.wait(30), 0)
        out = json.loads(exporter.stdout.read())
        self.assertEqual(out["revision"], 0)
        self.assertEqual(out["state"], "ready")

        r0_report = json.loads(
            (self.store / "reports" / "revision-0.json").read_text(encoding="utf-8")
        )
        r1_report = json.loads(
            (self.store / "reports" / "revision-1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(r0_report["revision"], 0)
        self.assertEqual(r1_report["revision"], 1)
        verify_report(self.store / "reports" / "revision-0.json", 0, ["A", "B", "C"])
        verify_report(self.store / "reports" / "revision-1.json", 1, ["A", "B", "C"])
        status = store_status(self.store)
        self.assertEqual(status["published"]["revision"], 1)
        self.assertEqual(
            {t["export_id"]: t["state"] for t in status["tasks"]},
            {"r0": "ready", "r1": "ready"},
        )

    def test_same_export_id_contention_pins_one_revision(self):
        barrier_dir = Path(self.tmp) / "barrier"
        barrier_dir.mkdir()
        (barrier_dir / "before_register_commit.wait").touch()
        procs = [
            subprocess.Popen(
                [sys.executable, "-I", str(WORKER), "barrier-export",
                 self.store, "race", "before_register_commit", barrier_dir],
                env={**os.environ, "RB_PYTHONPATH": str(SRC)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        # Both processes must have reached (and blocked at) the registration
        # point before release: one holds the write lock, the other waits.
        deadline = time.time() + 30
        while len(list(barrier_dir.glob("before_register_commit.arrived-*"))) < 2:
            for proc in procs:
                if proc.poll() is not None:
                    err = proc.stderr.read()
                    for other in procs:
                        other.stdout.close()
                        other.stderr.close()
                    self.fail(err)
            time.sleep(0.02)
            if time.time() > deadline:
                self.fail("contenders never both reached the barrier")
        (barrier_dir / "before_register_commit.wait").unlink()
        captured = [p.communicate(timeout=30) for p in procs]
        self.assertEqual(sorted(p.returncode for p in procs), [0, 0])
        with sqlite3.connect(self.store / "archive.sqlite3") as conn:
            rows = conn.execute(
                "SELECT revision, state FROM exports WHERE export_id = 'race'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (0, "ready"))
        self.assertTrue(all(out for out, _ in captured))


class CrashTests(unittest.TestCase, ProcessMixin):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store, _ = make_store(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _kill_export(self, point):
        proc = self.worker(
            "crash-export", self.store, "e-kill", point, expect_rc=-9
        )
        return proc

    def test_kill_before_registration_is_retryable(self):
        for point in ("before_register_commit", "in_register_txn"):
            shutil.rmtree(self.store, ignore_errors=True)
            source = Path(self.tmp) / "source.json"
            init_store(self.store, source)
            self.worker("crash-export", self.store, "e-kill", point, expect_rc=-9)
            with sqlite3.connect(self.store / "archive.sqlite3") as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0], 0
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0
                )
            info = export_report(self.store, "e-kill")
            self.assertEqual(info["revision"], 0)

    def test_concurrent_recover_completes_pending_task_once(self):
        # Register a task, then kill the writer so the task stays pending.
        self._kill_export("after_register")
        self.assertEqual(store_status(self.store)["tasks"][0]["state"], "pending")
        procs = [
            subprocess.Popen(
                [sys.executable, "-I", str(WORKER), "recover", self.store],
                env={**os.environ, "RB_PYTHONPATH": str(SRC)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        captured = [p.communicate(timeout=60) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0, 0])
        status = store_status(self.store)
        self.assertEqual(len(status["tasks"]), 1)
        self.assertEqual(status["tasks"][0]["state"], "ready")
        self.assertEqual(status["published"]["revision"], 0)
        # Both processes agree on the single immutable report digest.
        digests = {
            json.loads(out)["status"]["tasks"][0]["report_sha256"]
            for out, _ in captured
        }
        self.assertEqual(digests, {status["tasks"][0]["report_sha256"]})

    def test_kill_after_registration_pins_revision_then_recover(self):
        self._kill_export("after_register")
        status = store_status(self.store)
        self.assertEqual(status["tasks"][0]["state"], "pending")
        self.assertIsNone(status["published"])
        # Corrections continue while the old task is unfinished.
        apply_batch(self.store, batch(self.tmp, "b1", "b1", 0, [put("n1", ["A"], 5)]))
        # Replaying the same export id must keep the original revision.
        replayed = self.worker("export", self.store, "e-kill")
        self.assertEqual(json.loads(replayed.stdout)["revision"], 0)
        status = store_status(self.store)
        self.assertEqual(status["head_revision"], 1)
        self.assertEqual(status["published"]["revision"], 0)
        r0 = json.loads((self.store / "reports" / "revision-0.json").read_text())
        self.assertEqual(r0["revision"], 0)
        self.assertEqual(len(r0["ballots"]), 3)
        # The replayed report's ledger, result and digest all belong to revision 0.
        verify_report(
            self.store / "reports" / "revision-0.json", 0, ["A", "B", "C"]
        )
        with sqlite3.connect(self.store / "archive.sqlite3") as conn:
            pinned = conn.execute(
                "SELECT snapshot_sha256 FROM snapshots WHERE revision = 0"
            ).fetchone()[0]
        self.assertEqual(pinned, r0["snapshot_sha256"])

    def test_kill_during_file_write_leaves_only_unreferenced_residue(self):
        self._kill_export("during_file_write")
        status = store_status(self.store)
        self.assertEqual(status["tasks"][0]["state"], "pending")
        temps = list((self.store / "reports").glob(".revision-*.tmp"))
        self.assertTrue(temps)
        rec = self.worker("recover", self.store)
        payload = json.loads(rec.stdout)
        self.assertEqual(payload["recovered"][0]["export_id"], "e-kill")
        self.assertEqual(store_status(self.store)["published"]["revision"], 0)
        # Residue may remain but must never be referenced as a report.
        reports = list((self.store / "reports").glob("revision-*.json"))
        self.assertEqual([p.name for p in reports], ["revision-0.json"])

    def test_kill_after_file_install_before_publish_commit(self):
        self._kill_export("before_publish_commit")
        # The file is complete but the pointer/task commit died with the process.
        report = self.store / "reports" / "revision-0.json"
        self.assertTrue(report.exists())
        status = store_status(self.store)
        self.assertEqual(status["tasks"][0]["state"], "pending")
        self.assertIsNone(status["published"])
        self.worker("recover", self.store)
        status = store_status(self.store)
        self.assertEqual(status["tasks"][0]["state"], "ready")
        self.assertEqual(status["published"]["revision"], 0)

    def test_kill_after_publish_survives_reopen(self):
        info = export_report(self.store, "e")
        # Simulate a brand new process reading the committed state.
        proc = self.worker("status", self.store)
        status = json.loads(proc.stdout)
        self.assertEqual(status["published"]["revision"], 0)
        self.assertEqual(status["published"]["report_sha256"], info["report_sha256"])

    def test_apply_survives_process_restart(self):
        src = batch(self.tmp, "b1", "b1", 0, [put("n1", ["A"], 5)])
        self.worker("apply", self.store, src)
        proc = self.worker("apply", self.store, src)  # replay in another process
        self.assertEqual(json.loads(proc.stdout)["revision"], 1)
        self.assertEqual(store_status(self.store)["head_revision"], 1)

    def test_damaged_ready_report_is_refused_not_rewritten(self):
        export_report(self.store, "e")
        target = self.store / "reports" / "revision-0.json"
        original = target.read_bytes()
        target.write_bytes(original[:-1])
        with self.assertRaises(ArchiveError):
            export_report(self.store, "e")
        with self.assertRaises(ArchiveError):
            store_status(self.store)
        # The immutable file was not silently repaired.
        self.assertEqual(target.read_bytes(), original[:-1])

    def test_missing_report_file_fails_loudly(self):
        export_report(self.store, "e")
        (self.store / "reports" / "revision-0.json").unlink()
        with self.assertRaises(ArchiveError):
            store_status(self.store)
        # Recovery rebuilds the missing immutable bytes from the pinned snapshot.
        rec = self.worker("recover", self.store)
        self.assertEqual(json.loads(rec.stdout)["status"]["published"]["revision"], 0)


class CliTests(unittest.TestCase, ProcessMixin):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.source = Path(self.tmp) / "input.json"
        shutil.copy(FIXTURE, self.source)
        self.store = Path(self.tmp) / "store"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, expect_rc=0):
        # -I ignores PYTHONPATH, so use a generated launcher that inserts the
        # src path before importing; this still exercises the real CLI main().
        launcher = Path(self.tmp) / "run_archive_cli.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from ranked_ballots.archive_cli import main\n"
            "raise SystemExit(main())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-I", str(launcher), *map(str, args)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stderr)
        return proc

    def test_full_cli_lifecycle(self):
        out = self.cli("init", "--store", self.store, "--source", self.source)
        payload = json.loads(out.stdout)
        self.assertEqual(payload["head_revision"], 0)

        bsrc = Path(self.tmp) / "b.json"
        write_json(
            bsrc,
            {"batch_id": "b1", "expected_revision": 0,
             "changes": [put("one", ["B"], 4)]},
        )
        receipt = json.loads(self.cli("apply", "--store", self.store, "--source", bsrc).stdout)
        self.assertEqual(receipt["revision"], 1)

        exported = json.loads(
            self.cli("export", "--store", self.store, "--export-id", "run-1").stdout
        )
        self.assertEqual(exported["state"], "ready")

        status = json.loads(self.cli("status", "--store", self.store).stdout)
        self.assertEqual(status["head_revision"], 1)
        self.assertEqual(status["published"]["revision"], 1)

        recovered = json.loads(self.cli("recover", "--store", self.store).stdout)
        self.assertEqual(recovered["recovered"], [])

    def test_cli_failure_exit_two_with_reason(self):
        proc = self.cli("status", "--store", self.store, expect_rc=2)
        self.assertIn("not an archive store", proc.stderr)
        init_out = self.cli("init", "--store", self.store, "--source", self.source)
        self.assertEqual(init_out.returncode, 0)
        bad = Path(self.tmp) / "bad.json"
        write_json(bad, {"batch_id": "x", "expected_revision": True, "changes": []})
        proc = self.cli("apply", "--store", self.store, "--source", bad, expect_rc=2)
        self.assertTrue(proc.stderr.strip())

    def test_v1_cli_still_works(self):
        out = Path(self.tmp) / "v1.json"
        launcher = Path(self.tmp) / "run_v1_cli.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from ranked_ballots.cli import main\n"
            "raise SystemExit(main())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-I", str(launcher),
             "--source", self.source, "--output", out],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(out.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["result"], analyze(json.loads(self.source.read_text())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
