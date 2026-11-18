"""Tests for scan/v2-ledger capacity reconciliation (ranked-ballots-link).

Small random instances are checked against an independent brute-force
enumeration of every legal complete assignment.  A separate 1000+ scan
alternating-chain graph exercises scale without any weight expansion, and real
CLI subprocess runs cover feasible, infeasible and failure-preservation paths.
"""
import hashlib
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))
from ranked_ballots import analyze, canonical_bytes, link_records, LinkError  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #

def make_report(candidates, ballots, revision=0, winner_allowed_none=True):
    candidates = sorted(candidates)
    ballots = sorted(ballots, key=lambda b: b["id"])
    ledger = {"candidates": candidates, "ballots": ballots}
    report = {
        "schema_version": 2,
        "revision": revision,
        "candidates": candidates,
        "ballots": ballots,
        "snapshot_sha256": hashlib.sha256(canonical_bytes(ledger)).hexdigest(),
        "result": analyze(ledger),
    }
    if not winner_allowed_none:
        assert report["result"]["winner"] is not None
    return report


def write_report(tmp, report, name="report.json", raw=None):
    path = Path(tmp) / name
    path.write_bytes(raw.encode("utf-8") if raw is not None else canonical_bytes(report))
    return path


def write_scans(tmp, records, name="scans.json", raw=None):
    path = Path(tmp) / name
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps({"records": records}), encoding="utf-8")
    return path


def compatible(slots, ranking):
    """Independent compatibility rule straight from the specification."""
    if len(slots) != len(ranking):
        return False
    return all(s is None or s == c for s, c in zip(slots, ranking))


def neighbors_of(slots, ballots):
    return {b["id"] for b in ballots if compatible(slots, b["ranking"])}


def brute_force(records, ballots):
    """Enumerate every legal complete assignment; return (feasible, possible map)."""
    ballots_by_id = {b["id"]: b for b in ballots}
    options = []
    for scan_id, slots in records:
        opts = sorted(neighbors_of(slots, ballots))
        if not opts:
            return False, {scan_id: set() for scan_id, _ in records}
        options.append((scan_id, opts))
    possible = {scan_id: set() for scan_id, _ in records}
    loads = {b["id"]: 0 for b in ballots}

    def extend(position):
        if position == len(options):
            for scan_id, _ in options:
                possible[scan_id].add(assignment[scan_id])
            return
        scan_id, opts = options[position]
        for bid in opts:
            if loads[bid] < ballots_by_id[bid]["weight"]:
                loads[bid] += 1
                assignment[scan_id] = bid
                extend(position + 1)
                loads[bid] -= 1

    assignment = {}
    found = []

    def extend(position):
        if position == len(options):
            found.append(True)
            for scan_id, _ in options:
                possible[scan_id].add(assignment[scan_id])
            return
        scan_id, opts = options[position]
        for bid in opts:
            if loads[bid] < ballots_by_id[bid]["weight"]:
                loads[bid] += 1
                assignment[scan_id] = bid
                extend(position + 1)
                loads[bid] -= 1

    extend(0)
    return bool(found), possible


def verify_feasible_payload(testcase, payload, report, scans_raw_records):
    """Independently re-check a feasible payload from the supplied facts."""
    testcase.assertEqual(payload["status"], "feasible")
    testcase.assertIsNone(payload["witness"])
    ballots = report["ballots"]
    by_id = {b["id"]: b for b in ballots}
    slots_by_scan = dict(scans_raw_records)
    # assignment: every scan exactly once, per-record compatibility, capacities.
    assignment = payload["assignment"]
    testcase.assertEqual(sorted(a["scan_id"] for a in assignment),
                         sorted(slots_by_scan))
    testcase.assertEqual(len({a["scan_id"] for a in assignment}), len(assignment))
    loads = {b["id"]: 0 for b in ballots}
    for entry in assignment:
        bid = entry["ballot_id"]
        testcase.assertIn(bid, by_id)
        testcase.assertTrue(compatible(slots_by_scan[entry["scan_id"]],
                                        by_id[bid]["ranking"]))
        loads[bid] += 1
    for bid, load in loads.items():
        testcase.assertLessEqual(load, by_id[bid]["weight"])
    # records rows: sorted, non-empty unique possible sets, all locally compatible.
    rows = payload["records"]
    testcase.assertEqual([r["scan_id"] for r in rows], sorted(slots_by_scan))
    for row in rows:
        ids = row["possible_ballot_ids"]
        testcase.assertEqual(ids, sorted(set(ids)))
        testcase.assertTrue(ids)
        local = neighbors_of(slots_by_scan[row["scan_id"]], ballots)
        testcase.assertEqual(set(ids), set(ids) & local)
        testcase.assertEqual(row["classification"],
                             "forced" if len(ids) == 1 else "ambiguous")
    # Every assignment target must itself be advertised as possible.
    for entry in assignment:
        row = next(r for r in rows if r["scan_id"] == entry["scan_id"])
        testcase.assertIn(entry["ballot_id"], row["possible_ballot_ids"])


