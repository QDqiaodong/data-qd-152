"""Tests for the independent pairwise preference analysis.

The matrix, strict majority graph, strongly connected components and
Condorcet winner are checked against an independent from-spec recomputation
(dict-of-dict accumulation via ``itertools.combinations`` plus set-based
mutual reachability), hand-computed acceptance examples cover the mandated
cycle/tie/unexpressed/multi-component cases, and real CLI subprocess runs
cover determinism, rejection and failure-preservation paths.
"""
import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))

from ranked_ballots import pairwise_analysis  # noqa: E402


# --------------------------------------------------------------------------- #
# Independent recomputation straight from the specification
# --------------------------------------------------------------------------- #

def expected_result(data):
    candidates = sorted(data["candidates"])
    n = len(candidates)
    total = sum(b["weight"] for b in data["ballots"])
    over = {c: {d: 0 for d in candidates} for c in candidates}
    for ballot in data["ballots"]:
        # combinations preserves ranking order: x is ranked ahead of y.
        for x, y in itertools.combinations(ballot["ranking"], 2):
            over[x][y] += ballot["weight"]

    comparisons = []
    edges = []
    adjacency = {c: set() for c in candidates}
    for a, b in itertools.combinations(candidates, 2):
        ab, ba = over[a][b], over[b][a]
        if ab > ba:
            winner = a
        elif ba > ab:
            winner = b
        else:
            winner = None
        if winner is not None:
            loser = b if winner == a else a
            edges.append({"from": winner, "to": loser})
            adjacency[winner].add(loser)
        comparisons.append({
            "candidate_a": a,
            "candidate_b": b,
            "a_over_b_weight": ab,
            "b_over_a_weight": ba,
            "unexpressed_weight": total - ab - ba,
            "pairwise_winner": winner,
        })
    edges.sort(key=lambda e: (e["from"], e["to"]))

    def reach_from(source):
        seen = {source}
        stack = [source]
        while stack:
            u = stack.pop()
            for v in adjacency[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        return seen

    reach = {c: reach_from(c) for c in candidates}
    unassigned = set(candidates)
    components = []
    for c in candidates:
        if c not in unassigned:
            continue
        group = sorted(v for v in candidates if v in reach[c] and c in reach[v])
        unassigned.difference_update(group)
        components.append(group)
    components.sort(key=lambda g: g[0])

    champions = [c for c in candidates if len(adjacency[c]) == n - 1]
    return {
        "candidates": candidates,
        "total_weight": total,
        "comparisons": comparisons,
        "majority_graph": {"nodes": candidates, "edges": edges},
        "strongly_connected_components": components,
        "condorcet_winner": champions[0] if len(champions) == 1 else None,
    }


def make_ballots(groups):
    return [
        {"id": f"b{i}", "ranking": list(ranking), "weight": weight}
        for i, (ranking, weight) in enumerate(groups)
    ]


# --------------------------------------------------------------------------- #
# Hand-computed acceptance cases
# --------------------------------------------------------------------------- #

class Acceptance(unittest.TestCase):
    def test_cycle_one_component_null_condorcet(self):
        # A>B, B>C, C>A with equal weights: three strict majority edges, one
        # shared strongly connected component, no Condorcet winner.
        data = {
            "candidates": ["A", "B", "C"],
            "ballots": make_ballots([
                (["A", "B"], 4), (["B", "C"], 4), (["C", "A"], 4),
            ]),
        }
        result = pairwise_analysis(data)
        self.assertEqual(result["total_weight"], 12)
        self.assertEqual(
            result["majority_graph"],
            {
                "nodes": ["A", "B", "C"],
                "edges": [
                    {"from": "A", "to": "B"},
                    {"from": "B", "to": "C"},
                    {"from": "C", "to": "A"},
                ],
            },
        )
        self.assertEqual(
            result["strongly_connected_components"], [["A", "B", "C"]]
        )
        self.assertIsNone(result["condorcet_winner"])
        for row in result["comparisons"]:
            self.assertEqual(row["unexpressed_weight"], 8)
        self.assertEqual(result, expected_result(data))

    def test_unexpressed_weight_and_no_phantom_preferences(self):
        # Pairs expressed on only one side, or on neither, must not gain
        # weight for either candidate.
        data = {
            "candidates": ["A", "B", "C", "D"],
            "ballots": make_ballots([
                (["A", "B"], 2),   # expresses A>B only
                (["A"], 3),        # A alone: no pair
                ([], 1),           # empty ranking: no pair
                (["C", "D"], 5),   # expresses C>D only
            ]),
        }
        result = pairwise_analysis(data)
        self.assertEqual(result["total_weight"], 11)
        rows = {
            (row["candidate_a"], row["candidate_b"]): row
            for row in result["comparisons"]
        }
        self.assertEqual(len(rows), 6)
        ab = rows[("A", "B")]
        self.assertEqual(
            (ab["a_over_b_weight"], ab["b_over_a_weight"],
             ab["unexpressed_weight"], ab["pairwise_winner"]),
            (2, 0, 9, "A"),
        )
        cd = rows[("C", "D")]
        self.assertEqual(
            (cd["a_over_b_weight"], cd["b_over_a_weight"],
             cd["unexpressed_weight"], cd["pairwise_winner"]),
            (5, 0, 6, "C"),
        )
        # A and C never share a ranking: both sides zero, fully unexpressed.
        for pair in [("A", "C"), ("A", "D"), ("B", "C"), ("B", "D")]:
            row = rows[pair]
            self.assertEqual(
                (row["a_over_b_weight"], row["b_over_a_weight"],
                 row["unexpressed_weight"], row["pairwise_winner"]),
                (0, 0, 11, None),
            )
        self.assertEqual(
            result["majority_graph"]["edges"],
            [{"from": "A", "to": "B"}, {"from": "C", "to": "D"}],
        )
        self.assertEqual(
            result["strongly_connected_components"],
            [["A"], ["B"], ["C"], ["D"]],
        )
        self.assertIsNone(result["condorcet_winner"])
        self.assertEqual(result, expected_result(data))

    def test_tie_zero_and_strict_margin_of_one(self):
        data = {
            "candidates": ["A", "B", "C"],
            "ballots": make_ballots([
                (["A", "B"], 3),
                (["B", "A"], 3),   # exact tie 3 = 3: no winner, no edge
                (["A", "C"], 2),
                (["C", "A"], 1),   # strict margin of exactly 1: edge A>C
            ]),
        }
        result = pairwise_analysis(data)
        rows = {
            (row["candidate_a"], row["candidate_b"]): row
            for row in result["comparisons"]
        }
        ab = rows[("A", "B")]
        self.assertEqual(
            (ab["a_over_b_weight"], ab["b_over_a_weight"],
             ab["pairwise_winner"]),
            (3, 3, None),
        )
        ac = rows[("A", "C")]
        self.assertEqual(
            (ac["a_over_b_weight"], ac["b_over_a_weight"],
             ac["pairwise_winner"]),
            (2, 1, "A"),
        )
        # Both sides zero is also a tie: no winner, no edge.
        bc = rows[("B", "C")]
        self.assertEqual(
            (bc["a_over_b_weight"], bc["b_over_a_weight"],
             bc["unexpressed_weight"], bc["pairwise_winner"]),
            (0, 0, 9, None),
        )
        self.assertEqual(
            result["majority_graph"]["edges"], [{"from": "A", "to": "C"}]
        )
        self.assertIsNone(result["condorcet_winner"])
        self.assertEqual(result, expected_result(data))

    def test_multiple_components_and_condorcet_above_cycle(self):
        # D strictly beats each of A, B, C and E; A/B/C form a cycle; every
        # active candidate beats E.  Components: {A,B,C}, {D}, {E}; the
        # Condorcet winner is D even though A/B/C cycle.
        data = {
            "candidates": ["A", "B", "C", "D", "E"],
            "ballots": make_ballots([
                (["A", "B"], 1), (["B", "C"], 1), (["C", "A"], 1),
                (["D", "A"], 1), (["D", "B"], 1), (["D", "C"], 1),
                (["A", "E"], 1), (["B", "E"], 1), (["C", "E"], 1),
                (["D", "E"], 1),
            ]),
        }
        result = pairwise_analysis(data)
        self.assertEqual(
            result["majority_graph"]["edges"],
            [
                {"from": "A", "to": "B"},
                {"from": "A", "to": "E"},
                {"from": "B", "to": "C"},
                {"from": "B", "to": "E"},
                {"from": "C", "to": "A"},
                {"from": "C", "to": "E"},
                {"from": "D", "to": "A"},
                {"from": "D", "to": "B"},
                {"from": "D", "to": "C"},
                {"from": "D", "to": "E"},
            ],
        )
        self.assertEqual(
            result["strongly_connected_components"],
            [["A", "B", "C"], ["D"], ["E"]],
        )
        self.assertEqual(result["condorcet_winner"], "D")
        # Independent reachability check on the emitted edges.
        adjacency = {}
        for edge in result["majority_graph"]["edges"]:
            adjacency.setdefault(edge["from"], set()).add(edge["to"])

        def reaches(source):
            seen = {source}
            stack = [source]
            while stack:
                u = stack.pop()
                for v in adjacency.get(u, ()):
                    if v not in seen:
                        seen.add(v)
                        stack.append(v)
            return seen

        for member in ("A", "B", "C"):
            self.assertEqual(reaches(member), {"A", "B", "C", "E"})
        self.assertEqual(reaches("D"), {"A", "B", "C", "D", "E"})
        self.assertEqual(reaches("E"), {"E"})
        self.assertEqual(result, expected_result(data))

    def test_condorcet_unique_and_total_order(self):
        # Total strict order A>B>C>D: four singleton components, A wins all.
        data = {
            "candidates": ["A", "B", "C", "D"],
            "ballots": make_ballots([
                (["A", "B", "C", "D"], 2),
                (["A", "C"], 1),
            ]),
        }
        result = pairwise_analysis(data)
        self.assertEqual(result["condorcet_winner"], "A")
        self.assertEqual(
            result["strongly_connected_components"],
            [["A"], ["B"], ["C"], ["D"]],
        )
        self.assertEqual(len(result["majority_graph"]["edges"]), 6)
        self.assertEqual(result, expected_result(data))


# --------------------------------------------------------------------------- #
# Boundary shapes
# --------------------------------------------------------------------------- #

class Boundaries(unittest.TestCase):
    def test_empty_ballot_list(self):
        data = {"candidates": ["B", "A"], "ballots": []}
        result = pairwise_analysis(data)
        self.assertEqual(result["candidates"], ["A", "B"])
        self.assertEqual(result["total_weight"], 0)
        self.assertEqual(result["comparisons"], [{
            "candidate_a": "A",
            "candidate_b": "B",
            "a_over_b_weight": 0,
            "b_over_a_weight": 0,
            "unexpressed_weight": 0,
            "pairwise_winner": None,
        }])
        self.assertEqual(
            result["majority_graph"], {"nodes": ["A", "B"], "edges": []}
        )
        self.assertEqual(
            result["strongly_connected_components"], [["A"], ["B"]]
        )
        self.assertIsNone(result["condorcet_winner"])

    def test_empty_and_singleton_rankings_express_nothing(self):
        data = {
            "candidates": ["A", "B", "C"],
            "ballots": make_ballots([([], 5), (["A"], 7), (["B"], 1)]),
        }
        result = pairwise_analysis(data)
        self.assertEqual(result["total_weight"], 13)
        for row in result["comparisons"]:
            self.assertEqual(row["a_over_b_weight"], 0)
            self.assertEqual(row["b_over_a_weight"], 0)
            self.assertEqual(row["unexpressed_weight"], 13)
            self.assertIsNone(row["pairwise_winner"])
        self.assertEqual(result["majority_graph"]["edges"], [])
        self.assertEqual(
            result["strongly_connected_components"], [["A"], ["B"], ["C"]]
        )

    def test_single_candidate_is_condorcet_winner(self):
        data = {
            "candidates": ["A"],
            "ballots": make_ballots([(["A"], 7), ([], 3)]),
        }
        result = pairwise_analysis(data)
        self.assertEqual(result["total_weight"], 10)
        self.assertEqual(result["comparisons"], [])
        self.assertEqual(
            result["majority_graph"], {"nodes": ["A"], "edges": []}
        )
        self.assertEqual(result["strongly_connected_components"], [["A"]])
        self.assertEqual(result["condorcet_winner"], "A")

    def test_max_weight_and_exact_large_totals(self):
        # 20000 groups of weight 10^9: the contracted 2*10^13 total, exact.
        ballots = [
            {"id": f"g{i:05d}", "ranking": ["A", "B", "C"], "weight": 10**9}
            for i in range(20_000)
        ]
        data = {"candidates": ["A", "B", "C"], "ballots": ballots}
        result = pairwise_analysis(data)
        self.assertEqual(result["total_weight"], 2 * 10**13)
        for row in result["comparisons"]:
            self.assertEqual(row["unexpressed_weight"], 0)
            self.assertEqual(row["b_over_a_weight"], 0)
            self.assertEqual(row["a_over_b_weight"], 2 * 10**13)
        self.assertEqual(
            [row["pairwise_winner"] for row in result["comparisons"]],
            ["A", "A", "B"],
        )
        self.assertEqual(result["condorcet_winner"], "A")
        self.assertEqual(result, expected_result(data))

    def test_unranked_candidate_blocks_condorcet(self):
        # C never appears on any ballot: A beats B directly but ties C 0=0,
        # so there is no Condorcet winner.
        ballots = [
            {"id": f"g{i:05d}", "ranking": ["A", "B"], "weight": 10**9}
            for i in range(20_000)
        ]
        data = {"candidates": ["A", "B", "C"], "ballots": ballots}
        result = pairwise_analysis(data)
        self.assertEqual(result["total_weight"], 2 * 10**13)
        rows = {
            (row["candidate_a"], row["candidate_b"]): row
            for row in result["comparisons"]
        }
        self.assertEqual(rows[("A", "B")]["a_over_b_weight"], 2 * 10**13)
        self.assertEqual(rows[("A", "B")]["unexpressed_weight"], 0)
        self.assertEqual(rows[("A", "C")]["unexpressed_weight"], 2 * 10**13)
        self.assertEqual(rows[("B", "C")]["unexpressed_weight"], 2 * 10**13)
        self.assertEqual(
            result["majority_graph"]["edges"], [{"from": "A", "to": "B"}]
        )
        self.assertIsNone(result["condorcet_winner"])
        self.assertEqual(result, expected_result(data))

    def test_hundred_candidates_cover_all_pairs(self):
        candidates = [f"c{i:02d}" for i in range(100)]
        data = {"candidates": candidates, "ballots": []}
        result = pairwise_analysis(data)
        self.assertEqual(len(result["comparisons"]), 4950)
        pairs = [
            (row["candidate_a"], row["candidate_b"])
            for row in result["comparisons"]
        ]
        self.assertEqual(pairs, sorted(pairs))
        self.assertEqual(len(set(pairs)), 4950)
        self.assertEqual(len(result["strongly_connected_components"]), 100)
        self.assertIsNone(result["condorcet_winner"])


# --------------------------------------------------------------------------- #
# Validation (mirrors the v1 model rules)
# --------------------------------------------------------------------------- #

class Validation(unittest.TestCase):
    def base(self):
        return {
            "candidates": ["A", "B", "C"],
            "ballots": make_ballots([(["A", "B"], 2), (["C"], 1)]),
        }

    def bad(self, data):
        with self.assertRaises((ValueError, TypeError)):
            pairwise_analysis(data)

    def test_valid_baseline(self):
        self.assertEqual(
            pairwise_analysis(self.base()), expected_result(self.base())
        )

    def test_top_level_fields(self):
        self.bad({})
        self.bad({"candidates": ["A"]})
        self.bad({"ballots": []})
        data = self.base()
        data["extra"] = 1
        self.bad(data)
        self.bad(["candidates", "ballots"])

    def test_candidates_contract(self):
        data = self.base()
        data["candidates"] = []
        self.bad(data)
        data = self.base()
        data["candidates"] = ["A", "A"]
        self.bad(data)
        data = self.base()
        data["candidates"] = [f"c{i}" for i in range(101)]
        self.bad(data)
        data = self.base()
        data["candidates"] = ["A", 1, "B"]
        self.bad(data)
        data = self.base()
        data["candidates"] = [" A", "B"]
        self.bad(data)
        data = self.base()
        data["candidates"] = "AB"
        self.bad(data)

    def test_ballot_fields(self):
        data = self.base()
        data["ballots"][0]["note"] = "x"
        self.bad(data)
        data = self.base()
        del data["ballots"][0]["weight"]
        self.bad(data)
        data = self.base()
        data["ballots"] = [data["ballots"][0]] * 2  # duplicate ballot id
        self.bad(data)
        data = self.base()
        data["ballots"] = [
            {"id": f"b{i}", "ranking": [], "weight": 1} for i in range(20_001)
        ]
        self.bad(data)

    def test_ranking_contract(self):
        data = self.base()
        data["ballots"][0]["ranking"] = ["A", "A"]  # duplicate in ranking
        self.bad(data)
        data = self.base()
        data["ballots"][0]["ranking"] = ["A", "X"]  # unknown candidate
        self.bad(data)
        data = self.base()
        data["ballots"][0]["ranking"] = ["A"] * 101  # too long (and dup)
        self.bad(data)
        data = self.base()
        data["ballots"][0]["ranking"] = ["A", 1]
        self.bad(data)

    def test_weight_contract(self):
        for weight in (True, False, 0, -1, 10**9 + 1, 1.5, 2.0, "3", None):
            data = self.base()
            data["ballots"][0]["weight"] = weight
            self.bad(data)
        data = self.base()
        data["ballots"][0]["weight"] = 10**9
        self.assertEqual(pairwise_analysis(data)["total_weight"], 10**9 + 1)

    def test_identifier_contract(self):
        for bid in ("", " x", "x ", 1, None, True, "x" * 129):
            data = self.base()
            data["ballots"][0]["id"] = bid
            self.bad(data)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #

class Determinism(unittest.TestCase):
    def test_input_permutation_invariant(self):
        rng = random.Random(2026)
        candidates = [f"c{i}" for i in range(7)]
        ballots = []
        for i in range(40):
            ranking = rng.sample(candidates, rng.randrange(0, 8))
            ballots.append({
                "id": f"b{i:02d}", "ranking": ranking,
                "weight": rng.randrange(1, 10**6),
            })
        data = {"candidates": list(candidates), "ballots": ballots}
        result = pairwise_analysis(data)
        for _ in range(10):
            shuffled = {
                "candidates": rng.sample(candidates, len(candidates)),
                "ballots": rng.sample(ballots, len(ballots)),
            }
            self.assertEqual(pairwise_analysis(shuffled), result)

    def test_ranking_order_only_redirects_expressed_pairs(self):
        forward = {
            "candidates": ["A", "B"],
            "ballots": [{"id": "x", "ranking": ["A", "B"], "weight": 5}],
        }
        backward = {
            "candidates": ["A", "B"],
            "ballots": [{"id": "x", "ranking": ["B", "A"], "weight": 5}],
        }
        fa = pairwise_analysis(forward)["comparisons"][0]
        ba = pairwise_analysis(backward)["comparisons"][0]
        self.assertEqual(
            (fa["a_over_b_weight"], fa["b_over_a_weight"],
             fa["pairwise_winner"]),
            (5, 0, "A"),
        )
        self.assertEqual(
            (ba["a_over_b_weight"], ba["b_over_a_weight"],
             ba["pairwise_winner"]),
            (0, 5, "B"),
        )
        self.assertEqual(fa["unexpressed_weight"], ba["unexpressed_weight"])
        self.assertEqual(
            pairwise_analysis(forward)["majority_graph"]["edges"],
            [{"from": "A", "to": "B"}],
        )
        self.assertEqual(
            pairwise_analysis(backward)["majority_graph"]["edges"],
            [{"from": "B", "to": "A"}],
        )


# --------------------------------------------------------------------------- #
# Randomized cross-check against the independent recomputation
# --------------------------------------------------------------------------- #

class Randomized(unittest.TestCase):
    def test_random_instances_match_spec(self):
        rng = random.Random(99)
        for trial in range(60):
            n = rng.randrange(1, 9)
            candidates = rng.sample(
                ["A", "B", "C", "D", "E", "F", "G", "H"], n
            )
            ballots = []
            for i in range(rng.randrange(0, 25)):
                ranking = rng.sample(candidates, rng.randrange(0, n + 1))
                weight = rng.choice([1, 2, 3, 10**9, rng.randrange(1, 10**6)])
                ballots.append({
                    "id": f"t{trial}-b{i}", "ranking": ranking,
                    "weight": weight,
                })
            data = {"candidates": candidates, "ballots": ballots}
            result = pairwise_analysis(data)
            self.assertEqual(result, expected_result(data))
            # Contract invariants on top of the full comparison.
            self.assertEqual(
                len(result["comparisons"]), n * (n - 1) // 2
            )
            seen = set()
            for row in result["comparisons"]:
                pair = (row["candidate_a"], row["candidate_b"])
                self.assertLess(row["candidate_a"], row["candidate_b"])
                self.assertNotIn(pair, seen)
                seen.add(pair)
                self.assertEqual(
                    row["unexpressed_weight"],
                    result["total_weight"]
                    - row["a_over_b_weight"] - row["b_over_a_weight"],
                )
            flat = [
                c for group in result["strongly_connected_components"]
                for c in group
            ]
            self.assertEqual(sorted(flat), sorted(candidates))
            self.assertEqual(
                result["majority_graph"]["nodes"], sorted(candidates)
            )


# --------------------------------------------------------------------------- #
# Scale within the fixed bounds
# --------------------------------------------------------------------------- #

class Scale(unittest.TestCase):
    def test_full_rankings_at_bound(self):
        # 100 candidates x 3000 distinct full rankings (14.85M expressed
        # pairs): every pair is expressed and the totals stay exact.
        candidates = [f"c{i:02d}" for i in range(100)]
        rng = random.Random(7)
        ballots = []
        for i in range(3000):
            ranking = list(candidates)
            rng.shuffle(ranking)
            ballots.append({
                "id": f"g{i:04d}", "ranking": ranking, "weight": 10**9,
            })
        data = {"candidates": candidates, "ballots": ballots}
        result = pairwise_analysis(data)
        self.assertEqual(result["total_weight"], 3 * 10**12)
        self.assertEqual(len(result["comparisons"]), 4950)
        expressed = sum(
            row["a_over_b_weight"] + row["b_over_a_weight"]
            for row in result["comparisons"]
        )
        self.assertEqual(expressed, 4950 * 3 * 10**12)
        for row in result["comparisons"]:
            self.assertEqual(row["unexpressed_weight"], 0)
        self.assertEqual(result, expected_result(data))


# --------------------------------------------------------------------------- #
# Real CLI subprocess runs
# --------------------------------------------------------------------------- #

class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def cli(self, *args, expect_rc=0):
        launcher = self.tmp / "run_pairwise_cli.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from ranked_ballots.pairwise_cli import main\n"
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
    def source(self, data=None, name="in.json", raw=None):
        if raw is not None:
            return self.write(name, raw)
        return self.write(name, json.dumps(data))

    def test_success_cycle_and_byte_determinism(self):
        data = {
            "candidates": ["C", "A", "B"],
            "ballots": [
                {"id": "g3", "ranking": ["C", "A"], "weight": 4},
                {"id": "g1", "ranking": ["A", "B"], "weight": 4},
                {"id": "g2", "ranking": ["B", "C"], "weight": 4},
            ],
        }
        src = self.source(data)
        out = self.tmp / "out.json"
        proc = self.cli("--source", src, "--output", out)
        payload = json.loads(proc.stdout)
        self.assertEqual(json.loads(out.read_bytes()), payload)
        self.assertEqual(
            payload["majority_graph"]["edges"],
            [
                {"from": "A", "to": "B"},
                {"from": "B", "to": "C"},
                {"from": "C", "to": "A"},
            ],
        )
        self.assertEqual(payload["strongly_connected_components"],
                         [["A", "B", "C"]])
        self.assertIsNone(payload["condorcet_winner"])
        first = out.read_bytes()
        self.cli("--source", src, "--output", out)
        self.assertEqual(out.read_bytes(), first)
        # Reordered candidates/ballots input yields byte-identical output.
        permuted = self.source({
            "candidates": ["B", "C", "A"],
            "ballots": list(reversed(data["ballots"])),
        }, name="permuted.json")
        out2 = self.tmp / "out2.json"
        self.cli("--source", permuted, "--output", out2)
        self.assertEqual(out2.read_bytes(), first)

    def test_empty_ballots_via_cli(self):
        src = self.source({"candidates": ["A"], "ballots": []})
        out = self.tmp / "out.json"
        proc = self.cli("--source", src, "--output", out)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["total_weight"], 0)
        self.assertEqual(payload["comparisons"], [])
        self.assertEqual(payload["condorcet_winner"], "A")

    def test_failure_preserves_existing_output(self):
        good = self.source({
            "candidates": ["A", "B"],
            "ballots": [{"id": "g1", "ranking": ["A", "B"], "weight": 1}],
        })
        out = self.tmp / "out.json"
        self.cli("--source", good, "--output", out)
        before = out.read_bytes()
        bad_bodies = [
            "{}",
            '{"candidates":["A"],"ballots":[],"extra":1}',
            '{"candidates":["A"],"ballots":[],"candidates":["A"]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["A"],'
            '"weight":true}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["A"],'
            '"weight":1.5}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["A","A"],'
            '"weight":1}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["Z"],'
            '"weight":1}]}',
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["A"],'
            '"weight":0}]}',
            "NaN",
            '{"candidates":["A"],"ballots":[{"id":"x","ranking":["A"],'
            '"weight":NaN}]}',
            "not json",
            "",
        ]
        for i, body in enumerate(bad_bodies):
            bad = self.write(f"bad{i}.json", body)
            proc = self.cli("--source", bad, "--output", out, expect_rc=2)
            self.assertTrue(proc.stderr.strip())
            self.assertEqual(out.read_bytes(), before)
        # Missing source file and oversized source also fail safely.
        self.cli("--source", self.tmp / "missing.json", "--output", out,
                 expect_rc=2)
        self.assertEqual(out.read_bytes(), before)
        big = self.write("big.json", " " * 8_000_001)
        self.cli("--source", big, "--output", out, expect_rc=2)
        self.assertEqual(out.read_bytes(), before)

    def test_refuses_same_path_and_symlink_output(self):
        src = self.source({
            "candidates": ["A"],
            "ballots": [{"id": "g1", "ranking": ["A"], "weight": 1}],
        })
        before = src.read_bytes()
        self.cli("--source", src, "--output", src, expect_rc=2)
        self.assertEqual(src.read_bytes(), before)
        link = self.tmp / "link.json"
        link.symlink_to(src)
        self.cli("--source", src, "--output", link, expect_rc=2)
        self.assertEqual(src.read_bytes(), before)
        # A dangling symlink as output is refused as well.
        dangling = self.tmp / "dangling.json"
        dangling.symlink_to(self.tmp / "nowhere.json")
        self.cli("--source", src, "--output", dangling, expect_rc=2)


if __name__ == "__main__":
    unittest.main()
