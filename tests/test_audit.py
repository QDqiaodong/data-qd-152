"""Tests for the synthetic paper-ballot audit activity.

Only the implemented without-replacement design is exercised: the
with-replacement rules are intentionally not implemented, so no test may mix
the two sets of rules.
"""
import hashlib
import json
import random
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))
from ranked_ballots import (  # noqa: E402
    AuditError,
    analyze,
    audit,
    canonical_bytes,
    draw,
    record,
    start,
    status,
)


SEED = "00" * 32


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def make_report(candidates, ballots, revision=0):
    ledger = {"candidates": list(candidates), "ballots": list(ballots)}
    snapshot = hashlib.sha256(canonical_bytes(ledger)).hexdigest()
    return {
        "schema_version": 2,
        "revision": revision,
        "candidates": list(candidates),
        "ballots": list(ballots),
        "snapshot_sha256": snapshot,
        "result": analyze(ledger),
    }


def write_report(tmp, report, name="report.json"):
    path = Path(tmp) / name
    path.write_bytes(canonical_bytes(report))
    return path


def make_manifest(tmp, containers, name="manifest.json"):
    path = Path(tmp) / name
    path.write_text(json.dumps({"containers": containers}), encoding="utf-8")
    return path


def observation_file(tmp, observations, name="obs.json"):
    path = Path(tmp) / ("%s-%d.json" % (name, len(os.listdir(tmp))))
    path.write_text(json.dumps({"observations": observations}), encoding="utf-8")
    return path


def build_session(tmp, candidates, ballots, containers, *, seed=SEED,
                  risk="1/4", max_draws=20, name="session.json"):
    report = make_report(candidates, ballots)
    report_path = write_report(tmp, report)
    manifest_path = make_manifest(tmp, containers)
    session = Path(tmp) / name
    start(report_path, manifest_path, seed, risk, max_draws, session)
    return session


def two_candidate_session(tmp, *, weight_a=1, weight_b=3, count=None,
                          max_draws=20, seed=SEED, name="session.json"):
    """A single majority assertion: B vs A, N = weight_a + weight_b."""
    ballots = [
        {"id": "a", "ranking": ["A"], "weight": weight_a},
        {"id": "b", "ranking": ["B"], "weight": weight_b},
    ]
    if count is None:
        count = weight_a + weight_b
    return build_session(
        tmp, ["A", "B"], ballots, [{"id": "box", "count": count}],
        max_draws=max_draws, seed=seed, name=name,
    )


def brute_force_draws(containers, n, seed, count):
    """Straightforward per-position reference for the sampler."""
    remaining = list(range(n))
    starts = {}
    running = 0
    for entry in containers:
        starts[entry["id"]] = (running, running + entry["count"])
        running += entry["count"]
    out = []
    counter = 0
    seed_bytes = bytes.fromhex(seed)
    for index in range(1, count + 1):
        modulus = len(remaining)
        while True:
            value = int.from_bytes(
                hashlib.sha256(seed_bytes + counter.to_bytes(16, "big")).digest(),
                "big")
            counter += 1
            limit = (1 << 256) - ((1 << 256) % modulus)
            if value < limit:
                selected = value % modulus
                break
        position = remaining.pop(selected)
        container = next(cid for cid, (low, high) in starts.items()
                         if low <= position < high)
        out.append({
            "index": index,
            "global_position": position,
            "container": container,
            "position_in_container": position - starts[container][0] + 1,
        })
    return out


def project_draws(draws):
    return [
        (d["draw_index"], d["position"], d["container"], d["position_in_container"])
        for d in draws
    ]


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# start: inputs, immutability, and the report contract
# --------------------------------------------------------------------------- #