def verify_infeasible_payload(testcase, payload, report, scan_records):
    """Independently rebuild N(S) and the deficit from the raw facts."""
    testcase.assertEqual(payload["status"], "infeasible")
    testcase.assertIsNone(payload["assignment"])
    testcase.assertEqual(payload["records"], [])
    witness = payload["witness"]
    testcase.assertIsNotNone(witness)
    ballots = report["ballots"]
    by_id = {b["id"]: b for b in ballots}
    slots_by_scan = dict(scan_records)
    scan_ids = witness["scan_ids"]
    testcase.assertEqual(scan_ids, sorted(set(scan_ids)))
    testcase.assertTrue(scan_ids)
    testcase.assertTrue(set(scan_ids) <= set(slots_by_scan))
    expected_neighbors = set()
    for scan_id in scan_ids:
        expected_neighbors |= neighbors_of(slots_by_scan[scan_id], ballots)
    testcase.assertEqual(witness["ballot_ids"], sorted(expected_neighbors))
    testcase.assertEqual(witness["ballot_ids"], sorted(set(witness["ballot_ids"])))
    capacity_sum = sum(by_id[bid]["weight"] for bid in expected_neighbors)
    testcase.assertEqual(witness["capacity_sum"], capacity_sum)
    testcase.assertEqual(witness["record_count"], len(scan_ids))
    testcase.assertEqual(witness["deficit"], len(scan_ids) - capacity_sum)
    testcase.assertGreater(witness["deficit"], 0)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Stated acceptance examples
# --------------------------------------------------------------------------- #

class AcceptanceTests(TempDir):
    def _pair_report(self):
        return make_report(["A", "B", "C"], [
            {"id": "g1", "ranking": ["A", "B"], "weight": 1},
            {"id": "g2", "ranking": ["A", "C"], "weight": 1},
        ])

    def test_global_forcing_through_capacity(self):
        report = self._pair_report()
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [
            {"id": "s1", "slots": ["A", None]},
            {"id": "s2", "slots": ["A", "B"]},
        ])
        payload = link_records(rp, sp)
        verify_feasible_payload(self, payload, report, [
            ("s1", ["A", None]), ("s2", ["A", "B"])])
        rows = {r["scan_id"]: r for r in payload["records"]}
        self.assertEqual(rows["s1"]["possible_ballot_ids"], ["g2"])
        self.assertEqual(rows["s2"]["possible_ballot_ids"], ["g1"])
        self.assertEqual(rows["s1"]["classification"], "forced")
        self.assertEqual(rows["s2"]["classification"], "forced")
        self.assertEqual(payload["assignment"], [
            {"scan_id": "s1", "ballot_id": "g2"},
            {"scan_id": "s2", "ballot_id": "g1"},
        ])

    def test_unused_capacity_creates_alternative(self):
        # With s2 removed, g1 keeps spare capacity, so s1 is ambiguous.
        report = self._pair_report()
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [{"id": "s1", "slots": ["A", None]}])
        payload = link_records(rp, sp)
        verify_feasible_payload(self, payload, report, [("s1", ["A", None])])
        row = payload["records"][0]
        self.assertEqual(row["possible_ballot_ids"], ["g1", "g2"])
        self.assertEqual(row["classification"], "ambiguous")

    def test_strict_hall_deficit_three_records_two_unit_groups(self):
        report = self._pair_report()
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [
            {"id": f"s{i}", "slots": ["A", None]} for i in range(3)])
        payload = link_records(rp, sp)
        records = [(f"s{i}", ["A", None]) for i in range(3)]
        verify_infeasible_payload(self, payload, report, records)
        self.assertEqual(payload["witness"]["scan_ids"], ["s0", "s1", "s2"])
        self.assertEqual(payload["witness"]["ballot_ids"], ["g1", "g2"])
        self.assertEqual(payload["witness"]["record_count"], 3)
        self.assertEqual(payload["witness"]["capacity_sum"], 2)
        self.assertEqual(payload["witness"]["deficit"], 1)

    def test_weighted_capacity_feeds_deficit(self):
        # Same neighbourhood, but weights sum to 3: three scans now fit.
        report = make_report(["A", "B", "C"], [
            {"id": "g1", "ranking": ["A", "B"], "weight": 2},
            {"id": "g2", "ranking": ["A", "C"], "weight": 1},
        ])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [
            {"id": f"s{i}", "slots": ["A", None]} for i in range(3)])
        payload = link_records(rp, sp)
        records = [(f"s{i}", ["A", None]) for i in range(3)]
        verify_feasible_payload(self, payload, report, records)
        for row in payload["records"]:
            self.assertEqual(row["possible_ballot_ids"], ["g1", "g2"])

    def test_null_does_not_stretch_length(self):
        report = self._pair_report()
        rp = write_report(self.tmp, report)
        # ["A", null] must not match length-1 or length-3 rankings.
        other = make_report(["A", "B", "C", "D"], [
            {"id": "h1", "ranking": ["A"], "weight": 1},
            {"id": "h2", "ranking": ["A", "B", "C"], "weight": 1},
        ])
        rp2 = write_report(self.tmp, other, name="r2.json")
        sp = write_scans(self.tmp, [{"id": "s1", "slots": ["A", None]}],
                         name="sc2.json")
        payload = link_records(rp2, sp)
        verify_infeasible_payload(self, payload, other, [("s1", ["A", None])])
        self.assertEqual(payload["witness"]["ballot_ids"], [])
        self.assertEqual(payload["witness"]["capacity_sum"], 0)
        # And the matching pair report still works.
        payload = link_records(rp, sp)
        self.assertEqual(payload["status"], "feasible")


