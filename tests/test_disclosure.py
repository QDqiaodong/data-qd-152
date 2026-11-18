"""Tests for first-choice suppression planning (``ranked-ballots-disclosure``).

The protection test and the plan selection are checked against an independent,
deliberately naive enumeration: for small integer tables every non-negative
integer table with the same visible cells and all row/column totals is listed,
each mandatory cell's reachable values are collected, and every optional
subset is tried.  That oracle is exponential and is used only for tiny cases.
Production runs are additionally checked to never enumerate cell values (a
10^9-weight table is planned instantly).  Hand-computed acceptance cases cover
the stated two-row example, the zero row, shared extra suppression, blank
rankings, repeated ballot ids across precincts, reordering, invalid
coordinates, and failure preservation.
"""
import itertools
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

import ranked_ballots.disclosure as disclosure_module  # noqa: E402
from ranked_ballots import DisclosureError, disclosure_analysis  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def build_data(matrix, candidates, protect, optional):
    """Turn a first-choice matrix into the public input contract.

    Cell (i, j) with j < len(candidates) is candidate j's first-choice weight;
    the final column is the blank column for empty rankings.
    """
    precincts = []
    for i, row in enumerate(matrix):
        ballots = []
        for j, weight in enumerate(row):
            if weight:
                ballots.append({
                    "id": f"b{j}",
                    "ranking": [candidates[j]] if j < len(candidates) else [],
                    "weight": weight,
                })
        precincts.append({
            "id": f"P{i}", "candidates": list(candidates), "ballots": ballots,
        })

    def coordinate(pair):
        i, j = pair
        return {
            "precinct": f"P{i}",
            "candidate": candidates[j] if j < len(candidates) else None,
        }

    return {
        "precincts": precincts,
        "protect": [coordinate(p) for p in protect],
        "optional": [coordinate(p) for p in optional],
    }


def suppressed_set(report, candidates):
    """Suppressed cells as (row index, column index) for ``P<n>`` precincts."""
    result = set()
    for item in report["suppressed"]:
        row = int(item["precinct"][1:])
        candidate = item["candidate"]
        column = (candidates.index(candidate)
                  if candidate is not None else len(candidates))
        result.add((row, column))
    return result


def suppressed_coordinates(report):
    return {(item["precinct"], item["candidate"])
            for item in report["suppressed"]}


# --------------------------------------------------------------------------- #
# Independent exponential oracle (tiny tables only)
# --------------------------------------------------------------------------- #

def compatible_tables(matrix, fixed):
    """Every non-negative integer table with ``matrix``'s margins.

    Cells in ``fixed`` must keep their true value; suppressed cells are free.
    """
    nrows = len(matrix)
    ncols = len(matrix[0])
    row_totals = [sum(row) for row in matrix]
    column_totals = [sum(matrix[i][j] for i in range(nrows))
                     for j in range(ncols)]
    solutions = []
    remaining = list(column_totals)

    def distribute(row, column, current):
        if row == nrows:
            if any(remaining):
                return
            solutions.append([list(values) for values in current])
            return
        if column == ncols:
            if sum(current[row]) != row_totals[row]:
                return
            distribute(row + 1, 0, current)
            return
        pinned = fixed.get((row, column))
        if pinned is not None:
            if pinned > remaining[column]:
                return
            remaining[column] -= pinned
            current[row].append(pinned)
            distribute(row, column + 1, current)
            current[row].pop()
            remaining[column] += pinned
            return
        for value in range(remaining[column] + 1):
            remaining[column] -= value
            current[row].append(value)
            distribute(row, column + 1, current)
            current[row].pop()
            remaining[column] += value

    distribute(0, 0, [[] for _ in range(nrows)])
    return solutions


def oracle_values(matrix, suppressed):
    fixed = {(i, j): matrix[i][j]
             for i in range(len(matrix))
             for j in range(len(matrix[0]))
             if (i, j) not in suppressed}
    tables = compatible_tables(matrix, fixed)
    values = {}
    for i, j in suppressed:
        values[(i, j)] = {table[i][j] for table in tables}
    return values