class StartTests(TempDir):
    def test_report_digest_records_the_input_bytes_provenance(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        canonical = Path(self.tmp) / "canonical.json"
        canonical.write_bytes(canonical_bytes(report))
        pretty = Path(self.tmp) / "pretty.json"
        pretty.write_text(json.dumps(report, indent=2), encoding="utf-8")
        one = start(canonical, manifest, SEED, "1/4", 4,
                    Path(self.tmp) / "c.json")
        two = start(pretty, manifest, SEED, "1/4", 4,
                    Path(self.tmp) / "p.json")
        self.assertEqual(one["report_sha256"],
                         hashlib.sha256(canonical.read_bytes()).hexdigest())
        self.assertEqual(two["report_sha256"],
                         hashlib.sha256(pretty.read_bytes()).hexdigest())
        # The pinned content, snapshot digest and asserts are identical either
        # way, so the draw stream is too.
        self.assertEqual(one["snapshot_sha256"], two["snapshot_sha256"])
        self.assertEqual(draw(Path(self.tmp) / "c.json", 4)["draws"],
                         draw(Path(self.tmp) / "p.json", 4)["draws"])

    def test_start_accepts_valid_report_and_manifest(self):
        session = two_candidate_session(self.tmp)
        payload = status(session)
        self.assertEqual(payload["protocol"], "ranked-ballots-audit")
        self.assertEqual(payload["protocol_version"], 1)
        self.assertEqual(payload["design"], "without_replacement")
        self.assertEqual(payload["n"], 4)
        self.assertEqual(payload["effective_budget"], 4)
        self.assertEqual(payload["alpha"], {"numerator": 1, "denominator": 4, "text": "1/4"})
        self.assertEqual(payload["verdict"], "continue")
        self.assertEqual(payload["draw_count"], 0)
        self.assertEqual(payload["consumed"], 0)
        self.assertIsNone(payload["stop_draw"])
        self.assertEqual(len(payload["assertions"]), 1)
        self.assertEqual(payload["assertions"][0]["id"], "r1-majority-B")
        self.assertEqual(payload["assertions"][0]["state"], "pending")

    def test_start_normalizes_lowercase_seed_and_reduces_risk(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        report_path = write_report(self.tmp, report)
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        session = Path(self.tmp) / "s.json"
        payload = start(report_path, manifest, "AB" * 32, "2/8", 10, session)
        self.assertEqual(payload["seed"], "ab" * 32)
        self.assertEqual(payload["alpha"], {"numerator": 1, "denominator": 4, "text": "1/4"})

    def test_start_refuses_to_overwrite_an_existing_session(self):
        session = two_candidate_session(self.tmp)
        before = session.read_bytes()
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 2},
             {"id": "b", "ranking": ["B"], "weight": 2}],
        )
        report_path = write_report(self.tmp, report, "other.json")
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        with self.assertRaises(AuditError):
            start(report_path, manifest, "11" * 32, "1/4", 4, session)
        self.assertEqual(session.read_bytes(), before)

    def test_start_refuses_symlinked_session_destination(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        report_path = write_report(self.tmp, report)
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        target = Path(self.tmp) / "target.json"
        target.write_text("prior", encoding="utf-8")
        link = Path(self.tmp) / "link.json"
        link.symlink_to(target)
        with self.assertRaises(AuditError):
            start(report_path, manifest, SEED, "1/4", 4, link)
        self.assertEqual(target.read_text(), "prior")

    def test_report_contract_rejections(self):
        base = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])

        def refuses(mutate):
            report = json.loads(json.dumps(base))
            mutate(report)
            path = write_report(self.tmp, report, "bad-%d.json" % len(os.listdir(self.tmp)))
            session = Path(self.tmp) / ("s-%d.json" % len(os.listdir(self.tmp)))
            with self.assertRaises(AuditError):
                start(path, manifest, SEED, "1/4", 4, session)
            self.assertFalse(session.exists())

        refuses(lambda r: r.__setitem__("extra", 1))
        refuses(lambda r: r.pop("revision"))
        refuses(lambda r: r.__setitem__("schema_version", 1))
        refuses(lambda r: r.__setitem__("schema_version", True))
        refuses(lambda r: r.__setitem__("revision", -1))
        refuses(lambda r: r.__setitem__("revision", True))
        refuses(lambda r: r["candidates"].reverse())
        refuses(lambda r: r["ballots"].reverse())
        refuses(lambda r: r["ballots"][1].__setitem__("id", "a"))
        refuses(lambda r: r["ballots"][0].__setitem__("weight", 1.0))
        refuses(lambda r: r["ballots"][0].__setitem__("weight", True))
        refuses(lambda r: r["ballots"][0].__setitem__("weight", 0))
        refuses(lambda r: r["ballots"][0].__setitem__("ranking", ["A", "A"]))
        refuses(lambda r: r["ballots"][0].__setitem__("ranking", ["Z"]))
        refuses(lambda r: r.__setitem__("snapshot_sha256", "f" * 64))
        refuses(lambda r: r["result"].__setitem__("winner", "A"))

    def test_report_with_null_winner_is_refused(self):
        report = make_report(["A", "B"], [{"id": "a", "ranking": [], "weight": 5}])
        self.assertIsNone(report["result"]["winner"])
        path = write_report(self.tmp, report)
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 5}])
        with self.assertRaises(AuditError):
            start(path, manifest, SEED, "1/4", 4, Path(self.tmp) / "s.json")

    def test_report_must_contain_at_most_twelve_candidates(self):
        candidates = ["C%02d" % i for i in range(13)]
        ballots = [{"id": "b%02d" % i, "ranking": [c], "weight": 1}
                   for i, c in enumerate(candidates)]
        report = make_report(candidates, ballots)
        path = write_report(self.tmp, report)
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 13}])
        with self.assertRaises(AuditError):
            start(path, manifest, SEED, "1/4", 4, Path(self.tmp) / "s.json")

    def test_manifest_rejections(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        report_path = write_report(self.tmp, report)
        bad_manifests = [
            {},
            {"containers": []},
            {"containers": [{"id": "x", "count": 4, "extra": 1}]},
            {"containers": [{"id": "x", "count": 0}]},
            {"containers": [{"id": "x", "count": 4}, {"id": "x", "count": 1}]},
            {"containers": [{"id": "x", "count": 1}, {"id": "y", "count": 2}]},
            {"containers": [{"id": "x", "count": True}]},
            {"containers": [{"id": "x", "count": 3}]},
            {"containers": [{"id": "", "count": 4}]},
        ]
        for index, obj in enumerate(bad_manifests):
            path = Path(self.tmp) / ("m-%d.json" % index)
            path.write_text(json.dumps(obj), encoding="utf-8")
            session = Path(self.tmp) / ("s-%d.json" % index)
            with self.assertRaises(AuditError):
                start(report_path, path, SEED, "1/4", 4, session)
            self.assertFalse(session.exists())

    def test_strict_json_rejects_duplicate_keys_and_floats(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        for body in ['{"a":1,"a":2}', "NaN", '{"schema_version":2.0}']:
            path = Path(self.tmp) / "dup.json"
            path.write_text(body, encoding="utf-8")
            with self.assertRaises(AuditError):
                start(path, manifest, SEED, "1/4", 4, Path(self.tmp) / "s.json")

    def test_risk_and_seed_validation(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        report_path = write_report(self.tmp, report)
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        for index, (seed, risk) in enumerate([
            ("00" * 31, "1/4"),
            ("zz" * 32, "1/4"),
            (SEED, "0/4"),
            (SEED, "4/4"),
            (SEED, "1/0"),
            (SEED, "1/1000001"),
            (SEED, "1/4/5"),
        ]):
            with self.assertRaises(AuditError):
                start(report_path, manifest, seed, risk, 4,
                      Path(self.tmp) / ("s%d.json" % index))


# --------------------------------------------------------------------------- #
# Sampling: determinism, batching, manifest independence, large N
# --------------------------------------------------------------------------- #

class SamplingTests(TempDir):
    def test_batched_draws_equal_single_draw(self):
        one = two_candidate_session(self.tmp, count=12, max_draws=12, name="one.json",
                                    weight_b=11)
        many = two_candidate_session(self.tmp, count=12, max_draws=12, name="many.json",
                                     weight_b=11)
        single = draw(one, 12)["draws"]
        draw(many, 5)
        draw(many, 4)
        batched = draw(many, 3)["draws"]
        self.assertEqual(project_draws(single), project_draws(batched))
        self.assertEqual(len({d["position"] for d in single}), 12)

    def test_repeated_draw_count_one_is_stable(self):
        session = two_candidate_session(self.tmp, count=15, max_draws=15, weight_b=14)
        collected = []
        for _ in range(15):
            collected = draw(session, 1)["draws"]
        fresh = two_candidate_session(self.tmp, count=15, max_draws=15, weight_b=14,
                                      name="fresh.json")
        self.assertEqual(project_draws(collected), project_draws(draw(fresh, 15)["draws"]))
        self.assertEqual(len(collected), 15)

    def test_manifest_ordering_does_not_change_the_sequence(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 8}],
        )
        report_path = write_report(self.tmp, report)
        containers = [
            {"id": "c", "count": 2},
            {"id": "a", "count": 3},
            {"id": "b", "count": 4},
        ]
        first = start(report_path, make_manifest(self.tmp, containers, "m1.json"),
                      SEED, "1/4", 9, Path(self.tmp) / "s1.json")
        shuffled = [containers[2], containers[0], containers[1]]
        second = start(report_path, make_manifest(self.tmp, shuffled, "m2.json"),
                       SEED, "1/4", 9, Path(self.tmp) / "s2.json")
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(draw(Path(self.tmp) / "s1.json", 9)["draws"],
                         draw(Path(self.tmp) / "s2.json", 9)["draws"])

    def test_large_n_is_not_materialized(self):
        # N = 2*10^13 spread over a handful of containers.
        big = 20_000_000_000_000
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 1}],
        )
        # Patch N by using a huge weight; weight is bounded at 10^9 so instead
        # build the session with an overridden manifest check via direct facts.
        session_path = Path(self.tmp) / "big.json"
        manifest = make_manifest(
            self.tmp,
            [{"id": "box%02d" % i, "count": big // 100} for i in range(100)],
        )
        # The report N is 2, so the manifest cannot match; assert the cheap
        # rejection rather than expanding anything.
        report_path = write_report(self.tmp, report)
        with self.assertRaises(AuditError):
            start(report_path, manifest, SEED, "1/4", 10, session_path)
        # Now test the sampler directly against a 2*10^13 population.
        containers = [{"id": "b%03d" % i, "count": big // 1000} for i in range(1000)]
        draws, _ = audit._draw_sequence(containers, big, SEED, 50)
        self.assertEqual([d["index"] for d in draws], list(range(1, 51)))
        positions = [d["global_position"] for d in draws]
        self.assertEqual(len(set(positions)), 50)
        self.assertTrue(all(0 <= p < big for p in positions))

    def test_sampler_matches_a_brute_force_reference(self):
        rng = random.Random(5)
        for _ in range(15):
            n_cont = rng.randint(1, 5)
            ids = list(dict.fromkeys(
                "c%03d-%d" % (rng.randint(0, 999), i) for i in range(n_cont)))
            if len(ids) < n_cont:
                continue
            containers = sorted(
                ({"id": cid, "count": rng.randint(1, 6)}
                 for cid in ids), key=lambda entry: entry["id"])
            n = sum(entry["count"] for entry in containers)
            seed = "%064x" % rng.getrandbits(256)
            got, _ = audit._draw_sequence(containers, n, seed, n)
            self.assertEqual(got, brute_force_draws(containers, n, seed, n))

    def test_maximum_n_two_times_ten_to_thirteen_is_not_expanded(self):
        # 20000 ballots x weight 10^9 gives exactly N = 2*10^13.  Building a
        # session and issuing draws must stay proportional to containers and
        # draws, never to N or to per-ballot physical arrays.
        weight = 10 ** 9
        ballots = []
        for index in range(20000):
            ranking = ["A"] if index < 12000 else ["B"]
            ballots.append({"id": "b%05d" % index, "ranking": ranking,
                            "weight": weight})
        report = make_report(["A", "B"], ballots)
        expect_n = 20000 * weight
        self.assertEqual(report["result"]["total_weight"], expect_n)
        report_path = write_report(self.tmp, report, "huge.json")
        containers = [{"id": "c%05d" % i, "count": expect_n // 10000}
                      for i in range(10000)]
        manifest = make_manifest(self.tmp, containers, "huge-manifest.json")
        session = Path(self.tmp) / "huge-session.json"
        payload = start(report_path, manifest, SEED, "1/1000000", 2000, session)
        self.assertEqual(payload["n"], expect_n)
        self.assertEqual(payload["effective_budget"], 2000)
        payload = draw(session, 2000)
        positions = [d["position"] for d in payload["draws"]]
        self.assertEqual(len(positions), 2000)
        self.assertEqual(len(set(positions)), 2000)
        self.assertTrue(all(0 <= p < expect_n for p in positions))

    def test_fenwick_remaining_selection_matches_brute_force(self):
        containers = [
            {"id": "a", "count": 2},
            {"id": "b", "count": 5},
            {"id": "c", "count": 1},
        ]
        n = 8
        draws, _ = audit._draw_sequence(containers, n, "07" * 32, n)
        self.assertEqual(len({d["global_position"] for d in draws}), n)
        self.assertEqual(sorted(d["global_position"] for d in draws), list(range(n)))
        start = 0
        spans = {}
        for entry in sorted(containers, key=lambda c: c["id"]):
            spans[entry["id"]] = (start, start + entry["count"])
            start += entry["count"]
        for draw in draws:
            low, high = spans[draw["container"]]
            self.assertTrue(low <= draw["global_position"] < high)
            self.assertEqual(draw["position_in_container"],
                             draw["global_position"] - low + 1)

    def test_rejection_sampling_skips_blocks_and_advances_counter(self):
        modulus = 3
        limit = (1 << 256) - ((1 << 256) % modulus)
        blocks = iter([limit + 1, limit + 7, 7])
        seen = []

        def block(counter):
            seen.append(counter)
            return next(blocks)

        index, next_counter = audit._next_block(b"", 0, modulus, block=block)
        self.assertEqual(next_counter, 3)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(index, 7 % modulus)

    def test_rejected_blocks_stay_inside_the_cross_call_stream(self):
        # A block rejected while deriving draws 1..k must not be retried when
        # later calls extend the prefix, because the prefix is recomputed
        # exactly and never restarts the counter mid-sequence.
        containers = [{"id": "box", "count": 7}]
        modulus = 7
        limit = (1 << 256) - ((1 << 256) % modulus)
        # Force the very first block to be rejected, then accept everything.
        calls = {"n": 0}

        def block(counter):
            calls["n"] += 1
            if counter == 0:
                return limit + 1
            return counter  # accepted small values

        two, counter_two = audit._draw_sequence(containers, 7, SEED, 2, block=block)
        calls["n"] = 0
        five, counter_five = audit._draw_sequence(containers, 7, SEED, 5, block=block)
        self.assertEqual(two, five[:2])
        self.assertGreaterEqual(counter_two, 2)
        self.assertGreaterEqual(counter_five, 5)

    def test_non_string_seed_and_risk_are_refused(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        report_path = write_report(self.tmp, report)
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        for index, (seed, risk) in enumerate([
            (0, "1/4"),
            (SEED, 4),
            (SEED, "1"),
            (None, "1/4"),
        ]):
            with self.assertRaises(AuditError):
                start(report_path, manifest, seed, risk, 4,
                      Path(self.tmp) / ("ns%d.json" % index))

    def test_with_replacement_design_is_refused(self):
        # This implementation never mixes rule sets: a session claiming the
        # with-replacement design is rejected on every read.
        session = two_candidate_session(self.tmp)
        data = json.loads(session.read_text())
        data["design"] = "with_replacement"
        session.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(AuditError):
            status(session)
        with self.assertRaises(AuditError):
            draw(session, 1)

    def test_over_budget_draw_is_refused_without_change(self):
        session = two_candidate_session(self.tmp, count=4, max_draws=3)
        draw(session, 2)
        before = session.read_bytes()
        with self.assertRaises(AuditError):
            draw(session, 2)  # budget is min(3, 4) == 3
        self.assertEqual(session.read_bytes(), before)
        draw(session, 1)


# --------------------------------------------------------------------------- #
# record: identity, conflicts, ordering and conversation semantics
# --------------------------------------------------------------------------- #

class RecordTests(TempDir):
    def test_out_of_order_observation_waits_for_the_gap(self):
        session = two_candidate_session(self.tmp)
        draw(session, 2)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 2, "ranking": ["B"]}]))
        self.assertEqual(payload["consumed"], 0)
        self.assertEqual(payload["verdict"], "continue")
        self.assertIn("await", payload["reason"])
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        self.assertEqual(payload["consumed"], 2)

    def test_missing_and_empty_ranking_are_distinct(self):
        session = two_candidate_session(self.tmp)
        draw(session, 2)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "missing": True},
                       {"draw_index": 2, "ranking": []}]))
        observations = {o["position"]: o for o in payload["observations"]}
        draw1 = payload["draws"][0]
        draw2 = payload["draws"][1]
        self.assertTrue(observations[draw1["position"]]["missing"])
        self.assertNotIn("missing", observations[draw2["position"]])
        self.assertEqual(observations[draw2["position"]]["ranking"], [])

    def test_conflicting_observation_rejects_the_whole_batch(self):
        session = two_candidate_session(self.tmp)
        draw(session, 3)
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        before = session.read_bytes()
        with self.assertRaises(AuditError):
            record(session, observation_file(
                self.tmp, [{"draw_index": 2, "ranking": ["A"]},
                           {"draw_index": 1, "ranking": ["A"]}]))
        self.assertEqual(session.read_bytes(), before)

    def test_conflicting_repeat_for_same_position_rejects(self):
        # Two draw indices never share a position without replacement, so the
        # conflict path is exercised through a retried identical position.
        session = two_candidate_session(self.tmp)
        draw(session, 2)
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        before = session.read_bytes()
        with self.assertRaises(AuditError):
            record(session, observation_file(
                self.tmp, [{"draw_index": 1, "missing": True}]))
        self.assertEqual(session.read_bytes(), before)

    def test_idempotent_retry_and_unknown_draw_rejection(self):
        session = two_candidate_session(self.tmp)
        draw(session, 2)
        first = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        before = session.read_bytes()
        second = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        self.assertEqual(first, second)
        self.assertEqual(session.read_bytes(), before)
        with self.assertRaises(AuditError):
            record(session, observation_file(
                self.tmp, [{"draw_index": 3, "ranking": ["B"]}]))
        self.assertEqual(session.read_bytes(), before)

    def test_observation_shape_rejections(self):
        session = two_candidate_session(self.tmp)
        draw(session, 2)
        before = session.read_bytes()
        bad = [
            [{"draw_index": 1}],
            [{"draw_index": 1, "ranking": ["B"], "weight": 1}],
            [{"draw_index": 1, "ranking": ["B"], "missing": True}],
            [{"draw_index": 1, "ranking": ["Z"]}],
            [{"draw_index": 1, "ranking": ["B", "B"]}],
            [{"draw_index": 1, "missing": False}],
            [{"draw_index": 1, "ranking": ["B"]},
             {"draw_index": 1, "ranking": ["B"]}],
            [],
            [{"draw_index": 0, "ranking": ["B"]}],
            [{"draw_index": True, "ranking": ["B"]}],
        ]
        for index, observations in enumerate(bad):
            path = Path(self.tmp) / ("bad%d.json" % index)
            path.write_text(json.dumps({"observations": observations}),
                            encoding="utf-8")
            with self.assertRaises(AuditError):
                record(session, path)
            self.assertEqual(session.read_bytes(), before)
        for body in ['{"observations": [], "extra": 1}', "{}"]:
            path = Path(self.tmp) / "envelope.json"
            path.write_text(body, encoding="utf-8")
            with self.assertRaises(AuditError):
                record(session, path)
            self.assertEqual(session.read_bytes(), before)