class SemanticTests(TempDir):
    def test_same_ranking_distinct_group_identity(self):
        report = make_report(["A"], [
            {"id": "g-a", "ranking": ["A"], "weight": 1},
            {"id": "g-b", "ranking": ["A"], "weight": 1},
        ])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [{"id": "s1", "slots": ["A"]}])
        payload = link_records(rp, sp)
        verify_feasible_payload(self, payload, report, [("s1", ["A"])])
        self.assertEqual(payload["records"][0]["possible_ballot_ids"],
                         ["g-a", "g-b"])
        # Two scans: both groups stay possible for each scan (two matchings).
        sp = write_scans(self.tmp, [
            {"id": "s1", "slots": ["A"]}, {"id": "s2", "slots": ["A"]}])
        payload = link_records(rp, sp)
        verify_feasible_payload(self, payload, report,
                                [("s1", ["A"]), ("s2", ["A"])])
        for row in payload["records"]:
            self.assertEqual(row["possible_ballot_ids"], ["g-a", "g-b"])
            self.assertEqual(row["classification"], "ambiguous")

    def test_empty_ranking_only_matches_empty_slots(self):
        report = make_report(["A"], [
            {"id": "e", "ranking": [], "weight": 2},
            {"id": "a", "ranking": ["A"], "weight": 1},
        ])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [
            {"id": "s-empty", "slots": []},
            {"id": "s-null", "slots": [None]},
        ])
        payload = link_records(rp, sp)
        verify_feasible_payload(self, payload, report,
                                [("s-empty", []), ("s-null", [None])])
        rows = {r["scan_id"]: r for r in payload["records"]}
        self.assertEqual(rows["s-empty"]["possible_ballot_ids"], ["e"])
        self.assertEqual(rows["s-null"]["possible_ballot_ids"], ["a"])

    def test_zero_records_is_feasible_with_empty_assignment(self):
        report = make_report(["A"], [
            {"id": "a", "ranking": ["A"], "weight": 1}])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [])
        payload = link_records(rp, sp)
        self.assertEqual(payload["status"], "feasible")
        self.assertEqual(payload["assignment"], [])
        self.assertEqual(payload["records"], [])
        self.assertIsNone(payload["witness"])

    def test_empty_ledger_with_records_is_infeasible(self):
        report = make_report(["A"], [])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [{"id": "s1", "slots": []}])
        payload = link_records(rp, sp)
        verify_infeasible_payload(self, payload, report, [("s1", [])])

    def test_huge_weight_is_not_expanded(self):
        report = make_report(["A"], [
            {"id": "big", "ranking": ["A"], "weight": 10**9}])
        rp = write_report(self.tmp, report)
        records = [{"id": f"s{i:04d}", "slots": ["A"]} for i in range(5000)]
        sp = write_scans(self.tmp, records)
        payload = link_records(rp, sp)
        self.assertEqual(payload["status"], "feasible")
        self.assertEqual(len(payload["assignment"]), 5000)
        self.assertTrue(all(r["classification"] == "forced"
                            for r in payload["records"]))

    def test_locally_compatible_group_filled_by_other_records(self):
        report = make_report(["A", "B"], [
            {"id": "g1", "ranking": ["A"], "weight": 1},
            {"id": "g2", "ranking": ["B"], "weight": 1},
        ])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [
            {"id": "s1", "slots": ["A"]},       # only g1
            {"id": "s2", "slots": [None]},      # g1 and g2
        ])
        payload = link_records(rp, sp)
        records = [("s1", ["A"]), ("s2", [None])]
        verify_feasible_payload(self, payload, report, records)
        rows = {r["scan_id"]: r for r in payload["records"]}
        self.assertEqual(rows["s1"]["possible_ballot_ids"], ["g1"])
        self.assertEqual(rows["s2"]["possible_ballot_ids"], ["g2"])

    def test_spare_capacity_keeps_edge_possible(self):
        report = make_report(["A", "B"], [
            {"id": "g1", "ranking": ["A"], "weight": 3},
            {"id": "g2", "ranking": ["B"], "weight": 1},
        ])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [
            {"id": "s1", "slots": ["A"]},
            {"id": "s2", "slots": [None]},
        ])
        payload = link_records(rp, sp)
        rows = {r["scan_id"]: r for r in payload["records"]}
        # g1 still has two free seats after s1, so s2 can pick g1 or g2.
        self.assertEqual(rows["s2"]["possible_ballot_ids"], ["g1", "g2"])
        self.assertEqual(rows["s1"]["possible_ballot_ids"], ["g1"])

    def test_winner_null_and_thirteen_candidates_accepted(self):
        candidates = [f"c{i:02d}" for i in range(13)]
        report = make_report(candidates, [
            {"id": "blank", "ranking": [], "weight": 1}])
        self.assertIsNone(report["result"]["winner"])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [{"id": "s1", "slots": []}])
        payload = link_records(rp, sp)
        self.assertEqual(payload["status"], "feasible")

    def test_one_hundred_candidates_accepted(self):
        candidates = [f"c{i:02d}" for i in range(100)]
        ranking = list(candidates)
        report = make_report(candidates, [
            {"id": "full", "ranking": ranking, "weight": 1}])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [{"id": "s1", "slots": list(ranking)}])
        payload = link_records(rp, sp)
        self.assertEqual(payload["records"][0]["possible_ballot_ids"], ["full"])

    def test_order_does_not_change_identity_sets(self):
        report = make_report(["A", "B", "C"], [
            {"id": "g1", "ranking": ["A", "B"], "weight": 1},
            {"id": "g2", "ranking": ["A", "C"], "weight": 1},
        ])
        rp = write_report(self.tmp, report)
        records_a = [
            {"id": "z", "slots": ["A", None]},
            {"id": "a", "slots": ["A", "B"]},
            {"id": "m", "slots": ["A", "C"]},
        ]
        sp1 = write_scans(self.tmp, records_a, name="a.json")
        sp2 = write_scans(self.tmp, list(reversed(records_a)), name="b.json")
        p1 = link_records(rp, sp1)
        p2 = link_records(rp, sp2)
        self.assertEqual(p1["records"], p2["records"])
        self.assertEqual(p1["status"], p2["status"])
        # Same bytes re-run is byte-for-byte deterministic.
        rp_b = write_report(self.tmp, report, name="report-copy.json")
        sp3 = write_scans(self.tmp, records_a, name="a-copy.json")
        p3 = link_records(rp_b, sp3)
        self.assertEqual(canonical_bytes(p1), canonical_bytes(p3))

    def test_digests_pin_exact_input_bytes(self):
        report = make_report(["A"], [{"id": "a", "ranking": ["A"], "weight": 1}])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [{"id": "s1", "slots": ["A"]}])
        payload = link_records(rp, sp)
        self.assertEqual(payload["report_sha256"],
                         hashlib.sha256(rp.read_bytes()).hexdigest())
        self.assertEqual(payload["scans_sha256"],
                         hashlib.sha256(sp.read_bytes()).hexdigest())
        self.assertEqual(payload["snapshot_sha256"], report["snapshot_sha256"])
        self.assertEqual(payload["schema_version"], 1)