def oracle_plan(matrix, mandatory, optional):
    """Return the mandated extra suppression set, or None when impossible."""
    ordered = sorted(optional)
    for size in range(len(ordered) + 1):
        for extra in itertools.combinations(ordered, size):
            values = oracle_values(matrix, set(mandatory) | set(extra))
            if all(len(values[cell]) >= 2 for cell in mandatory):
                return list(extra)
    return None


# --------------------------------------------------------------------------- #
# Stated acceptance examples
# --------------------------------------------------------------------------- #

class AcceptanceTests(unittest.TestCase):
    def test_two_row_rectangle(self):
        # Rows (1,3,3) and (3,1,1) over A, B, blank; first row first column
        # must be suppressed.  Suppressing it alone still leaves the rectangle
        # determined, so the optimum suppresses the other three corners.
        matrix = [[1, 3, 3], [3, 1, 1]]
        candidates = ["A", "B"]
        protect = [(0, 0)]
        optional = [(i, j) for i in range(2) for j in range(3)
                    if (i, j) != (0, 0)]
        report = disclosure_analysis(
            build_data(matrix, candidates, protect, optional))
        self.assertEqual(report["columns"], ["A", "B", None])
        self.assertEqual(report["column_totals"], [4, 4, 4])
        self.assertEqual(
            suppressed_set(report, candidates),
            {(0, 0), (0, 1), (1, 0), (1, 1)},
        )
        # Two size-3 extras protect the mandatory cell: the A/B rectangle and
        # the blank/B rectangle (columns 0/1 vs columns 1/2).  The specified
        # lexicographic tie-break must pick the A/B one, not the other.
        def protects(extra):
            values = oracle_values(matrix, set(protect) | set(extra))
            return all(len(values[cell]) >= 2 for cell in protect)

        alternatives = [
            extra for extra in itertools.combinations(sorted(optional), 3)
            if protects(extra)
        ]
        self.assertEqual(alternatives, [
            ((0, 1), (1, 0), (1, 1)),
            ((0, 2), (1, 0), (1, 2)),
        ])
        self.assertEqual(sorted(oracle_plan(matrix, protect, optional)),
                         [(0, 1), (1, 0), (1, 1)])
        self.assertNotEqual(
            suppressed_set(report, candidates),
            {(0, 0), (0, 2), (1, 0), (1, 2)},
        )

    def test_zero_row_without_mandatory_is_fine(self):
        # A zero-total row is forced to zero even when all its cells are
        # suppressed; a mandatory cell in another row can still be protected.
        matrix = [[0, 0, 0], [2, 1, 1], [1, 2, 2]]
        candidates = ["A", "B"]
        optional = [(i, j) for i in range(3) for j in range(3)
                    if (i, j) != (1, 0)]
        report = disclosure_analysis(build_data(
            matrix, candidates, [(1, 0)], optional))
        suppressed = suppressed_set(report, candidates)
        self.assertIn((1, 0), suppressed)
        self.assertEqual(len(suppressed), 4)
        zero_row = report["rows"][0]
        self.assertEqual(zero_row["total"], 0)
        self.assertEqual(zero_row["values"], [0, 0, 0])

    def test_zero_row_mandatory_is_impossible(self):
        matrix = [[0, 0, 0], [2, 1, 1], [1, 2, 2]]
        candidates = ["A", "B"]
        everything = [(i, j) for i in range(3) for j in range(3)
                      if (i, j) != (0, 0)]
        # Even suppressing the whole zero row cannot help: its total is 0.
        with self.assertRaises(DisclosureError):
            disclosure_analysis(build_data(
                matrix, candidates, [(0, 0)], everything))

    def test_mandatory_cells_share_the_same_extra_cells(self):
        # Two mandatory diagonal cells: each is protected by the very same
        # 2x2 cycle, so the shared extras are the two off-diagonal corners.
        matrix = [[2, 2], [2, 2]]
        candidates = ["A"]
        report = disclosure_analysis(build_data(
            matrix, candidates, [(0, 0), (1, 1)],
            [(0, 1), (1, 0)],
        ))
        self.assertEqual(suppressed_set(report, candidates),
                         {(0, 0), (0, 1), (1, 0), (1, 1)})
        # Protecting the same row's two cells needs the same two extras.
        report = disclosure_analysis(build_data(
            matrix, candidates, [(0, 0), (0, 1)],
            [(1, 0), (1, 1)],
        ))
        self.assertEqual(suppressed_set(report, candidates),
                         {(0, 0), (0, 1), (1, 0), (1, 1)})

    def test_different_mandatory_cells_may_have_different_witnesses(self):
        # The true table has its two protected amounts on the blank column.
        # Each of (0,0) and (2,0) can become 1 under a compatible table, but
        # no single table moves both -- different mandatory cells are proven
        # by different tables, which is exactly the required semantics.
        matrix = [[0, 0, 2], [1, 0, 2], [0, 0, 2]]
        candidates = ["A", "B"]
        protect = [(0, 0), (2, 0)]
        optional = [(0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (2, 1), (2, 2)]
        report = disclosure_analysis(
            build_data(matrix, candidates, protect, optional))
        suppressed = suppressed_set(report, candidates)
        self.assertEqual(
            suppressed,
            {(0, 0), (0, 2), (1, 0), (1, 2), (2, 0), (2, 2)},
        )
        tables = compatible_tables(matrix, {
            (i, j): matrix[i][j] for i in range(3) for j in range(3)
            if (i, j) not in suppressed
        })
        self.assertEqual({table[0][0] for table in tables}, {0, 1})
        self.assertEqual({table[2][0] for table in tables}, {0, 1})
        self.assertFalse(any(
            table[0][0] != 0 and table[2][0] != 0 for table in tables))


# --------------------------------------------------------------------------- #
# Random small tables against the exhaustive oracle
# --------------------------------------------------------------------------- #

class OracleTests(unittest.TestCase):
    def test_random_small_tables(self):
        import random
        rng = random.Random(20261118)
        for _ in range(120):
            nrows = rng.randint(1, 3)
            ncands = rng.randint(1, 2)
            ncols = ncands + 1
            matrix = [[rng.randint(0, 2) for _ in range(ncols)]
                      for _ in range(nrows)]
            cells = [(i, j) for i in range(nrows) for j in range(ncols)]
            mandatory = rng.sample(cells, rng.randint(1, min(2, len(cells))))
            rest = [c for c in cells if c not in mandatory]
            optional = rng.sample(rest, rng.randint(0, min(3, len(rest))))
            candidates = [f"C{j}" for j in range(ncands)]
            expected = oracle_plan(matrix, mandatory, optional)
            data = build_data(matrix, candidates, mandatory, optional)
            if expected is None:
                with self.assertRaises(DisclosureError):
                    disclosure_analysis(data)
                continue
            report = disclosure_analysis(data)
            self.assertEqual(
                suppressed_set(report, candidates),
                set(mandatory) | set(expected),
                (matrix, mandatory, optional),
            )


# --------------------------------------------------------------------------- #
# Contract and semantics
# --------------------------------------------------------------------------- #

class SemanticsTests(unittest.TestCase):
    def test_blank_rankings_and_billion_weight(self):
        weight = 10**9
        precincts = []
        for precinct in ("north", "south"):
            precincts.append({
                "id": precinct, "candidates": ["B", "A"], "ballots": [
                    {"id": "empty", "ranking": [], "weight": weight},
                    {"id": "a", "ranking": ["A"], "weight": weight},
                    {"id": "b", "ranking": ["B", "A"], "weight": weight},
                ],
            })
        data = {
            "precincts": precincts,
            "protect": [{"precinct": "north", "candidate": None}],
            "optional": [
                {"precinct": "north", "candidate": "A"},
                {"precinct": "north", "candidate": "B"},
                {"precinct": "south", "candidate": "A"},
                {"precinct": "south", "candidate": "B"},
                {"precinct": "south", "candidate": None},
            ],
        }
        report = disclosure_analysis(data)
        self.assertEqual(report["columns"], ["A", "B", None])
        self.assertEqual(report["column_totals"], [2 * weight, 2 * weight,
                                                   2 * weight])
        by_precinct = {row["precinct"]: row for row in report["rows"]}
        self.assertEqual(by_precinct["north"]["total"], 3 * weight)
        # The blank north cell is mandatory.  Both candidate columns tie, so
        # the lexicographically smallest coordinate sequence (A before B)
        # picks the A/blank rectangle.
        self.assertEqual(suppressed_coordinates(report), {
            ("north", "A"), ("north", None),
            ("south", "A"), ("south", None),
        })

    def test_same_ballot_id_across_precincts(self):
        data = {
            "precincts": [
                {"id": "a", "candidates": ["A", "B"], "ballots": [
                    {"id": "g", "ranking": ["A"], "weight": 2}]},
                {"id": "b", "candidates": ["A", "B"], "ballots": [
                    {"id": "g", "ranking": ["B"], "weight": 3}]},
            ],
            "protect": [{"precinct": "a", "candidate": "A"}],
            "optional": [
                {"precinct": "a", "candidate": "B"},
                {"precinct": "a", "candidate": None},
                {"precinct": "b", "candidate": "A"},
                {"precinct": "b", "candidate": "B"},
                {"precinct": "b", "candidate": None},
            ],
        }
        report = disclosure_analysis(data)
        self.assertEqual(len(report["rows"]), 2)
        # Both precincts use ballot id "g"; that is legal across precincts.
        self.assertEqual(
            suppressed_coordinates(report),
            {("a", "A"), ("a", "B"), ("b", "A"), ("b", "B")},
        )

    def test_column_and_row_order(self):
        matrix = [[1, 2, 3], [4, 5, 6]]
        candidates = ["b", "a"]
        report = disclosure_analysis(build_data(
            matrix, candidates, [(0, 1)],
            [(0, 0), (0, 2), (1, 0), (1, 1), (1, 2)]))
        self.assertEqual(report["columns"], ["a", "b", None])
        # Column a is first-choice 2 / 5, b is 1 / 4, blank 3 / 6.
        self.assertEqual(report["column_totals"], [7, 5, 9])

    def test_output_has_only_public_fields(self):
        report = disclosure_analysis(build_data(
            [[1, 2, 3], [4, 5, 6]], ["A", "B"],
            [(0, 0)], [(0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]))
        self.assertEqual(set(report), {
            "schema_version", "columns", "column_totals", "rows", "suppressed",
        })
        self.assertEqual(report["schema_version"], 1)
        # No hidden original value or digest leaks through the report.
        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn("sha256", encoded)

    def test_production_never_enumerates_values(self):
        # Planning huge weights costs the same as tiny ones: the protection
        # test only asks whether residual cycles exist.  Cap the number of
        # mandatory-cell checks to prove no per-value work happens.
        matrix = [[10**9, 10**9], [10**9, 10**9]]
        calls = []
        real = disclosure_module._cell_forced

        def counting(*args):
            calls.append(1)
            return real(*args)

        with mock.patch.object(disclosure_module, "_cell_forced", counting):
            report = disclosure_analysis(build_data(
                matrix, ["A"], [(0, 0)], [(0, 1), (1, 0), (1, 1)]))
        self.assertEqual(len(report["suppressed"]), 4)
        # One mandatory cell, at most 2**3 optional subsets.
        self.assertLessEqual(len(calls), 8)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

class ValidationTests(unittest.TestCase):
    def good(self):
        return {
            "precincts": [
                {"id": "p", "candidates": ["A"], "ballots": [
                    {"id": "g", "ranking": ["A"], "weight": 1}]},
            ],
            "protect": [{"precinct": "p", "candidate": "A"}],
            "optional": [],
        }

    def assert_rejected(self, mutate):
        data = self.good()
        mutate(data)
        with self.assertRaises((ValueError, TypeError, KeyError)):
            disclosure_analysis(data)

    def test_top_level_shape(self):
        self.assert_rejected(lambda d: d.update(extra=1))
        self.assert_rejected(lambda d: d.pop("optional"))
        self.assert_rejected(lambda d: d.update(precincts=[]))

    def test_precinct_shape(self):
        self.assert_rejected(
            lambda d: d["precincts"][0].update(candidates=[]))
        self.assert_rejected(
            lambda d: d["precincts"].append({
                "id": "p", "candidates": ["A"], "ballots": []}))
        self.assert_rejected(
            lambda d: d["precincts"].append({
                "id": "q", "candidates": ["B"], "ballots": []}))

    def test_ballot_ids_unique_only_within_precinct(self):
        data = {
            "precincts": [
                {"id": "p", "candidates": ["A", "B"], "ballots": [
                    {"id": "g", "ranking": ["A"], "weight": 1},
                    {"id": "h", "ranking": ["B"], "weight": 1}]},
                {"id": "q", "candidates": ["A", "B"], "ballots": [
                    {"id": "g", "ranking": ["A"], "weight": 1},
                    {"id": "h", "ranking": ["B"], "weight": 1}]},
            ],
            "protect": [{"precinct": "p", "candidate": "A"}],
            "optional": [
                {"precinct": "p", "candidate": "B"},
                {"precinct": "q", "candidate": "A"},
                {"precinct": "q", "candidate": "B"},
            ],
        }
        # "g" and "h" repeat across p and q: legal, because ids are per
        # precinct.  The plan still succeeds.
        report = disclosure_analysis(data)
        self.assertEqual(len(report["suppressed"]), 4)
        # A duplicate id inside one precinct is rejected.
        clone = json.loads(json.dumps(data))
        clone["precincts"][0]["ballots"].append(
            {"id": "g", "ranking": ["A"], "weight": 1})
        with self.assertRaises(ValueError):
            disclosure_analysis(clone)

    def test_invalid_coordinates(self):
        self.assert_rejected(lambda d: d.update(protect=[]))
        self.assert_rejected(lambda d: d["protect"].append(
            {"precinct": "p", "candidate": "A"}))
        self.assert_rejected(lambda d: d["protect"][0].update(
            precinct="missing"))
        self.assert_rejected(lambda d: d["protect"][0].update(candidate="Z"))
        self.assert_rejected(lambda d: d["protect"][0].update(candidate=1))
        self.assert_rejected(lambda d: d["optional"].append(
            {"precinct": "p", "candidate": "A"}))
        self.assert_rejected(lambda d: d.update(optional=[
            {"precinct": "p", "candidate": "A"},
            {"precinct": "p", "candidate": "A"}]))
        self.assert_rejected(lambda d: d["optional"].append(
            {"precinct": "p", "candidate": "A", "extra": 1}))

    def test_more_than_ten_optional_via_wide_table(self):
        matrix = [[1] * 3 + [0]] * 4
        candidates = ["A", "B", "C"]
        optional = [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0),
                    (1, 1), (1, 2), (1, 3), (2, 0), (2, 1), (2, 2)]
        data = build_data(matrix, candidates, [(3, 0)], optional)
        with self.assertRaises(ValueError):
            disclosure_analysis(data)


# --------------------------------------------------------------------------- #
# Real CLI
# --------------------------------------------------------------------------- #

class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def cli(self, *args, expect_rc=0):
        launcher = self.tmp / "run_disclosure_cli.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from ranked_ballots.disclosure_cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-I", str(launcher), *map(str, args)],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stderr)
        return proc

    def accept(self):
        return build_data([[1, 3, 3], [3, 1, 1]], ["A", "B"], [(0, 0)],
                          [(0, 1), (0, 2), (1, 0), (1, 1), (1, 2)])

    def test_success_and_byte_determinism(self):
        src = self.write("in.json", self.accept())
        out = self.tmp / "out.json"
        proc = self.cli("--source", src, "--output", out)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload, json.loads(out.read_bytes()))
        self.assertEqual(payload["schema_version"], 1)
        first = out.read_bytes()
        self.cli("--source", src, "--output", out)
        self.assertEqual(out.read_bytes(), first)

    def test_reordering_leaves_bytes_unchanged(self):
        data = self.accept()
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        self.cli("--source", src, "--output", out)
        first = out.read_bytes()

        def permuted():
            copy = json.loads(json.dumps(data))
            copy["precincts"].reverse()
            for precinct in copy["precincts"]:
                precinct["candidates"].reverse()
                precinct["ballots"].reverse()
            copy["protect"].reverse()
            copy["optional"].reverse()
            return copy

        src2 = self.write("permuted.json", permuted())
        out2 = self.tmp / "out2.json"
        self.cli("--source", src2, "--output", out2)
        self.assertEqual(out2.read_bytes(), first)

    def test_impossible_plan_exits_two_without_leaking(self):
        data = build_data([[0, 0, 0], [2, 1, 1]], ["A", "B"], [(0, 0)],
                          [(0, 1), (0, 2), (1, 0), (1, 1), (1, 2)])
        src = self.write("in.json", data)
        out = self.tmp / "out.json"
        out.write_text("prior", encoding="utf-8")
        proc = self.cli("--source", src, "--output", out, expect_rc=2)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(out.read_text(encoding="utf-8"), "prior")

    def test_failure_preserves_output_and_validates(self):
        good = self.accept()
        src = self.write("in.json", good)
        out = self.tmp / "out.json"
        self.cli("--source", src, "--output", out)
        before = out.read_bytes()
        bad_bodies = [
            "{}",
            '{"precincts":[],"protect":[],"optional":[]}',
            '{"precincts":[{"id":"p","candidates":["A"],"ballots":[]}],'
            '"protect":[{"precinct":"p","candidate":"A"}],"optional":[]}',
            '{"precincts":[{"id":"p","candidates":["A"],"ballots":[]}],'
            '"protect":[],"optional":[]}',
            '{"precincts":[{"id":"p","candidates":["A"],"ballots":[]}],'
            '"protect":[{"precinct":"p","candidate":null}],"optional":[]}',
            '{"precincts":[{"id":"p","candidates":["A"],"ballots":[]}],'
            '"protect":[{"precinct":"p","candidate":"A"}],"optional":[],'
            '"protect":[{"precinct":"p","candidate":"A"}]}',
            '{"precincts":[{"id":"p","candidates":["A"],"ballots":'
            '[{"id":"g","ranking":["A"],"weight":NaN}]}],"protect":'
            '[{"precinct":"p","candidate":"A"}],"optional":[]}',
            "not json",
        ]
        for i, body in enumerate(bad_bodies):
            bad = self.tmp / f"bad{i}.json"
            bad.write_text(body, encoding="utf-8")
            proc = self.cli("--source", bad, "--output", out, expect_rc=2)
            self.assertTrue(proc.stderr.strip())
            self.assertEqual(out.read_bytes(), before)
        self.cli("--source", self.tmp / "missing.json", "--output", out,
                 expect_rc=2)
        self.assertEqual(out.read_bytes(), before)

    def test_refuses_same_path_and_symlink(self):
        src = self.write("in.json", self.accept())
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

    def test_size_limit(self):
        src = self.tmp / "big.json"
        src.write_bytes(b" " * 8_000_001)
        out = self.tmp / "out.json"
        self.cli("--source", src, "--output", out, expect_rc=2)


if __name__ == "__main__":
    unittest.main()