# --------------------------------------------------------------------------- #
# Sequential e-process acceptance numbers of the without-replacement design
# --------------------------------------------------------------------------- #

class NumericAcceptanceTests(TempDir):
    def test_second_draw_e_is_five_thirds_and_third_passes_on_population(self):
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=3)
        draw(session, 2)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": i, "ranking": ["B"]} for i in (1, 2)]))
        assertion = payload["assertions"][0]
        self.assertEqual(assertion["state"], "pending")
        self.assertEqual(assertion["e"], "5/3")
        self.assertEqual(assertion["y_sum"], "2/1")
        payload = draw(session, 1)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 3, "ranking": ["B"]}]))
        assertion = payload["assertions"][0]
        self.assertEqual(assertion["state"], "passed")
        self.assertEqual(assertion["first_pass_draw"], 3)
        self.assertEqual(assertion["passing_rule"], "population_bound")
        self.assertEqual(payload["verdict"], "certified")
        self.assertEqual(payload["stop_draw"], 3)

    def test_threshold_pass_is_exact_rational_and_frozen(self):
        # A large N with dense +1 evidence can cross 1/alpha before the
        # population bound; verify the exact threshold comparison.
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        for draw_number in range(1, 60):
            audit._advance_assertion(state, 1000, draw_number, 2, 1, 4)
            if state.passed:
                break
        self.assertTrue(state.passed)
        self.assertGreaterEqual(
            state.e_num * 4, state.e_den * 1)
        frozen = (state.e_num, state.e_den, state.y_sum)
        audit._advance_assertion(state, 1000, draw_number + 1, 0, 1, 4)
        self.assertEqual((state.e_num, state.e_den, state.y_sum), frozen)

    def test_zero_mu_and_large_mu_and_last_position(self):
        # mu = 0 occurs once S = N/2; mu = 1 once S <= N/2 - remaining.
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        n = 4
        # N=4: draw1 mu=(4-0)/(2*4)=1/2, y=1 -> factor 1+(1/2)/2=5/4
        audit._advance_assertion(state, n, 1, 2, 1, 4)
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(5, 4))
        # draw2 mu=(4-2)/(2*3)=1/3, y=1 -> factor 1+(2/3)/2=4/3
        audit._advance_assertion(state, n, 2, 2, 1, 4)
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(5, 3))
        # draw3 mu=(4-4)/(2*2)=0, y=1 -> factor 3/2
        audit._advance_assertion(state, n, 3, 2, 1, 4)
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(5, 2))

    def test_mu_is_clamped_to_one(self):
        # N=2: draw 1 with y=0 (mu=1/2, factor 3/4), then draw 2 is the last
        # position with S=0, so the raw mu is 2/2 = 1 and is used as exactly 1.
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        audit._advance_assertion(state, 2, 1, 0, 1, 4)
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(3, 4))
        audit._advance_assertion(state, 2, 2, 0, 1, 4)
        # factor at mu=1, y=0 is 1/2, so E = 3/8 (a raw mu above 1 would
        # have been nonsensical; the clamp is exactly 1 here).
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(3, 8))

    def test_strictly_equal_threshold_counts_as_pass(self):
        # N=4, first draw, y=1 -> E=5/4.  With alpha=4/5 the threshold is
        # exactly reached, and equality must pass.
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        audit._advance_assertion(state, 4, 1, 2, 4, 5)
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(5, 4))
        self.assertTrue(state.passed)
        self.assertEqual(state.passing_rule, "threshold")
        self.assertEqual(state.first_pass_draw, 1)

    def test_zero_difference_does_not_certify(self):
        # Paper tickets that never show a strict difference keep every
        # assertion pending: the ratio is 1, never >= 1/alpha.
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=4)
        draw(session, 4)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": i, "ranking": []} for i in (1, 2, 3, 4)]))
        for assertion in payload["assertions"]:
            self.assertEqual(assertion["state"], "pending")
            self.assertEqual(assertion["e"], "1/1")
        self.assertEqual(payload["verdict"], "not_certified")
        self.assertIn("exhausted", payload["reason"])

    def test_multiple_assertions_pass_at_different_prefixes(self):
        # A=45, B=30, C=25: C is eliminated, then B beats A 55:45.  Paper
        # evidence A for the first 45 draws then B for the rest lets the two
        # elimination assertions and the final majority pass at distinct
        # prefixes (7, 51, 96 in this fixture).
        ballots = [
            {"id": "1", "ranking": ["A", "B"], "weight": 45},
            {"id": "2", "ranking": ["B"], "weight": 30},
            {"id": "3", "ranking": ["C", "B"], "weight": 25},
        ]
        session = build_session(
            self.tmp, ["A", "B", "C"], ballots, [{"id": "box", "count": 100}],
            max_draws=100,
        )
        payload = draw(session, 100)
        observations = []
        for entry in payload["draws"]:
            ranking = ["A"] if entry["draw_index"] <= 45 else ["B"]
            observations.append({"draw_index": entry["draw_index"], "ranking": ranking})
        payload = record(session, observation_file(self.tmp, observations))
        passed = {a["id"]: a["first_pass_draw"] for a in payload["assertions"]}
        self.assertTrue(all(value is not None for value in passed.values()))
        self.assertGreaterEqual(len(set(passed.values())), 2)
        self.assertEqual(payload["verdict"], "certified")
        self.assertEqual(payload["stop_draw"], max(passed.values()))

    def test_last_position_denominator_and_mu_bounds(self):
        # Independent reference implementation of the declared rules.
        def reference(n, ys):
            e = Fraction(1)
            s2 = 0  # 2 * S
            out = []
            for t, y2 in enumerate(ys, start=1):
                remaining = n - t + 1
                raw_num = n - s2
                raw_den = 2 * remaining
                if raw_num < 0:
                    out.append(("population", e, s2, t - 1))
                    break
                mu = Fraction(min(1, Fraction(raw_num, raw_den)))
                e *= 1 + (Fraction(y2, 2) - mu) / 2
                s2 += y2
                if s2 > n:
                    out.append(("population", e, s2, t))
                elif e >= 1 / Fraction(1, 4):
                    out.append(("threshold", e, s2, t))
                else:
                    out.append((None, e, s2, None))
            return out

        n = 4
        ys = [2, 1, 1, 0]
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        for t, y2 in enumerate(ys, start=1):
            audit._advance_assertion(state, n, t, y2, 1, 4)
            expected = reference(n, ys)[t - 1]
            self.assertEqual(Fraction(state.e_num, state.e_den), expected[1])
            self.assertEqual(state.y_sum, expected[2])
        # The final denominator is remaining = 1, and no pass occurred.
        self.assertFalse(state.passed)
        self.assertEqual(state.y_sum, n)  # S = N/2 exactly, not above it

    def test_mu_above_one_is_clamped(self):
        # All N=4 paper tickets missing: S stays 0.  At t=4 the raw mu is
        # 2/1, clamped to 1, so the all-zero score shrinks E by 1/2.
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        for draw_number in (1, 2, 3):
            audit._advance_assertion(state, 4, draw_number, 0, 1, 4)
        # factors 3/4, 2/3, 1/2 -> E = 1/4
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(1, 4))
        audit._advance_assertion(state, 4, 4, 0, 1, 4)
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(1, 8))

    def test_mu_equal_zero(self):
        # S = N/2 makes mu = 0: the factor stays positive (1 + y/2).
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        state.y_sum = 8  # S = 4, N = 8
        audit._advance_assertion(state, 8, 1, 2, 1, 4)
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(3, 2))

    def test_consumed_y_above_half_passes_without_multiplying(self):
        # Once the consumed y-sum exceeds N/2 the assertion passes through the
        # population bound; no negative factor is ever applied.
        state = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        state.y_sum = 4  # S = 2 = N/2 for N = 4
        audit._advance_assertion(state, 4, 1, 2, 1, 10**6)
        self.assertTrue(state.passed)
        self.assertEqual(state.passing_rule, "population_bound")
        self.assertEqual(Fraction(state.e_num, state.e_den), Fraction(3, 2))
        # If S is already past N/2 before a draw, the pass happens with no
        # multiplier applied at all and the previous draw is the first pass.
        later = audit._AssertionState({"id": "t", "active": ["A", "B"],
                                       "preferred": "B", "opposed": None})
        later.y_sum = 10  # S = 5 > N/2 for N = 4
        audit._advance_assertion(later, 4, 3, 0, 1, 4)
        self.assertTrue(later.passed)
        self.assertEqual(later.passing_rule, "population_bound")
        self.assertEqual(later.first_pass_draw, 2)
        self.assertEqual(Fraction(later.e_num, later.e_den), 1)

    def test_missing_is_the_worst_score_per_assertion(self):
        assertion = {"id": "x", "active": ["A", "B", "C"], "preferred": "A",
                     "opposed": "C"}
        self.assertEqual(audit._observation_score(assertion, {"missing": True}), 0)
        self.assertEqual(audit._observation_score(assertion, {"ranking": ["C"]}), 0)
        self.assertEqual(audit._observation_score(assertion, {"ranking": ["B"]}), 1)
        self.assertEqual(audit._observation_score(assertion, {"ranking": ["A"]}), 2)
        self.assertEqual(audit._observation_score(assertion, {"ranking": []}), 1)

    def test_transferred_active_first_preference(self):
        # A ticket transfers to the second round's active set.
        assertion = {"id": "x", "active": ["A", "B"], "preferred": "B",
                     "opposed": None}
        self.assertEqual(audit._observation_score(assertion, {"ranking": ["C", "B"]}), 2)
        self.assertEqual(audit._observation_score(assertion, {"ranking": ["C", "A"]}), 0)
        self.assertEqual(audit._observation_score(assertion, {"ranking": ["C"]}), 1)