# --------------------------------------------------------------------------- #
# Long alternating dependency chain at scale (>= 1000 scans), degree exactly 2
# --------------------------------------------------------------------------- #

def build_chain(group_count, symbol_count=100):
    """Build a degree-2 alternating path on length-3 full rankings.

    Pairs of consecutive rankings form rows.  Row ``k`` owns two row-private
    symbols (``a_k`` at position 0 and ``v_k`` at position 2) and shares one
    boundary symbol ``u_{k+1}`` with the next row at position 1::

        R_{2k}   = [a_k, u_k,     v_k]
        R_{2k+1} = [a_k, u_{k+1}, v_k]      # differs only at position 1
        R_{2k+2} = [a_{k+1}, u_{k+1}, v_{k+1}]

    The within-row scan blanks position 1; its readable signature
    ``(a_k, v_k)`` occurs in exactly the two rankings of row ``k``.  The
    boundary scan blanks positions 0 and 2; its readable position-1 value
    ``u_{k+1}`` occurs in exactly the two rankings straddling the boundary.
    An independent post-check proves every chain scan has exactly the two
    intended neighbours, so the compatibility graph is a simple path with no
    extra locally-compatible edges.  The construction uses about
    ``1.5 * group_count`` distinct candidates; callers reach 1000+ scans by
    replicating scans against weighted copies of the groups.
    """
    needed = group_count + (group_count // 2) + 2
    assert needed <= symbol_count, "chain longer than available symbols"
    symbols = [f"c{i:02d}" for i in range(symbol_count)]
    next_symbol = 0

    def take():
        nonlocal next_symbol
        value = symbols[next_symbol]
        next_symbol += 1
        return value

    u = [take()]  # u_0
    a = []
    v = []
    rankings = []
    k = 0
    while len(rankings) < group_count:
        a_k = take()
        v_k = take()
        a.append(a_k)
        v.append(v_k)
        u_k = u[k]
        rankings.append([a_k, u_k, v_k])
        if len(rankings) == group_count:
            break
        u_next = take()
        u.append(u_next)
        rankings.append([a_k, u_next, v_k])
        k += 1
    assert len({tuple(x) for x in rankings}) == len(rankings)
    for ranking in rankings:
        assert len(set(ranking)) == 3
    scans = []
    for i in range(1, group_count):
        differing = [p for p in range(3)
                     if rankings[i - 1][p] != rankings[i][p]]
        assert 1 <= len(differing) <= 2
        slots = list(rankings[i])
        for changed in differing:
            slots[changed] = None
        scans.append((f"p{i:04d}", slots))
    # Independent post-condition: exact two neighbours per chain scan.
    for scan_id, slots in scans:
        matches = [
            j for j, ranking in enumerate(rankings)
            if compatible(slots, ranking)
        ]
        i = int(scan_id[1:])
        assert matches == [i - 1, i], (scan_id, matches)
    return rankings, scans, symbols[:next_symbol]


class LargeChainTests(TempDir):
    """Long alternating-dependency graphs at production scale.

    The exact degree-2 path is limited to ~64 distinct groups by the 100
    candidate budget, so the 1000+ scan instances replicate each scan type
    against groups whose weight holds the multiplicity.  The replicated graph
    remains one connected alternating chain: every reassignment propagates
    through the whole path, which the SCC-based classification must report.
    """

    GROUPS = 64
    REPLICAS = 8  # 63 chain scans * 8 = 504 chain records per layer

    def _build(self, *, with_pins, extra_right_pin):
        """Return (report, ballots, records, raw_records) for one chain layer.

        A full-ranking pin scan matches only its own group.  When pins are
        present each group's weight equals its pin demand plus its two chain
        demands, so tight capacity forces every chain edge; without pins the
        same groups have spare pin capacity and every chain edge stays
        ambiguous.  ``extra_right_pin`` overfills the last tight group by one
        unit, producing a strict Hall deficit.
        """
        rankings, chain_scans, used_symbols = build_chain(self.GROUPS)
        base = self.REPLICAS
        # Canonical tight allocation: chain scan p_i is assigned to group g_i.
        # Group g_0 therefore receives only its full-ranking pin; interior
        # group g_j (1 <= j < GROUPS-1) receives one pin plus the REPLICAS
        # copies of p_j; the last group g_{GROUPS-1} additionally receives the
        # copies of p_{GROUPS-1}.  Every weight equals this exact demand, so with
        # pins present the tight capacity forces the whole chain.  Without pins
        # the same weights leave one spare seat per group and every chain edge
        # stays ambiguous.  The optional extra right pin overfills g_last by one
        # because no spare capacity exists anywhere in the pinned tight graph.
        ballots = []
        for j in range(self.GROUPS):
            demand = (1 if with_pins else 0)
            if j >= 1:
                demand += base  # copies of p_j assigned to g_j
            # Weights must stay >= 1 even for the no-pin endpoint, which keeps
            # one spare seat; the pinned variant is exactly tight at g_0.
            if demand == 0:
                demand = 1
            ballots.append({"id": f"g{j:04d}",
                            "ranking": rankings[j], "weight": demand})
        report = make_report(used_symbols, ballots)
        records = []
        if with_pins:
            for j in range(self.GROUPS):
                records.append({"id": f"pin{j:04d}",
                                "slots": list(rankings[j])})
        for copy in range(base):
            for scan_id, slots in chain_scans:
                records.append({
                    "id": f"{scan_id}-r{copy:02d}",
                    "slots": list(slots),
                })
        if extra_right_pin:
            records.append({"id": "z-last", "slots": list(rankings[-1])})
        raw_records = [(r["id"], r["slots"]) for r in records]
        return report, ballots, records, raw_records

    def test_forced_chain_over_more_than_one_thousand_scans(self):
        # Two identical layers double every weight and record count, pushing the
        # single connected alternating graph past 1000 scans.
        report, ballots, records, raw_records = self._build(
            with_pins=True, extra_right_pin=False)
        layer_two = [{
            "id": "b-" + record["id"],
            "slots": list(record["slots"]),
        } for record in records]
        records = records + layer_two
        raw_records = [(r["id"], r["slots"]) for r in records]
        for ballot in ballots:
            ballot["weight"] *= 2
        report = make_report(report["candidates"], ballots)
        self.assertGreaterEqual(len(records), 1000)
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, records)
        payload = link_records(rp, sp)
        verify_feasible_payload(self, payload, report, raw_records)
        # Each pin fills its group's pin capacity; the tight weights then force
        # every chain scan onto the right-hand group of its pair, all the way
        # down the path, in both layers.
        for row in payload["records"]:
            self.assertEqual(row["classification"], "forced", row["scan_id"])
        by_scan = {a["scan_id"]: a["ballot_id"] for a in payload["assignment"]}
        for j in range(self.GROUPS):
            self.assertEqual(by_scan[f"pin{j:04d}"], f"g{j:04d}")
            self.assertEqual(by_scan[f"b-pin{j:04d}"], f"g{j:04d}")
        for i in range(1, self.GROUPS):
            for copy in range(self.REPLICAS):
                expected = f"g{i:04d}"
                self.assertEqual(
                    by_scan[f"p{i:04d}-r{copy:02d}"], expected)
                self.assertEqual(
                    by_scan[f"b-p{i:04d}-r{copy:02d}"], expected)

    def test_chain_without_pins_keeps_each_pair_ambiguous(self):
        report, ballots, records, raw_records = self._build(
            with_pins=False, extra_right_pin=False)
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, records)
        payload = link_records(rp, sp)
        verify_feasible_payload(self, payload, report, raw_records)
        for row in payload["records"]:
            self.assertEqual(row["classification"], "ambiguous")
            i = int(row["scan_id"][1:5])
            self.assertEqual(row["possible_ballot_ids"],
                             [f"g{i-1:04d}", f"g{i:04d}"])

    def test_large_infeasible_chain_witness(self):
        report, ballots, records, raw_records = self._build(
            with_pins=True, extra_right_pin=True)
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, records)
        payload = link_records(rp, sp)
        verify_infeasible_payload(self, payload, report, raw_records)
        witness = payload["witness"]
        self.assertGreater(witness["deficit"], 0)
        self.assertEqual(witness["record_count"],
                         len(witness["scan_ids"]))
        # The extra end pin overfills the last tight group by exactly one.
        self.assertEqual(witness["deficit"], 1)


