"""Tests for candidate-withdrawal sensitivity analysis.

Each scenario is rebuilt independently from the original parsed input (never
from a previous scenario), recomputed with the existing ``analyze`` contract,
and compared field by field with the report.  Contract, byte-determinism,
failure-preservation, atomic publication and maximum-bound behaviour are
covered through both the Python API and real CLI subprocess runs.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))

import ranked_ballots.sensitivity as sensitivity_module  # noqa: E402
from ranked_ballots import sensitivity_analysis  # noqa: E402
from ranked_ballots.model import analyze  # noqa: E402


def scenario_input(data, withdrawn):
    """From-spec scenario: remove the candidate and ranking entries only."""
    ballots = sorted(
        ({"id": b["id"], "ranking": [c for c in b["ranking"] if c != withdrawn],
          "weight": b["weight"]} for b in data["ballots"]),
        key=lambda b: b["id"],
    )
    return {
        "candidates": [c for c in sorted(data["candidates"]) if c != withdrawn],
        "ballots": ballots,
    }


def canonical_input_digest(data):
    ballots = sorted(
        ({"id": b["id"], "ranking": list(b["ranking"]), "weight": b["weight"]}
         for b in data["ballots"]),
        key=lambda b: b["id"],
    )
    raw = json.dumps(
        {"candidates": sorted(data["candidates"]), "ballots": ballots},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


DIGEST = "0" * 64


def expected_report(data, digest=None):
    """Independent from-spec recomputation of the whole report."""
    candidates = sorted(data["candidates"])
    baseline = analyze(data)
    base_last = baseline["rounds"][-1] if baseline["rounds"] else {
        "continuing_weight": 0, "exhausted_weight": 0,
    }
    scenarios = []
    for candidate in candidates:
        scenario = scenario_input(data, candidate)
        if scenario["candidates"]:
            result = analyze(scenario)
        else:
            result = {"winner": None, "rounds": [], "total_weight": 0}
        last = result["rounds"][-1] if result["rounds"] else {
            "continuing_weight": 0, "exhausted_weight": 0,
        }
        scenarios.append({
            "withdrawn_candidate": candidate,
            "input": scenario,
            "result": result,
            "changes": {
                "winner_changed": baseline["winner"] != result["winner"],
                "winner_before": baseline["winner"],
                "winner_after": result["winner"],
                "continuing_weight_delta":
                    last["continuing_weight"] - base_last["continuing_weight"],
                "exhausted_weight_delta":
                    last["exhausted_weight"] - base_last["exhausted_weight"],
            },
        })
    return {
        "format": 1,
        "source_sha256": digest or canonical_input_digest(data),
        "baseline": baseline,
        "scenarios": scenarios,
    }


def acceptance_data():
    """Baseline C wins; withdrawing C flips to B; withdrawing B empties a
    non-empty ranking and raises exhaust without changing the winner."""
    return {
        "candidates": ["A", "B", "C", "D"],
        "ballots": [
            {"id": "b1", "ranking": ["B"], "weight": 10},
            {"id": "b2", "ranking": ["A"], "weight": 6},
            {"id": "b3", "ranking": ["C"], "weight": 3},
            {"id": "b4", "ranking": ["D"], "weight": 1},
        ],
    }


class Acceptance(unittest.TestCase):
    def test_baseline_and_scenarios_match_spec(self):
        data = acceptance_data()
        report = sensitivity_analysis(data)
        self.assertEqual(report, expected_report(data))
        self.assertEqual(
            report["baseline"],
            analyze(data),
            "baseline must be the unmodified existing analyze result",
        )

        baseline = report["baseline"]
        self.assertEqual(baseline["winner"], "B")
        self.assertEqual(baseline["total_weight"], 20)
        self.assertEqual(
            (baseline["rounds"][-1]["continuing_weight"],
             baseline["rounds"][-1]["exhausted_weight"]),
            (19, 1),
        )
        by_candidate = {
            s["withdrawn_candidate"]: s for s in report["scenarios"]
        }
        self.assertEqual(
            [s["withdrawn_candidate"] for s in report["scenarios"]],
            ["A", "B", "C", "D"],
        )

        # Withdrawing the winner B empties a non-empty ranking and flips the
        # winner to A.
        b = by_candidate["B"]
        self.assertEqual(b["input"]["ballots"][0]["ranking"], [])
        self.assertEqual(b["result"]["winner"], "A")
        self.assertTrue(b["changes"]["winner_changed"])
        self.assertEqual(b["changes"]["winner_before"], "B")
        self.assertEqual(b["changes"]["winner_after"], "A")
        self.assertEqual(b["changes"]["continuing_weight_delta"], 10 - 19)
        self.assertEqual(b["changes"]["exhausted_weight_delta"], 10 - 1)

        # Withdrawing A empties another non-empty ranking and raises exhaust,
        # while the winner stays B.
        a = by_candidate["A"]
        self.assertEqual(a["input"]["ballots"][1]["ranking"], [])
        self.assertEqual(a["result"]["winner"], "B")
        self.assertFalse(a["changes"]["winner_changed"])
        self.assertEqual(a["changes"]["exhausted_weight_delta"], 6 - 1)

        # Every scenario retains all ballot ids, weights and group count.
        for scenario in report["scenarios"]:
            self.assertEqual(
                [(x["id"], x["weight"]) for x in scenario["input"]["ballots"]],
                [("b1", 10), ("b2", 6), ("b3", 3), ("b4", 1)],
            )
            self.assertEqual(
                scenario["result"], analyze(scenario["input"])
                if scenario["input"]["candidates"] else
                scenario["result"]
            )

    def test_cross_consistency_every_field(self):
        data = acceptance_data()
        report = sensitivity_analysis(data)
        baseline = report["baseline"]
        base_last = baseline["rounds"][-1]
        for scenario in report["scenarios"]:
            self.assertEqual(
                scenario["input"],
                scenario_input(data, scenario["withdrawn_candidate"]),
            )
            self.assertEqual(scenario["result"], analyze(scenario["input"]))
            changes = scenario["changes"]
            last = scenario["result"]["rounds"][-1]
            self.assertEqual(
                changes["continuing_weight_delta"],
                last["continuing_weight"] - base_last["continuing_weight"],
            )
            self.assertEqual(
                changes["exhausted_weight_delta"],
                last["exhausted_weight"] - base_last["exhausted_weight"],
            )
            self.assertEqual(changes["winner_before"], baseline["winner"])
            self.assertEqual(changes["winner_after"], scenario["result"]["winner"])
            self.assertEqual(
                changes["winner_changed"],
                baseline["winner"] != scenario["result"]["winner"],
            )

    def test_scenarios_are_independent_not_cumulative(self):
        # If removals accumulated, later scenarios would miss earlier
        # candidates; each scenario must remove exactly one candidate.
        data = acceptance_data()
        report = sensitivity_analysis(data)
        for scenario in report["scenarios"]:
            self.assertEqual(
                scenario["input"]["candidates"],
                [c for c in sorted(data["candidates"])
                 if c != scenario["withdrawn_candidate"]],
            )


class Boundaries(unittest.TestCase):
    def test_candidate_absent_from_all_rankings(self):
        data = {
            "candidates": ["A", "B", "C"],
            "ballots": [
                {"id": "x", "ranking": ["A"], "weight": 3},
                {"id": "y", "ranking": ["B"], "weight": 2},
            ],
        }
        report = sensitivity_analysis(data)
        c = report["scenarios"][2]
        self.assertEqual(c["withdrawn_candidate"], "C")
        self.assertEqual(c["input"]["candidates"], ["A", "B"])
        self.assertEqual(c["result"]["winner"], report["baseline"]["winner"])
        self.assertNotIn("C", c["result"]["rounds"][0]["tallies"])

    def test_empty_ranking_group_kept_with_weight(self):
        data = {
            "candidates": ["A", "B"],
            "ballots": [
                {"id": "empty", "ranking": [], "weight": 10**9},
                {"id": "a", "ranking": ["A"], "weight": 1},
            ],
        }
        report = sensitivity_analysis(data)
        for scenario in report["scenarios"]:
            groups = {b["id"]: b for b in scenario["input"]["ballots"]}
            self.assertEqual(groups["empty"],
                             {"id": "empty", "ranking": [], "weight": 10**9})
        self.assertEqual(report["baseline"]["total_weight"], 10**9 + 1)

    def test_max_weight_exact_arithmetic(self):
        data = {
            "candidates": ["A", "B", "C"],
            "ballots": [
                {"id": "g1", "ranking": ["A", "B", "C"], "weight": 10**9},
                {"id": "g2", "ranking": ["B", "C"], "weight": 10**9},
                {"id": "g3", "ranking": ["C", "A"], "weight": 10**9},
            ],
        }
        report = sensitivity_analysis(data)
        self.assertEqual(report["baseline"]["total_weight"], 3 * 10**9)
        for scenario in report["scenarios"]:
            self.assertEqual(scenario["input"]["ballots"][0]["weight"], 10**9)
            self.assertIsInstance(
                scenario["changes"]["exhausted_weight_delta"], int)

    def test_scenario_empty_candidate_path_uses_existing_analyze(self):
        # The empty-candidate special case is only for the one-candidate input;
        # every other scenario result must equal existing analyze exactly.
        for data in (acceptance_data(), {
            "candidates": ["A", "B"],
            "ballots": [{"id": "x", "ranking": [], "weight": 4}],
        }):
            report = sensitivity_analysis(data)
            for scenario in report["scenarios"]:
                self.assertEqual(scenario["result"], analyze(scenario["input"]))

    def test_single_candidate_withdrawal_is_empty_ledger(self):
        data = {
            "candidates": ["A"],
            "ballots": [
                {"id": "x", "ranking": ["A"], "weight": 7},
                {"id": "y", "ranking": [], "weight": 2},
            ],
        }
        report = sensitivity_analysis(data)
        self.assertEqual(report["baseline"], analyze(data))
        self.assertEqual(len(report["scenarios"]), 1)
        scenario = report["scenarios"][0]
        self.assertEqual(scenario["input"],
                         {"candidates": [], "ballots": [
                             {"id": "x", "ranking": [], "weight": 7},
                             {"id": "y", "ranking": [], "weight": 2}]})
        self.assertEqual(scenario["result"],
                         {"winner": None, "rounds": [], "total_weight": 0})
        self.assertTrue(scenario["changes"]["winner_changed"])
        self.assertEqual(scenario["changes"]["winner_after"], None)
        self.assertEqual(scenario["changes"]["continuing_weight_delta"], -7)
        self.assertEqual(scenario["changes"]["exhausted_weight_delta"], -2)

    def test_no_ballots_baseline_and_scenarios(self):
        data = {"candidates": ["A", "B"], "ballots": []}
        report = sensitivity_analysis(data)
        self.assertEqual(report["baseline"]["winner"], None)
        self.assertEqual(len(report["baseline"]["rounds"]), 1)
        self.assertEqual(report["baseline"]["rounds"][0]["outcome"],
                         "no_continuing_ballots")
        for scenario in report["scenarios"]:
            self.assertEqual(scenario["result"]["winner"], None)
            self.assertEqual(scenario["changes"]["continuing_weight_delta"], 0)
            self.assertEqual(scenario["changes"]["exhausted_weight_delta"], 0)


class Validation(unittest.TestCase):
    def bad(self, data):
        with self.assertRaises((ValueError, TypeError)):
            sensitivity_analysis(data)

    def base(self):
        return {
            "candidates": ["A", "B"],
            "ballots": [
                {"id": "x", "ranking": ["A", "B"], "weight": 1},
                {"id": "y", "ranking": [], "weight": 2},
            ],
        }

    def test_valid(self):
        self.assertEqual(sensitivity_analysis(self.base()),
                         expected_report(self.base()))

    def test_top_level_contract(self):
        self.bad({})
        self.bad(["candidates", "ballots"])
        data = self.base()
        data["extra"] = 1
        self.bad(data)
        data = self.base()
        del data["ballots"]
        self.bad(data)

    def test_candidates_contract(self):
        for candidates in ([], ["A", "A"], [f"c{i}" for i in range(101)],
                           ["A", 1], [" A"], "AB"):
            data = self.base()
            data["candidates"] = candidates
            self.bad(data)

    def test_ballots_contract(self):
        data = self.base()
        data["ballots"][0]["note"] = 1
        self.bad(data)
        data = self.base()
        del data["ballots"][0]["weight"]
        self.bad(data)
        data = self.base()
        data["ballots"] = [data["ballots"][0]] * 2  # duplicate ids
        self.bad(data)
        for bad_id in ("", " x", "x " * 65, 1, True):
            data = self.base()
            data["ballots"][0]["id"] = bad_id
            self.bad(data)

    def test_ranking_contract(self):
        for ranking in (["A", "A"], ["A", "Z"], ["A", 1],
                        ["A"] * 101):
            data = self.base()
            data["ballots"][0]["ranking"] = ranking
            self.bad(data)
        data = self.base()
        data["ballots"][0]["ranking"] = "AB"
        self.bad(data)

    def test_weight_contract(self):
        for weight in (True, False, 0, -1, 10**9 + 1, 1.5, 2.0, "3", None):
            data = self.base()
            data["ballots"][0]["weight"] = weight
            self.bad(data)

    def test_bound_counts(self):
        data = self.base()
        data["ballots"] = [
            {"id": f"b{i}", "ranking": [], "weight": 1} for i in range(20_001)
        ]
        self.bad(data)

    def test_digest_is_canonical_input_digest(self):
        report = sensitivity_analysis(self.base())
        self.assertEqual(report["source_sha256"],
                         canonical_input_digest(self.base()))
        reordered = {**self.base(), "ballots": list(reversed(
            self.base()["ballots"]))}
        self.assertEqual(sensitivity_analysis(reordered)["source_sha256"],
                         report["source_sha256"])


class Determinism(unittest.TestCase):
    def test_candidate_and_ballot_order_do_not_change_report_content(self):
        data = acceptance_data()
        first = sensitivity_analysis(data)
        permuted = {
            "candidates": list(reversed(data["candidates"])),
            "ballots": list(reversed(data["ballots"])),
        }
        second = sensitivity_analysis(permuted)
        self.assertEqual(first, second)

    def test_ranking_order_follows_analyze(self):
        forward = {
            "candidates": ["A", "B", "C"],
            "ballots": [
                {"id": "g1", "ranking": ["A", "B", "C"], "weight": 4},
                {"id": "g2", "ranking": ["B", "C", "A"], "weight": 3},
                {"id": "g3", "ranking": ["C", "A", "B"], "weight": 3},
            ],
        }
        backward = {
            "candidates": ["A", "B", "C"],
            "ballots": [
                {"id": "g1", "ranking": ["C", "B", "A"], "weight": 4},
                {"id": "g2", "ranking": ["A", "C", "B"], "weight": 3},
                {"id": "g3", "ranking": ["B", "A", "C"], "weight": 3},
            ],
        }
        r1 = sensitivity_analysis(forward)
        r2 = sensitivity_analysis(backward)
        self.assertEqual(r1, expected_report(forward))
        self.assertEqual(r2, expected_report(backward))
        self.assertNotEqual(r1, r2)


class RandomizedConsistency(unittest.TestCase):
    def test_random_instances_match_from_spec_report(self):
        import random
        rng = random.Random(20261118)
        pool = ["A", "B", "C", "D", "E", "F"]
        for _ in range(80):
            candidates = rng.sample(pool, rng.randrange(1, 6))
            ballots = []
            for index in range(rng.randrange(0, 15)):
                ranking = rng.sample(candidates, rng.randrange(len(candidates) + 1))
                ballots.append({
                    "id": f"b{index}",
                    "ranking": ranking,
                    "weight": rng.choice([1, 2, 5, 10**9,
                                          rng.randrange(1, 100)]),
                })
            data = {"candidates": candidates, "ballots": ballots}
            report = sensitivity_analysis(data)
            self.assertEqual(report, expected_report(data))
            # The count-engine shortcut must equal a full analyze call on the
            # emitted scenario input for every attempt, including ties,
            # exhaustion and single-candidate withdrawals.
            for scenario in report["scenarios"]:
                if scenario["input"]["candidates"]:
                    self.assertEqual(scenario["result"], analyze(scenario["input"]))


class Scale(unittest.TestCase):
    def test_hundred_candidates_twenty_thousand_groups(self):
        candidates = [f"c{i:03d}" for i in range(100)]
        ballots = []
        for i in range(12_000):
            ballots.append({
                "id": f"big{i:05d}", "ranking": ["c000"], "weight": 10**9,
            })
        for i in range(8_000):
            ballots.append({
                "id": f"small{i:05d}", "ranking": [], "weight": 1,
            })
        data = {"candidates": candidates, "ballots": ballots}
        report = sensitivity_analysis(data)
        self.assertEqual(report["baseline"]["total_weight"],
                         12_000 * 10**9 + 8_000)
        self.assertEqual(report["baseline"]["winner"], "c000")
        self.assertEqual(len(report["scenarios"]), 100)
        self.assertEqual(
            [s["withdrawn_candidate"] for s in report["scenarios"]], candidates
        )
        for index, scenario in enumerate(report["scenarios"]):
            self.assertEqual(len(scenario["input"]["ballots"]), 20_000)
            self.assertEqual(
                sum(b["weight"] for b in scenario["input"]["ballots"]),
                12_000 * 10**9 + 8_000,
            )
            if index == 0:
                self.assertIsNone(scenario["result"]["winner"])
                self.assertEqual(scenario["result"]["total_weight"],
                                 12_000 * 10**9 + 8_000)
                self.assertEqual(
                    scenario["changes"]["exhausted_weight_delta"],
                    12_000 * 10**9,
                )
            else:
                self.assertEqual(scenario["result"]["winner"], "c000")
                self.assertEqual(
                    scenario["result"]["total_weight"],
                    12_000 * 10**9 + 8_000,
                )
                self.assertEqual(
                    scenario["changes"]["winner_changed"], False)


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, value):
        path = self.tmp / name
        if isinstance(value, (dict, list)):
            path.write_text(json.dumps(value), encoding="utf-8")
        else:
            path.write_text(value, encoding="utf-8")
        return path

    def cli(self, *args, expect_rc=0):
        launcher = self.tmp / "run_sensitivity_cli.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from ranked_ballots.sensitivity_cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-I", str(launcher), *map(str, args)],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stderr)
        return proc


class CliTests(TempDir):
    def test_success_canonical_bytes_and_replay(self):
        data = acceptance_data()
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        proc = self.cli("--source", src, "--output", out)
        raw = out.read_bytes()
        self.assertEqual(json.loads(raw), json.loads(proc.stdout))
        self.assertEqual(
            raw,
            (json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False) + "\n").encode(),
        )
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        payload = json.loads(raw)
        self.assertEqual(set(payload),
                         {"format", "source_sha256", "baseline", "scenarios"})
        self.assertEqual(payload["format"], 1)
        self.assertEqual(payload["source_sha256"], canonical_input_digest(data))
        self.assertEqual(payload, expected_report(data))
        self.assertEqual(src.read_bytes(), json.dumps(data).encode())
        self.cli("--source", src, "--output", out)
        self.assertEqual(out.read_bytes(), raw)

    def test_reordered_input_report_bytes_identical(self):
        # Reordering the candidates/ballots arrays is the same valid input, so
        # the entire canonical report is byte-for-byte identical.
        data = acceptance_data()
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        self.cli("--source", src, "--output", out)
        first = json.loads(out.read_bytes())
        permuted = {
            "candidates": list(reversed(data["candidates"])),
            "ballots": list(reversed(data["ballots"])),
        }
        src2 = self.write("in2.json", permuted)
        out2 = self.tmp / "out2.json"
        self.cli("--source", src2, "--output", out2)
        self.assertEqual(out.read_bytes(), out2.read_bytes())

    def test_invalid_inputs_preserve_output(self):
        good = self.write("good.json", acceptance_data())
        out = self.tmp / "out.json"
        self.cli("--source", good, "--output", out)
        before = out.read_bytes()
        bodies = [
            "{}",
            '{"candidates":["A"],"ballots":[],"candidates":["B"]}',
            '{"candidates":["A"],"ballots":[],"extra":1}',
            '{"candidates":[],"ballots":[]}',
            '{"candidates":["A","A"],"ballots":[]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":[],"weight":true}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":[],"weight":0}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":[],"weight":1000000001}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["A","A"],"weight":1}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["Z"],"weight":1}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":[],"weight":1},'
            '{"id":"x","ranking":[],"weight":1}]}',
            '{"candidates":["A"],"ballots":[{"id":"","ranking":[],"weight":1}]}',
            "NaN",
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":[],"weight":NaN}]}',
            "not json",
            "",
        ]
        for index, body in enumerate(bodies):
            bad = self.write(f"bad{index}.json", body)
            proc = self.cli("--source", bad, "--output", out, expect_rc=2)
            self.assertTrue(proc.stderr.strip())
            self.assertEqual(out.read_bytes(), before, body)
        self.cli("--source", self.tmp / "missing.json", "--output", out,
                 expect_rc=2)
        self.assertEqual(out.read_bytes(), before)
        big = self.write("big.json", " " * 8_000_001)
        self.cli("--source", big, "--output", out, expect_rc=2)
        self.assertEqual(out.read_bytes(), before)

    def test_same_path_and_symlink_output_refused(self):
        src = self.write("in.json", acceptance_data())
        before = src.read_bytes()
        self.cli("--source", src, "--output", src, expect_rc=2)
        self.assertEqual(src.read_bytes(), before)
        link = self.tmp / "link.json"
        link.symlink_to(src)
        self.cli("--source", src, "--output", link, expect_rc=2)
        self.assertEqual(src.read_bytes(), before)
        dangling = self.tmp / "dangling.json"
        dangling.symlink_to(self.tmp / "nowhere.json")
        self.cli("--source", src, "--output", dangling, expect_rc=2)
        self.assertFalse((self.tmp / "nowhere.json").exists())


class AtomicPublication(TempDir):
    def test_replace_failure_preserves_output_and_cleans_temp(self):
        data = acceptance_data()
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        out.write_bytes(b"prior-bytes")
        with mock.patch.object(sensitivity_module.os, "replace",
                               side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                sensitivity_module.sensitivity_report(src, out)
        self.assertEqual(out.read_bytes(), b"prior-bytes")
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if p.name.startswith(".sensitivity-")],
            [],
        )

    def test_verification_failure_preserves_output(self):
        data = acceptance_data()
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        out.write_bytes(b"prior-bytes")
        with mock.patch.object(sensitivity_module, "_verify_scenario",
                               side_effect=ValueError("simulated check failure")):
            with self.assertRaises(ValueError):
                sensitivity_module.sensitivity_report(src, out)
        self.assertEqual(out.read_bytes(), b"prior-bytes")
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if p.name.startswith(".sensitivity-")],
            [],
        )
        # The source itself is untouched.
        self.assertEqual(json.loads(src.read_text()), data)

    def test_streaming_scenario_failure_midway_preserves_output(self):
        # Streaming publication writes chunks as scenarios are produced; a
        # failure after some scenarios must still discard the temp file.
        data = acceptance_data()
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        out.write_bytes(b"prior-bytes")
        real = sensitivity_module._scenario
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("simulated count failure")
            return real(*args, **kwargs)

        with mock.patch.object(sensitivity_module, "_scenario", flaky):
            with self.assertRaises(RuntimeError):
                sensitivity_module.sensitivity_report(src, out, return_report=False)
        self.assertEqual(out.read_bytes(), b"prior-bytes")
        self.assertEqual(
            [p.name for p in self.tmp.iterdir() if p.name.startswith(".sensitivity-")],
            [],
        )

    def test_staged_readback_failure_preserves_output(self):
        data = acceptance_data()
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        out.write_bytes(b"prior-bytes")
        real_open = open

        class BadStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.stream.close()

            def read(self, size=-1):
                # Corrupt the staged bytes once, then signal EOF.
                if getattr(self, "done", False):
                    return b""
                self.done = True
                return b"corrupted"

        def fake_open(path, mode="r", *args, **kwargs):
            stream = real_open(path, mode, *args, **kwargs)
            if mode == "rb" and str(path).startswith(str(self.tmp)) \
                    and Path(path).name.startswith(".sensitivity-"):
                return BadStream(stream)
            return stream

        with mock.patch.object(sensitivity_module, "open", fake_open, create=True):
            with self.assertRaises(OSError):
                sensitivity_module.sensitivity_report(src, out)
        self.assertEqual(out.read_bytes(), b"prior-bytes")


# --------------------------------------------------------------------------- #
# Shared count engine equivalence (defensive regression)
# --------------------------------------------------------------------------- #

def reference_count(candidates, ballots):
    """The original full-rescan IRV loop, kept as an independent oracle."""
    active = set(candidates)
    rounds = []
    winner = None
    while active:
        totals = {c: 0 for c in sorted(active)}
        exhausted = 0
        for ranking, weight in ballots:
            choice = next((c for c in ranking if c in active), None)
            if choice is None:
                exhausted += weight
            else:
                totals[choice] += weight
        continuing = sum(totals.values())
        row = {"round": len(rounds) + 1, "tallies": totals,
               "continuing_weight": continuing, "exhausted_weight": exhausted,
               "eliminated": None}
        majority = [c for c in active if totals[c] * 2 > continuing]
        if continuing == 0:
            row["outcome"] = "no_continuing_ballots"
            rounds.append(row)
            break
        if majority:
            winner = majority[0]
            row["outcome"] = "majority"
            rounds.append(row)
            break
        eliminated = min(active, key=lambda c: (totals[c], c))
        row["eliminated"] = eliminated
        row["outcome"] = "elimination"
        rounds.append(row)
        active.remove(eliminated)
    return {"winner": winner, "rounds": rounds,
            "total_weight": sum(w for r, w in ballots),
            "tie_policy": "single lowest tally, lexicographically smallest ID eliminated"}


class CountEngineEquivalence(unittest.TestCase):
    def test_optimized_engine_matches_full_rescan(self):
        import itertools
        from ranked_ballots.calculation import count
        candidates = ["A", "B", "C"]
        rankings = [list(p) for k in range(4)
                    for p in itertools.permutations(candidates, k)]
        for first in rankings:
            for second in rankings:
                for w1 in (1, 2):
                    for w2 in (1, 2):
                        ballots = [(first, w1), (second, w2)]
                        self.assertEqual(count(candidates, ballots),
                                         reference_count(candidates, ballots))
        import random
        rng = random.Random(99)
        for _ in range(500):
            pool = rng.sample(["A", "B", "C", "D", "E"],
                              rng.randrange(1, 6))
            ballots = [
                (rng.sample(pool, rng.randrange(len(pool) + 1)),
                 rng.randrange(1, 5))
                for _ in range(rng.randrange(0, 4))
            ]
            self.assertEqual(count(pool, ballots),
                             reference_count(pool, ballots))


if __name__ == "__main__":
    unittest.main()