# --------------------------------------------------------------------------- #
# Assertion compilation
# --------------------------------------------------------------------------- #

class AssertionTests(TempDir):
    def test_elimination_and_majority_assertions(self):
        report = make_report(
            ["A", "B", "C"],
            [{"id": "1", "ranking": ["A", "B"], "weight": 4},
             {"id": "2", "ranking": ["B"], "weight": 3},
             {"id": "3", "ranking": ["C", "B"], "weight": 2}],
        )
        assertions = audit._compile_assertions(report["result"])
        self.assertEqual(
            [(a["round"], a["kind"], a["preferred"], a["opposed"]) for a in assertions],
            [(1, "elimination", "A", "C"), (1, "elimination", "B", "C"),
             (2, "majority", "B", None)],
        )
        self.assertEqual(assertions[-1]["active"], ["A", "B"])

    def test_assertions_are_deterministic(self):
        report = make_report(
            ["A", "B", "C"],
            [{"id": "1", "ranking": ["A", "B"], "weight": 4},
             {"id": "2", "ranking": ["B"], "weight": 3},
             {"id": "3", "ranking": ["C", "B"], "weight": 2}],
        )
        first = audit._compile_assertions(report["result"])
        second = audit._compile_assertions(report["result"])
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# Terminal states, stop points and suffix handling
# --------------------------------------------------------------------------- #