# --------------------------------------------------------------------------- #
# Random small graphs vs exhaustive enumeration
# --------------------------------------------------------------------------- #

class RandomBruteForceTests(unittest.TestCase):
    def run_seed(self, seed):
        rng = random.Random(seed)
        candidate_count = rng.randrange(2, 7)
        candidates = [f"c{i}" for i in range(candidate_count)]
        group_count = rng.randrange(1, 7)
        ballots = []
        used_rankings = set()
        for j in range(group_count):
            length = rng.randrange(0, min(4, candidate_count + 1))
            ranking = rng.sample(candidates, length) if length else []
            if tuple(ranking) in used_rankings:
                continue
            used_rankings.add(tuple(ranking))
            ballots.append({
                "id": f"g{j}",
                "ranking": ranking,
                "weight": rng.randrange(1, 4),
            })
        record_count = rng.randrange(0, 8)
        records = []
        for i in range(record_count):
            if ballots and rng.random() < 0.7:
                base = list(rng.choice(ballots)["ranking"])
                slots = [None if rng.random() < 0.4 else c for c in base]
            else:
                length = rng.randrange(0, 4)
                picks = rng.sample(candidates, min(length, candidate_count))
                slots = picks + [None] * (length - len(picks))
                # de-duplicate known candidates by dropping repeated positions
                clean = []
                seen = set()
                for value in slots:
                    if value is None or value not in seen:
                        clean.append(value)
                        seen.add(value)
                slots = clean
            records.append((f"s{i}", slots))
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            report = make_report(candidates, ballots)
            rp = write_report(td, report)
            sp = write_scans(td, [{"id": sid, "slots": slots}
                                  for sid, slots in records])
            payload = link_records(rp, sp)
            feasible, possible = brute_force(records, ballots)
            if feasible:
                verify_feasible_payload(self, payload, report, records)
                for row in payload["records"]:
                    expected = possible[row["scan_id"]]
                    self.assertEqual(set(row["possible_ballot_ids"]), expected)
                    self.assertEqual(
                        row["classification"],
                        "forced" if len(expected) == 1 else "ambiguous")
            else:
                verify_infeasible_payload(self, payload, report, records)

    def test_many_random_instances(self):
        for seed in range(250):
            self.run_seed(seed)


