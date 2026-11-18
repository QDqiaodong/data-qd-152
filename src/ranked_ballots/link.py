"""Capacity reconciliation between partial scan transcriptions and a fixed v2 ledger.

A scan transcription records only the ranking positions a transcriber could read:
a list whose length is the full ranking length, with ``null`` for an unreadable
position.  An electronic ballot *group* (a v2 report ``ballots`` entry, keyed by
its ``id``) aggregates identical rankings with a ``weight`` capacity.  This
module checks whether every scanned ticket can simultaneously belong to the
given groups under two global rules:

1. a scan is compatible with a group only when the lengths are equal and every
   readable (non-null) position equals the ranking position;
2. each scan is assigned to exactly one compatible group and the number of
   scans assigned to a group never exceeds its ``weight``; unused capacity is
   allowed.

The check is purely combinatorial consistency of the supplied facts.  It never
modifies the report or any audit activity, never counts winners and never
certifies statistics.  It also cannot prove that the scans really originate from
physical tickets or that the electronic ledger is correct.

Algorithm (work scales with group count, scan count and compatibility edges;
weights are never expanded into individual tickets and complete assignments are
never enumerated):

* one bipartite b-matching max flow (source -> scans -> groups -> sink, Dinic);
* a single residual-graph SCC computation classifies *every* compatibility edge
  at once: an unsaturated edge (u, v) occurs in some maximum flow exactly when u
  and v are in the same residual SCC, while a saturated edge already occurs in
  the flow found -- so no per-edge flow rerun is needed;
* an infeasible instance yields a Hall witness from the residual reachable set:
  the scans reached from the source strictly outnumber the total weight of all
  their neighbouring groups, and the deficit is positive.

Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path

from .archive import canonical_bytes
from .common import ident
from .model import analyze

MAX_REPORT_BYTES = 32_000_000
MAX_SCANS_BYTES = 8_000_000
MAX_CANDIDATES = 100
MAX_BALLOTS = 20_000
MAX_WEIGHT = 10**9
MAX_RECORDS = 5_000
MAX_SLOTS = 100
MAX_COMPAT_EDGES = 200_000
SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 2
REPORT_KEYS = {
    "schema_version",
    "revision",
    "candidates",
    "ballots",
    "snapshot_sha256",
    "result",
}
_HEX64 = "0123456789abcdef"


class LinkError(Exception):
    """Expected, explainable reconciliation failure.  The CLI maps this to 2."""


# --------------------------------------------------------------------------- #
# Strict JSON reading
# --------------------------------------------------------------------------- #

def _read_json(path, limit, label):
    target = Path(path)
    try:
        with target.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as error:
        raise LinkError("cannot read " + label + ": " + str(error)) from error
    if len(raw) > limit:
        raise LinkError(label + " exceeds " + str(limit // 1_000_000) + " MB")
    if not raw:
        raise LinkError("empty " + label)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LinkError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def invalid_constant(value):
        raise LinkError("non-finite JSON number: " + value)

    def invalid_float(value):
        raise LinkError("JSON numbers must be integers, not floats: " + value)

    try:
        obj = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=invalid_constant,
            parse_float=invalid_float,
        )
    except LinkError:
        raise
    except (ValueError, UnicodeDecodeError) as error:
        raise LinkError("invalid JSON in " + label + ": " + str(error)) from error
    return obj, raw


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise LinkError(label + " must be an integer in " + str(minimum) + ".." + str(maximum))
    return value


def _identifier(value, label):
    try:
        return ident(value)
    except (ValueError, TypeError) as error:
        raise LinkError(label + ": " + str(error)) from error


# --------------------------------------------------------------------------- #
# v2 report validation (archive contract, without the audit's 12-candidate limit)
# --------------------------------------------------------------------------- #

def _validate_report(obj, raw):
    if not isinstance(obj, dict) or set(obj) != REPORT_KEYS:
        raise LinkError(
            "report must have exactly schema_version, revision, candidates, "
            "ballots, snapshot_sha256, result"
        )
    if type(obj["schema_version"]) is not int or obj["schema_version"] != REPORT_SCHEMA_VERSION:
        raise LinkError("report schema_version must be the integer 2")
    _integer(obj["revision"], 0, 2**63 - 1, "report revision")
    snapshot = obj["snapshot_sha256"]
    if not isinstance(snapshot, str) or len(snapshot) != 64 \
            or any(ch not in _HEX64 for ch in snapshot):
        raise LinkError("snapshot_sha256 must be 64 lowercase hex characters")

    candidates_obj = obj["candidates"]
    if not isinstance(candidates_obj, list) or not 1 <= len(candidates_obj) <= MAX_CANDIDATES:
        raise LinkError("candidates must contain 1.." + str(MAX_CANDIDATES) + " identifiers")
    candidates = [_identifier(c, "candidate id") for c in candidates_obj]
    if len(set(candidates)) != len(candidates):
        raise LinkError("duplicate candidate id")
    if candidates != sorted(candidates):
        raise LinkError("candidates must be sorted ascending by id")
    candidate_set = set(candidates)

    ballots_obj = obj["ballots"]
    if not isinstance(ballots_obj, list) or len(ballots_obj) > MAX_BALLOTS:
        raise LinkError("ballots must be an array of at most " + str(MAX_BALLOTS) + " entries")
    ballots = []
    seen_ballot_ids = set()
    ballot_ids = []
    for entry in ballots_obj:
        if not isinstance(entry, dict) or set(entry) != {"id", "ranking", "weight"}:
            raise LinkError("each ballot must have exactly id, ranking, weight")
        bid = _identifier(entry["id"], "ballot id")
        ranking_obj = entry["ranking"]
        if not isinstance(ranking_obj, list) or len(ranking_obj) > MAX_SLOTS:
            raise LinkError("ballot ranking must be an array of at most " + str(MAX_SLOTS))
        ranking = [_identifier(c, "ranking candidate") for c in ranking_obj]
        if len(set(ranking)) != len(ranking):
            raise LinkError("ballot ranking contains a duplicate candidate: " + bid)
        if any(c not in candidate_set for c in ranking):
            raise LinkError("ballot ranking references an unknown candidate: " + bid)
        weight = _integer(entry["weight"], 1, MAX_WEIGHT, "ballot weight")
        if bid in seen_ballot_ids:
            raise LinkError("duplicate ballot id: " + bid)
        seen_ballot_ids.add(bid)
        ballot_ids.append(bid)
        ballots.append({"id": bid, "ranking": ranking, "weight": weight})
    if ballot_ids != sorted(ballot_ids):
        raise LinkError("ballots must be sorted ascending by id")

    ledger = {"candidates": candidates, "ballots": ballots}
    if _sha256(canonical_bytes(ledger)) != snapshot:
        raise LinkError(
            "snapshot_sha256 does not match the canonical {candidates, ballots} bytes"
        )
    try:
        expected_result = analyze(ledger)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise LinkError("ballots violate the analyze contract: " + str(error)) from error
    try:
        if canonical_bytes(obj["result"]) != canonical_bytes(expected_result):
            raise LinkError("report result does not equal analyze(candidates, ballots)")
    except (TypeError, ValueError) as error:
        raise LinkError("report result is not canonical JSON: " + str(error)) from error
    # The supplied report bytes themselves must be the immutable canonical encoding.
    if raw != canonical_bytes(obj):
        raise LinkError("report bytes must equal archive.canonical_bytes(report)")
    return {
        "candidates": candidates,
        "ballots": ballots,
        "snapshot_sha256": snapshot,
        "revision": obj["revision"],
    }


# --------------------------------------------------------------------------- #
# Scan transcription validation
# --------------------------------------------------------------------------- #

def _validate_scans(obj, candidate_set):
    if not isinstance(obj, dict) or set(obj) != {"records"}:
        raise LinkError("scans input must have exactly the records field")
    records_obj = obj["records"]
    if not isinstance(records_obj, list) or len(records_obj) > MAX_RECORDS:
        raise LinkError("records must be an array of at most " + str(MAX_RECORDS) + " entries")
    records = []
    seen_scan_ids = set()
    for entry in records_obj:
        if not isinstance(entry, dict) or set(entry) != {"id", "slots"}:
            raise LinkError("each record must have exactly id, slots")
        scan_id = _identifier(entry["id"], "scan id")
        if scan_id in seen_scan_ids:
            raise LinkError("duplicate scan id: " + scan_id)
        seen_scan_ids.add(scan_id)
        slots_obj = entry["slots"]
        if not isinstance(slots_obj, list) or not 0 <= len(slots_obj) <= MAX_SLOTS:
            raise LinkError("slots must be an array of length 0.." + str(MAX_SLOTS))
        slots = []
        known = []
        for position, value in enumerate(slots_obj):
            if value is None:
                slots.append(None)
                continue
            candidate = _identifier(value, "slots[" + str(position) + "] candidate")
            if candidate in known:
                raise LinkError("duplicate known candidate in slots of scan " + scan_id)
            if candidate not in candidate_set:
                raise LinkError(
                    "slots of scan " + scan_id + " reference an unknown candidate: " + candidate
                )
            known.append(candidate)
            slots.append(candidate)
        records.append((scan_id, slots))
    records.sort(key=lambda item: item[0])
    return records


# --------------------------------------------------------------------------- #
# Compatibility derivation (indexed; never scans ballot x record pairs blindly)
# --------------------------------------------------------------------------- #

def _derive_compatibility(records, ballots):
    """Return per-scan lists of compatible ballot indices and the total edge count.

    Ballot indices follow report order (ids are sorted ascending).  The derivation
    groups by ranking length and indexes only the positions actually constrained
    by the scans, so work is bounded by records, ballots and resulting edges.
    """
    n = len(records)
    edges = [[] for _ in range(n)]
    ballots_by_length = {}
    for index, ballot in enumerate(ballots):
        ballots_by_length.setdefault(len(ballot["ranking"]), []).append(index)
    scans_by_length = {}
    for index, (_scan_id, slots) in enumerate(records):
        scans_by_length.setdefault(len(slots), []).append(index)

    total_edges = 0
    for length, scan_indices in scans_by_length.items():
        group = ballots_by_length.get(length)
        if not group:
            continue
        constrained_positions = sorted({
            position
            for scan_index in scan_indices
            for position, value in enumerate(records[scan_index][1])
            if value is not None
        })
        # position -> candidate -> set of ballot indices within this length group
        position_index = {}
        for position in constrained_positions:
            mapping = {}
            for ballot_index in group:
                candidate = ballots[ballot_index]["ranking"][position]
                mapping.setdefault(candidate, set()).add(ballot_index)
            position_index[position] = mapping
        empty = frozenset()
        for scan_index in scan_indices:
            slots = records[scan_index][1]
            required = [
                (position, slots[position])
                for position in constrained_positions
                if slots[position] is not None
            ]
            if not required:
                compatible = set(group)
            else:
                candidate_sets = [
                    position_index[position].get(candidate, empty)
                    for position, candidate in required
                ]
                candidate_sets.sort(key=len)
                compatible = set(candidate_sets[0])
                for candidate_set in candidate_sets[1:]:
                    compatible &= candidate_set
            ordered = sorted(compatible)
            total_edges += len(ordered)
            if total_edges > MAX_COMPAT_EDGES:
                raise LinkError(
                    "derived compatibility edges exceed the bound of "
                    + str(MAX_COMPAT_EDGES)
                )
            edges[scan_index] = ordered
    return edges, total_edges


# --------------------------------------------------------------------------- #
# Dinic max flow over the bipartite capacity network
# --------------------------------------------------------------------------- #

class _Edge:
    __slots__ = ("to", "rev", "cap")

    def __init__(self, to, rev, cap):
        self.to = to
        self.rev = rev
        self.cap = cap


def _add_edge(graph, u, v, cap):
    forward = _Edge(v, len(graph[v]), cap)
    backward = _Edge(u, len(graph[u]), 0)
    graph[u].append(forward)
    graph[v].append(backward)
    return forward


def _max_flow(graph, source, sink, required):
    """Dinic; stops once ``required`` units flow.  Network depth is at most 3."""
    total = 0
    size = len(graph)
    while total < required:
        level = [-1] * size
        level[source] = 0
        queue = deque((source,))
        while queue:
            u = queue.popleft()
            for edge in graph[u]:
                if edge.cap > 0 and level[edge.to] < 0:
                    level[edge.to] = level[u] + 1
                    queue.append(edge.to)
        if level[sink] < 0:
            break
        next_arc = [0] * size

        def augment(u, pushed):
            if u == sink:
                return pushed
            while next_arc[u] < len(graph[u]):
                edge = graph[u][next_arc[u]]
                if edge.cap > 0 and level[edge.to] == level[u] + 1:
                    sent = augment(edge.to, min(pushed, edge.cap))
                    if sent:
                        edge.cap -= sent
                        graph[edge.to][edge.rev].cap += sent
                        return sent
                next_arc[u] += 1
            return 0

        while total < required:
            pushed = augment(source, required - total)
            if not pushed:
                break
            total += pushed
    return total


# --------------------------------------------------------------------------- #
# Iterative Tarjan SCC over the residual graph (no recursion on input size)
# --------------------------------------------------------------------------- #

def _residual_components(graph):
    size = len(graph)
    index = [-1] * size
    low = [0] * size
    on_stack = [False] * size
    stack = []
    component = [-1] * size
    counter = 0
    component_count = 0
    for root in range(size):
        if index[root] != -1:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        work = [(root, 0)]
        while work:
            v, arc_position = work[-1]
            if arc_position < len(graph[v]):
                edge = graph[v][arc_position]
                work[-1] = (v, arc_position + 1)
                if edge.cap <= 0:
                    continue
                w = edge.to
                if index[w] == -1:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, 0))
                elif on_stack[w]:
                    if index[w] < low[v]:
                        low[v] = index[w]
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    if low[v] < low[parent]:
                        low[parent] = low[v]
                if low[v] == index[v]:
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        component[w] = component_count
                        if w == v:
                            break
                    component_count += 1
    return component


def _residual_reachable(graph, source):
    reached = [False] * len(graph)
    reached[source] = True
    queue = deque((source,))
    while queue:
        u = queue.popleft()
        for edge in graph[u]:
            if edge.cap > 0 and not reached[edge.to]:
                reached[edge.to] = True
                queue.append(edge.to)
    return reached


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def link_records(report_path, scans_path):
    """Reconcile one scan transcription file against one fixed v2 report.

    Returns the deterministic output payload (also written by the CLI).
    Raises :class:`LinkError` for any malformed input or exceeded bound.
    """
    report_obj, report_raw = _read_json(report_path, MAX_REPORT_BYTES, "report")
    scans_obj, scans_raw = _read_json(scans_path, MAX_SCANS_BYTES, "scans input")
    report = _validate_report(report_obj, report_raw)
    candidate_set = set(report["candidates"])
    ballots = report["ballots"]
    records = _validate_scans(scans_obj, candidate_set)
    edges, total_edges = _derive_compatibility(records, ballots)

    n = len(records)
    m = len(ballots)
    report_sha = _sha256(report_raw)
    scans_sha = _sha256(scans_raw)
    snapshot_sha = report["snapshot_sha256"]
    base = {
        "schema_version": SCHEMA_VERSION,
        "report_sha256": report_sha,
        "scans_sha256": scans_sha,
        "snapshot_sha256": snapshot_sha,
    }

    if n == 0:
        return {
            **base,
            "status": "feasible",
            "assignment": [],
            "records": [],
            "witness": None,
        }

    source = 0
    scan_node = lambda i: 1 + i  # noqa: E731
    ballot_node = lambda j: 1 + n + j  # noqa: E731
    sink = 1 + n + m
    graph = [[] for _ in range(sink + 1)]
    for i in range(n):
        _add_edge(graph, source, scan_node(i), 1)
    for j, ballot in enumerate(ballots):
        _add_edge(graph, ballot_node(j), sink, ballot["weight"])
    compatibility = []
    for i, ballot_indices in enumerate(edges):
        for j in ballot_indices:
            forward = _add_edge(graph, scan_node(i), ballot_node(j), 1)
            compatibility.append((i, j, forward))

    flow = _max_flow(graph, source, sink, n)
    if flow < n:
        reached = _residual_reachable(graph, source)
        scan_witness = [
            i for i in range(n) if reached[scan_node(i)]
        ]
        ballot_witness = [
            j for j in range(m) if reached[ballot_node(j)]
        ]
        # The residual construction makes reached ballots exactly N(S); assert
        # the equality from the derived adjacency so the witness is independently
        # re-derivable from the input alone.
        neighbor_set = set()
        for i in scan_witness:
            neighbor_set.update(edges[i])
        if neighbor_set != set(ballot_witness) or not scan_witness:
            raise LinkError("internal Hall witness construction failed")  # pragma: no cover
        capacity_sum = sum(ballots[j]["weight"] for j in ballot_witness)
        record_count = len(scan_witness)
        deficit = record_count - capacity_sum
        if deficit <= 0:
            raise LinkError("internal Hall deficit construction failed")  # pragma: no cover
        return {
            **base,
            "status": "infeasible",
            "assignment": None,
            "records": [],
            "witness": {
                "scan_ids": [records[i][0] for i in scan_witness],
                "ballot_ids": [ballots[j]["id"] for j in ballot_witness],
                "record_count": record_count,
                "capacity_sum": capacity_sum,
                "deficit": deficit,
            },
        }

    components = _residual_components(graph)
    assignment = []
    possible = [[] for _ in range(n)]
    saturated_edge = {}
    for i, j, forward in compatibility:
        if forward.cap == 0:  # unit capacity edge carrying the scan's one unit
            saturated_edge[i] = j
            possible[i].append(j)
        elif components[scan_node(i)] == components[ballot_node(j)]:
            # Residual u->v exists (flow 0); same SCC means v reaches u, so an
            # alternating cycle reroutes one unit onto this edge in some max flow.
            possible[i].append(j)
    for i in range(n):
        if i not in saturated_edge or not possible[i]:
            raise LinkError("internal assignment construction failed")  # pragma: no cover
        assignment.append({
            "scan_id": records[i][0],
            "ballot_id": ballots[saturated_edge[i]]["id"],
        })
    record_rows = []
    for i, (scan_id, _slots) in enumerate(records):
        ballot_ids = sorted(ballots[j]["id"] for j in possible[i])
        record_rows.append({
            "scan_id": scan_id,
            "possible_ballot_ids": ballot_ids,
            "classification": "forced" if len(ballot_ids) == 1 else "ambiguous",
        })
    return {
        **base,
        "status": "feasible",
        "assignment": assignment,
        "records": record_rows,
        "witness": None,
    }