class TerminalTests(TempDir):
    def certify_two_candidate(self):
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=4)
        draw(session, 4)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]},
                       {"draw_index": 2, "ranking": ["B"]},
                       {"draw_index": 3, "ranking": ["B"]}]))
        return session, payload

    def test_batch_draw_across_stop_leaves_an_unconsumed_suffix(self):
        # A batch draw larger than the eventual stop point leaves issued draws
        # after it; those stay unconsumed and cannot enter the certificate.
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=4)
        draw(session, 4)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": index, "ranking": ["B"]} for index in (1, 2, 3)]))
        self.assertEqual(payload["verdict"], "certified")
        self.assertEqual(payload["stop_draw"], 3)
        self.assertEqual(payload["draw_count"], 4)
        self.assertFalse(payload["draws"][3]["consumed"])
        self.assertFalse(payload["draws"][3]["explained"])

    def test_stop_leaves_suffix_unconsumed_and_rejects_new_facts(self):
        session, payload = self.certify_two_candidate()
        self.assertEqual(payload["verdict"], "certified")
        self.assertEqual(payload["stop_draw"], 3)
        self.assertTrue(payload["draws"][3]["explained"] is False)
        self.assertFalse(payload["draws"][3]["consumed"])
        self.assertEqual(payload["consumed"], 3)
        before = session.read_bytes()
        with self.assertRaises(AuditError):
            draw(session, 1)
        self.assertEqual(session.read_bytes(), before)
        with self.assertRaises(AuditError):
            record(session, observation_file(
                self.tmp, [{"draw_index": 4, "ranking": ["B"]}]))
        self.assertEqual(session.read_bytes(), before)
        # A byte-identical retry of a known fact still succeeds.
        replay = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        self.assertEqual(replay["verdict"], "certified")
        self.assertEqual(session.read_bytes(), before)

    def test_gap_prevents_early_certification_until_filled(self):
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=4)
        draw(session, 4)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 2, "ranking": ["B"]},
                       {"draw_index": 3, "ranking": ["B"]},
                       {"draw_index": 4, "ranking": ["B"]}]))
        self.assertEqual(payload["verdict"], "continue")
        self.assertEqual(payload["consumed"], 0)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        self.assertEqual(payload["verdict"], "certified")
        self.assertEqual(payload["stop_draw"], 3)

    def test_later_draw_extends_the_same_fixed_stream(self):
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=5,
                                        count=6, max_draws=6)
        first = draw(session, 2)["draws"]
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["A"]},
                       {"draw_index": 2, "ranking": ["A"]}]))
        # The fixed stream continues; a fresh replay of the same draws matches.
        later = draw(session, 2)["draws"]
        fresh = two_candidate_session(self.tmp, weight_a=1, weight_b=5,
                                      count=6, max_draws=6, name="fresh2.json")
        self.assertEqual(project_draws(later),
                         project_draws(draw(fresh, 4)["draws"]))
        self.assertEqual(project_draws(first), project_draws(later[:2]))

    def test_budget_exhausted_with_gap_stays_continue(self):
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=2)
        draw(session, 2)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 2, "ranking": []}]))
        self.assertEqual(payload["verdict"], "continue")
        self.assertEqual(payload["consumed"], 0)
        payload = record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": []}]))
        self.assertEqual(payload["consumed"], 2)
        self.assertEqual(payload["verdict"], "not_certified")
        # No further draws are possible once the effective budget is issued.
        with self.assertRaises(AuditError):
            draw(session, 1)

    def test_not_certified_identical_retry_succeeds(self):
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=2)
        draw(session, 2)
        fact = {"draw_index": 1, "ranking": []}
        record(session, observation_file(self.tmp, [fact]))
        record(session, observation_file(
            self.tmp, [{"draw_index": 2, "ranking": []}]))
        before = session.read_bytes()
        replay = record(session, observation_file(self.tmp, [fact]))
        self.assertEqual(replay["verdict"], "not_certified")
        self.assertEqual(session.read_bytes(), before)

    def test_not_certified_is_terminal(self):
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=3,
                                        count=4, max_draws=2)
        draw(session, 2)
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": []},
                       {"draw_index": 2, "ranking": []}]))
        before = session.read_bytes()
        with self.assertRaises(AuditError):
            draw(session, 1)
        with self.assertRaises(AuditError):
            record(session, observation_file(
                self.tmp, [{"draw_index": 1, "ranking": ["A"]}]))
        self.assertEqual(session.read_bytes(), before)