# --------------------------------------------------------------------------- #
# Strict input validation
# --------------------------------------------------------------------------- #

class ValidationTests(TempDir):
    def setUp(self):
        super().setUp()
        self.report = make_report(["A", "B"], [
            {"id": "g1", "ranking": ["A", "B"], "weight": 1}])
        self.rp = write_report(self.tmp, self.report)

    def expect_link_error(self, report_path=None, scans_path=None):
        with self.assertRaises(LinkError):
            link_records(report_path or self.rp, scans_path or self.good_scans())

    def good_scans(self, name="good.json"):
        return write_scans(self.tmp, [{"id": "s1", "slots": ["A", None]}],
                           name=name)

    def test_scans_shape_errors(self):
        bad_bodies = [
            "{}",
            '{"records":[],"records":[]}',
            '{"records":[{}]}',
            '{"records":[{"id":"s1"}]}',
            '{"records":[{"slots":[]}]}',
            '{"records":[{"id":"s1","slots":[],"extra":1}]}',
            '{"records":[{"id":"s1","slots":"A"}]}',
            '{"records":[{"id":"s1","slots":[null,1]}]}',
            '{"records":[{"id":"s1","slots":[["A"]]}]}',
            '{"records":[{"id":" s1","slots":[]}]}',
            '{"records":[{"id":"","slots":[]}]}',
            '{"records":[{"id":"s1","slots":' + '[null,' * 100 + 'null]}]}',
            "NaN",
            '{"records":[NaN]}',
        ]
        for body in bad_bodies:
            sp = Path(self.tmp) / "bad.json"
            sp.write_text(body, encoding="utf-8")
            with self.assertRaises(LinkError, msg=body):
                link_records(self.rp, sp)

    def test_duplicate_scan_id_rejected(self):
        sp = write_scans(self.tmp, [
            {"id": "s1", "slots": []}, {"id": "s1", "slots": []}])
        self.expect_link_error(scans_path=sp)

    def test_duplicate_known_candidate_rejected(self):
        sp = write_scans(self.tmp, [
            {"id": "s1", "slots": ["A", None, "A"]}])
        self.expect_link_error(scans_path=sp)

    def test_unknown_candidate_rejected(self):
        sp = write_scans(self.tmp, [{"id": "s1", "slots": ["Z"]}])
        self.expect_link_error(scans_path=sp)

    def test_boolean_and_float_rejected(self):
        sp = Path(self.tmp) / "b.json"
        sp.write_text('{"records":[{"id":true,"slots":[]}]}')
        self.expect_link_error(scans_path=sp)
        report = dict(self.report)
        report["ballots"] = [{"id": "g1", "ranking": ["A", "B"], "weight": 1.5}]
        rp = write_report(self.tmp, report, name="float.json",
                          raw=json.dumps(report))
        sp = self.good_scans(name="for-float.json")
        with self.assertRaises(LinkError):
            link_records(rp, sp)

    def test_too_many_records_rejected(self):
        sp = write_scans(self.tmp, [
            {"id": f"s{i:05d}", "slots": []} for i in range(5001)])
        self.expect_link_error(scans_path=sp)

    def test_report_validation_errors(self):
        good_ledger = {
            "candidates": self.report["candidates"],
            "ballots": self.report["ballots"],
        }
        variants = []
        # wrong schema version
        bad = dict(self.report); bad["schema_version"] = 1
        variants.append(bad)
        # revision out of range / boolean
        bad = dict(self.report); bad["revision"] = -1
        variants.append(bad)
        bad = dict(self.report); bad["revision"] = 2**63
        variants.append(bad)
        bad = dict(self.report); bad["revision"] = True
        variants.append(bad)
        # unknown / missing top level field
        bad = dict(self.report); bad["extra"] = 1
        variants.append(bad)
        bad = {k: v for k, v in self.report.items() if k != "result"}
        variants.append(bad)
        # unsorted candidates / ballots
        bad = dict(self.report); bad["candidates"] = ["B", "A"]
        variants.append(bad)
        bad = dict(self.report)
        bad["ballots"] = list(reversed(self.report["ballots"]))
        variants.append(bad)
        # ballot with unknown field
        bad = dict(self.report)
        bad["ballots"] = [dict(self.report["ballots"][0], x=1)]
        variants.append(bad)
        # duplicate ballot id, unknown candidate, duplicate ranking entry.
        # These violate analyze(), so build the report-shaped object by hand.
        def raw_report(candidates, ballots):
            ledger = {"candidates": sorted(candidates),
                      "ballots": sorted(ballots, key=lambda b: b["id"])}
            return {
                "schema_version": 2,
                "revision": 0,
                "candidates": ledger["candidates"],
                "ballots": ledger["ballots"],
                "snapshot_sha256": hashlib.sha256(
                    canonical_bytes(ledger)).hexdigest(),
                "result": {"winner": None},
            }

        variants.append(raw_report(
            ["A", "B"], [{"id": "g1", "ranking": ["A", "A"], "weight": 1}]))
        variants.append(raw_report(
            ["A"], [{"id": "g1", "ranking": ["Z"], "weight": 1}]))
        variants.append(raw_report(["A"], [
            {"id": "g1", "ranking": ["A"], "weight": 1},
            {"id": "g1", "ranking": ["A"], "weight": 1}]))
        variants.append(raw_report(
            ["A"], [{"id": "g1", "ranking": ["A"], "weight": 0}]))
        # damaged snapshot and result
        bad = dict(self.report); bad["snapshot_sha256"] = "0" * 64
        variants.append(bad)
        bad = dict(self.report); bad["result"] = analyze(good_ledger) | {"winner": "Z"}
        variants.append(bad)
        for index, bad_report in enumerate(variants):
            rp = write_report(self.tmp, bad_report, name=f"bad-{index}.json",
                              raw=json.dumps(bad_report))
            with self.assertRaises(LinkError, msg=str(index)):
                link_records(rp, self.good_scans(name=f"g-{index}.json"))
        # revision exactly at the upper bound is accepted.
        edge = dict(self.report); edge["revision"] = 2**63 - 1
        rp = write_report(self.tmp, edge, name="rev-edge.json")
        payload = link_records(rp, self.good_scans(name="rev-edge-scan.json"))
        self.assertEqual(payload["status"], "feasible")

    def test_non_canonical_report_bytes_rejected(self):
        rp = Path(self.tmp) / "pretty.json"
        rp.write_text(json.dumps(self.report, indent=4), encoding="utf-8")
        self.expect_link_error(report_path=rp)

    def test_corrupt_canonical_bytes_rejected(self):
        raw = canonical_bytes(self.report)
        rp = Path(self.tmp) / "corrupt.json"
        rp.write_bytes(raw[:-2] + b"x\n")
        with self.assertRaises(LinkError):
            link_records(rp, self.good_scans())

    def test_duplicate_json_key_in_report_rejected(self):
        rp = Path(self.tmp) / "dup.json"
        rp.write_text(
            canonical_bytes(self.report).decode("utf-8").replace(
                '"revision":0', '"revision":0,"revision":1', 1),
            encoding="utf-8")
        with self.assertRaises(LinkError):
            link_records(rp, self.good_scans())

    def test_size_limits_enforced(self):
        big_scans = Path(self.tmp) / "big-scans.json"
        big_scans.write_text('{"records":[]}' + " " * (8_000_001),
                             encoding="utf-8")
        self.expect_link_error(scans_path=big_scans)
        big_report = Path(self.tmp) / "big-report.json"
        big_report.write_text(canonical_bytes(self.report).decode("utf-8")
                              + " " * (32_000_001), encoding="utf-8")
        sp = self.good_scans()
        with self.assertRaises(LinkError):
            link_records(big_report, sp)

    def test_compatibility_edge_bound(self):
        # 101 fully-unconstrained length-1 scans against 2000 distinct groups:
        # 202000 compatibility edges, over the 200000 bound.
        candidates = [f"c{i}" for i in range(100)]
        ballots = []
        for j in range(2000):
            ballots.append({"id": f"g{j:04d}",
                            "ranking": [candidates[j % 100]], "weight": 1})
        report = make_report(candidates, ballots)
        rp = write_report(self.tmp, report, name="edges.json")
        sp = write_scans(self.tmp, [
            {"id": f"s{j:03d}", "slots": [None]} for j in range(101)],
                         name="edge-scans.json")
        with self.assertRaises(LinkError):
            link_records(rp, sp)


# --------------------------------------------------------------------------- #
# Real CLI
# --------------------------------------------------------------------------- #

class CliTests(TempDir):
    def cli(self, *args, expect_rc=0):
        launcher = Path(self.tmp) / "run_link_cli.py"
        launcher.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from ranked_ballots.link_cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-I", str(launcher), *map(str, args)],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stderr)
        return proc

    def test_cli_feasible_infeasible_and_determinism(self):
        report = make_report(["A", "B", "C"], [
            {"id": "g1", "ranking": ["A", "B"], "weight": 1},
            {"id": "g2", "ranking": ["A", "C"], "weight": 1},
        ])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [
            {"id": "s1", "slots": ["A", None]},
            {"id": "s2", "slots": ["A", "B"]}])
        out = Path(self.tmp) / "out.json"
        proc = self.cli("--report", rp, "--scans", sp, "--output", out)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "feasible")
        # stdout payload equals the published file (sorted-keys pretty JSON),
        # byte-identical on re-run.
        first = out.read_bytes()
        self.assertEqual(json.loads(first), payload)
        self.cli("--report", rp, "--scans", sp, "--output", out)
        self.assertEqual(out.read_bytes(), first)

        sp2 = write_scans(self.tmp, [
            {"id": f"s{i}", "slots": ["A", None]} for i in range(3)],
            name="bad-scans.json")
        out2 = Path(self.tmp) / "out2.json"
        proc = self.cli("--report", rp, "--scans", sp2, "--output", out2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "infeasible")
        self.assertIsNone(payload["assignment"])
        verify_infeasible_payload(
            self, payload, report,
            [(f"s{i}", ["A", None]) for i in range(3)])
        self.assertEqual(out2.read_bytes(), json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
            indent=2).encode("utf-8") + b"\n")

    def test_cli_failure_preserves_existing_output(self):
        report = make_report(["A"], [{"id": "g1", "ranking": ["A"], "weight": 1}])
        rp = write_report(self.tmp, report)
        good = write_scans(self.tmp, [{"id": "s1", "slots": ["A"]}])
        out = Path(self.tmp) / "out.json"
        self.cli("--report", rp, "--scans", good, "--output", out)
        before = out.read_bytes()
        malformed = Path(self.tmp) / "malformed.json"
        malformed.write_text('{"records":[{"id":"x","slots":["Z"]}]}',
                             encoding="utf-8")
        proc = self.cli("--report", rp, "--scans", malformed,
                        "--output", out, expect_rc=2)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual(out.read_bytes(), before)
        # Edge-bound failure also preserves.
        candidates = [f"c{i}" for i in range(100)]
        big = make_report(candidates, [
            {"id": f"g{j:04d}", "ranking": [candidates[j % 100]], "weight": 1}
            for j in range(2000)])
        rp_big = write_report(self.tmp, big, name="big.json")
        sp_big = write_scans(self.tmp, [
            {"id": f"s{j:03d}", "slots": [None]} for j in range(101)],
            name="big-scans.json")
        self.cli("--report", rp_big, "--scans", sp_big,
                 "--output", out, expect_rc=2)
        self.assertEqual(out.read_bytes(), before)

    def test_cli_refuses_same_path_and_symlink_output(self):
        report = make_report(["A"], [{"id": "g1", "ranking": ["A"], "weight": 1}])
        rp = write_report(self.tmp, report)
        sp = write_scans(self.tmp, [{"id": "s1", "slots": ["A"]}])
        self.cli("--report", rp, "--scans", sp, "--output", rp, expect_rc=2)
        self.cli("--report", rp, "--scans", sp, "--output", sp, expect_rc=2)
        link = Path(self.tmp) / "link.json"
        link.symlink_to(rp)
        self.cli("--report", rp, "--scans", sp, "--output", link, expect_rc=2)

    def test_cli_large_chain_real_subprocess(self):
        builder = LargeChainTests()
        builder.setUp()
        try:
            report, ballots, records, _raw = builder._build(
                with_pins=True, extra_right_pin=False)
            layer_two = [{
                "id": "b-" + record["id"],
                "slots": list(record["slots"]),
            } for record in records]
            records = records + layer_two
            for ballot in ballots:
                ballot["weight"] *= 2
            report = make_report(report["candidates"], ballots)
            rp = write_report(self.tmp, report, name="chain-report.json")
            sp = write_scans(self.tmp, records, name="chain-scans.json")
            out = Path(self.tmp) / "chain-out.json"
            proc = self.cli("--report", rp, "--scans", sp, "--output", out)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "feasible")
            self.assertGreaterEqual(len(payload["records"]), 1000)
            self.assertTrue(all(r["classification"] == "forced"
                                for r in payload["records"]))
            # The published file is ordinary deterministic JSON and re-runs
            # byte-identically.
            first = out.read_bytes()
            self.cli("--report", rp, "--scans", sp, "--output", out)
            self.assertEqual(out.read_bytes(), first)
        finally:
            builder.tearDown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