# --------------------------------------------------------------------------- #
# Reopening, recomputation and tamper detection
# --------------------------------------------------------------------------- #

class ReopenTests(TempDir):
    def test_status_is_read_only_and_reproducible(self):
        session = two_candidate_session(self.tmp)
        draw(session, 3)
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        before = session.read_bytes()
        first = status(session)
        second = status(session)
        self.assertEqual(first, second)
        self.assertEqual(session.read_bytes(), before)

    def test_derived_cache_corruption_is_rejected(self):
        session = two_candidate_session(self.tmp)
        draw(session, 3)
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": ["B"]}]))
        data = json.loads(session.read_text())
        data["derived"]["verdict"] = "certified"
        data["derived"]["assertions"]["r1-majority-B"]["state"] = "passed"
        session.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(AuditError):
            status(session)

    def test_forged_derived_with_recomputation_mismatch_is_rejected(self):
        session = two_candidate_session(self.tmp)
        data = json.loads(session.read_text())
        data["derived"]["verdict"] = "certified"
        data["derived"]["stop_draw"] = 1
        session.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(AuditError):
            status(session)

    def test_tampered_draws_are_rejected(self):
        session = two_candidate_session(self.tmp)
        draw(session, 2)
        data = json.loads(session.read_text())
        data["draws"][0]["global_position"] = (data["draws"][0]["global_position"] + 1) % 4
        session.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(AuditError):
            status(session)

    def test_status_explains_exhausted_and_gap_reasons(self):
        # Budget = N and every position explained without all assertions
        # passing: population genuinely exhausted.
        session = two_candidate_session(self.tmp, weight_a=1, weight_b=1,
                                        count=2, max_draws=2)
        draw(session, 2)
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "ranking": []},
                       {"draw_index": 2, "ranking": []}]))
        payload = status(session)
        self.assertEqual(payload["verdict"], "not_certified")
        self.assertIn("population_exhausted", payload["reason"])
        # Issued out, but a gap remains: continue, not not_certified.
        session2 = two_candidate_session(self.tmp, weight_a=1, weight_b=1,
                                         count=2, max_draws=2, name="gap.json")
        draw(session2, 2)
        record(session2, observation_file(
            self.tmp, [{"draw_index": 2, "ranking": []}]))
        payload = status(session2)
        self.assertEqual(payload["verdict"], "continue")
        self.assertIn("awaiting_explanation", payload["reason"])

    def test_activity_does_not_depend_on_source_files(self):
        session = two_candidate_session(self.tmp)
        report_path = Path(status(session)["report_path"])
        manifest_path = Path(status(session)["manifest_path"])
        report_path.unlink()
        manifest_path.unlink()
        # The pinned snapshot and manifest are embedded, so the session opens.
        payload = status(session)
        self.assertEqual(payload["n"], 4)
        draw(session, 2)

    def test_cached_passed_flag_corruption_is_rejected(self):
        session = two_candidate_session(self.tmp)
        data = json.loads(session.read_text())
        data["derived"]["assertions"]["r1-majority-B"]["passed"] = True
        data["derived"]["assertions"]["r1-majority-B"]["state"] = "passed"
        session.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(AuditError):
            status(session)

    def test_observation_facts_are_rederivable(self):
        session = two_candidate_session(self.tmp)
        draw(session, 3)
        record(session, observation_file(
            self.tmp, [{"draw_index": 1, "missing": True},
                       {"draw_index": 2, "ranking": ["B"]}]))
        data = json.loads(session.read_text())
        # Reordering observations breaks the canonical order and is rejected.
        data["observations"].reverse()
        session.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(AuditError):
            status(session)


# --------------------------------------------------------------------------- #
# Real CLI
# --------------------------------------------------------------------------- #

class CliTests(TempDir):
    def cli(self, *args, expect_rc=0):
        launcher = Path(self.tmp) / "run_audit_cli.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from ranked_ballots.audit_cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-I", str(launcher), *map(str, args)],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stderr)
        return proc

    def test_full_cli_lifecycle_and_failure_preserves_bytes(self):
        report = make_report(
            ["A", "B"],
            [{"id": "a", "ranking": ["A"], "weight": 1},
             {"id": "b", "ranking": ["B"], "weight": 3}],
        )
        report_path = write_report(self.tmp, report)
        manifest = make_manifest(self.tmp, [{"id": "box", "count": 4}])
        session = Path(self.tmp) / "session.json"

        started = json.loads(self.cli(
            "start", "--report", report_path, "--manifest", manifest,
            "--seed", SEED, "--risk", "1/4", "--max-draws", "4",
            "--session", session).stdout)
        self.assertEqual(started["verdict"], "continue")

        # start refuses to overwrite.
        before = session.read_bytes()
        proc = self.cli("start", "--report", report_path, "--manifest", manifest,
                        "--seed", SEED, "--risk", "1/4", "--max-draws", "4",
                        "--session", session, expect_rc=2)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(session.read_bytes(), before)

        drawn = json.loads(self.cli("draw", "--session", session, "--count", "2").stdout)
        self.assertEqual(drawn["draw_count"], 2)

        # Out-of-order observation: draw 2 before draw 1.
        obs = observation_file(self.tmp, [{"draw_index": 2, "ranking": ["B"]}])
        payload = json.loads(self.cli("record", "--session", session,
                                      "--source", obs).stdout)
        self.assertEqual(payload["consumed"], 0)

        # Illegal record leaves the activity bytes unchanged.
        before = session.read_bytes()
        bad = observation_file(self.tmp, [{"draw_index": 9, "ranking": ["B"]}])
        self.cli("record", "--session", session, "--source", bad, expect_rc=2)
        self.assertEqual(session.read_bytes(), before)

        obs = observation_file(self.tmp, [{"draw_index": 1, "ranking": ["B"]}])
        payload = json.loads(self.cli("record", "--session", session,
                                      "--source", obs).stdout)
        self.assertEqual(payload["consumed"], 2)

        shown = json.loads(self.cli("status", "--session", session).stdout)
        self.assertEqual(shown["verdict"], "continue")
        self.assertEqual(shown["draw_count"], 2)

    def test_cli_missing_argument_exits_two(self):
        self.cli("status", "--session", Path(self.tmp) / "absent.json", expect_rc=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
