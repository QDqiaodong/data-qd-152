"""Suppression planning for a first-choice public summary table.

``ranked-ballots-disclosure`` is a read-only planning tool over an offline
multi-precinct first-choice tally.  It never changes the v1 count, the archive,
the paper-ballot audit, scan reconciliation, the pairwise view or the
withdrawal sensitivity report.

Input contract
--------------
The top level is exactly ``{precincts, protect, optional}``:

* ``precincts`` holds 1..8 objects ``{id, candidates, ballots}`` with unique
  precinct ids.  Every precinct is an independent v1 ballot-group input: the
  candidate set is identical across precincts (1..5 unique candidates) and each
  ballot group keeps its own ``id``, ``ranking`` and integer ``weight``
  (1..10^9) under the existing v1 rules.  Ballot-group ids only have to be
  unique *inside* a precinct, and all precincts together hold at most 20,000
  groups.  Groups are never expanded by weight.
* ``protect`` is a non-empty array of coordinates ``{precinct, candidate}``
  that must be suppressed; ``optional`` lists at most ten further coordinates
  that may be suppressed.  ``candidate = null`` selects the blank column and is
  never confused with a candidate id.  Every coordinate must exist, each array
  is duplicate-free, and the two arrays are disjoint.

The public table has one row per precinct and one column per candidate id
(ascending) plus a final blank column.  A group with a non-empty ranking adds
its whole weight to the first-ranked candidate; an empty ranking adds its
whole weight to the blank column.  Row totals and aligned column totals are
published as well.

Planning semantics
------------------
A plan adds a subset of the allowed optional cells to the mandatory cells.
With the plan fixed, the visible cell values and *all* row and column totals
stay public; every suppressed cell may take any non-negative integer value.
A mandatory cell is protected by the plan when it can take at least two
different values across the compatible integer tables (a different compatible
table may witness each mandatory cell).  The input ballot-group contents and
weights are not public constraints: the alternative tables only satisfy the
published cells and marginal totals.

The plan with the fewest additional suppressions is chosen; among equally
large plans the lexicographically smallest sorted coordinate sequence wins.
Coordinates order by precinct id, then candidate id, with the blank column
after every candidate id.

Each plan is evaluated from scratch: the visible cells left by that plan drive
the test for every mandatory cell, and no inference is carried over between
plans.  The check never enumerates candidate cell values.  From the true
table (always feasible, since it produced the published cells and totals),
residual arcs exist only for suppressed cells; a mandatory cell can change
exactly when the residual network contains a cycle through it.  Reachability
answers that question for arbitrary integer magnitudes, so weights up to 10^9
cost nothing extra.

This is a single-release *inferential disclosure* indicator.  It does not
promise protection against external information, earlier releases, or
inference about the suppression process itself.

Output
------
The published report holds only the public summary table, its margins and the
suppressed positions -- never the internal feasible table, a suppressed cell's
true value or a digest of the source file.  It has exactly five fields:

* ``schema_version``: ``1``;
* ``columns``: candidate ids in ascending order followed by ``null``;
* ``column_totals``: one integer per column (the blank total included);
* ``rows``: precincts in ascending id order, each ``{precinct, total, values}``
  with ``values`` aligned to ``columns`` and ``null`` exactly at suppressed
  cells (visible zeros stay integers);
* ``suppressed``: every suppressed coordinate in coordinate order, in the same
  ``{precinct, candidate}`` form as the input, mandatory and additional cells
  alike.

The report is written as UTF-8.  Reordering equivalent input precincts,
candidates, ballot groups or either coordinate array leaves the published
bytes unchanged.  An unsatisfiable plan or any input/write failure exits 2 with
a reason on stderr that never reveals a cell value; the input and output paths
must differ, an output symlink is refused, a failed run keeps the previous
output bytes and a successful run replaces the output atomically.

Only the Python standard library is used.
"""
from __future__ import annotations

import itertools
from pathlib import Path

from .common import ident, read, require, rows, save
from .pairwise import validate as validate_ledger

MAX_PRECINCTS = 8
MAX_CANDIDATES = 5
MAX_BALLOT_GROUPS = 20_000
MAX_OPTIONAL = 10
SCHEMA_VERSION = 1


class DisclosureError(ValueError):
    """Expected, explainable suppression-planning failure (CLI exit code 2)."""


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #

def validate_precinct(item):
    """Apply the existing v1 ballot-group rules to one precinct.

    ``pairwise.validate`` mirrors the v1 model exactly: strict fields, unique
    ballot ids, integer weights in 1..10^9, duplicate-free rankings, known
    candidates.  Its 100-candidate / 20,000-group limits are not stricter than
    the precinct's own bounds, which the caller checks separately.
    """
    return validate_ledger({
        "candidates": item["candidates"],
        "ballots": item["ballots"],
    })


def _parse_coordinates(value, name, limit, precincts, candidates):
    """Validate one coordinate array against the known precincts/candidates."""
    if not isinstance(value, list):
        raise ValueError(name + " must be an array")
    if len(value) > limit:
        raise ValueError(name + " exceeds " + str(limit) + " coordinates")
    result = []
    seen = set()
    for item in value:
        require(item, "precinct candidate")
        precinct = ident(item["precinct"])
        if precinct not in precincts:
            raise ValueError(name + " references an unknown precinct")
        raw_candidate = item["candidate"]
        if raw_candidate is None:
            candidate = None
        else:
            candidate = ident(raw_candidate)
            if candidate not in candidates:
                raise ValueError(name + " references an unknown candidate")
        coordinate = (precinct, candidate)
        if coordinate in seen:
            raise ValueError(name + " contains a duplicate coordinate")
        seen.add(coordinate)
        result.append(coordinate)
    return result


def _validate(data):
    """Enforce the whole contract and return the normalised public table.

    Each precinct is validated by the existing v1 ballot-group validator
    (:func:`ranked_ballots.pairwise.validate`, whose rules mirror ``analyze``),
    so ``id``/``ranking``/``weight`` handling cannot drift from the count.
    Precinct-specific bounds (1..5 shared candidates, 8 precincts, 20,000
    groups in total) are checked around it.
    """
    require(data, "precincts protect optional")
    raw_precincts = rows(data["precincts"], MAX_PRECINCTS)
    if not raw_precincts:
        raise ValueError("expected 1..8 precincts")

    tally_by_precinct = {}
    total_by_precinct = {}
    candidates = None
    total_groups = 0
    precinct_ids = set()
    for item in raw_precincts:
        require(item, "id candidates ballots")
        precinct = ident(item["id"])
        if precinct in precinct_ids:
            raise ValueError("duplicate precinct id: " + precinct)
        precinct_ids.add(precinct)
        listed, ballots = validate_precinct(item)
        listed_candidates = set(listed)
        if len(listed) > MAX_CANDIDATES:
            raise ValueError(
                "precinct " + precinct + " requires 1..5 candidates"
            )
        if candidates is None:
            candidates = listed_candidates
        elif listed_candidates != candidates:
            raise ValueError("all precincts must share one candidate set")
        total_groups += len(ballots)
        if total_groups > MAX_BALLOT_GROUPS:
            raise ValueError("ballot groups across precincts exceed 20000")
        tally = {}
        total = 0
        for ranking, weight in ballots:
            first = ranking[0] if ranking else None
            tally[first] = tally.get(first, 0) + weight
            total += weight
        tally_by_precinct[precinct] = tally
        total_by_precinct[precinct] = total

    protect = _parse_coordinates(
        data["protect"], "protect",
        MAX_PRECINCTS * (MAX_CANDIDATES + 1), precinct_ids, candidates,
    )
    if not protect:
        raise ValueError("protect must not be empty")
    optional = _parse_coordinates(
        data["optional"], "optional", MAX_OPTIONAL, precinct_ids, candidates,
    )
    if set(protect) & set(optional):
        raise ValueError("protect and optional coordinates must be disjoint")

    columns = sorted(candidates) + [None]
    row_ids = sorted(tally_by_precinct)
    matrix = [
        [tally_by_precinct[precinct].get(column, 0) for column in columns]
        for precinct in row_ids
    ]
    row_totals = [total_by_precinct[precinct] for precinct in row_ids]
    column_totals = [
        sum(matrix[i][j] for i in range(len(row_ids)))
        for j in range(len(columns))
    ]
    return {
        "rows": row_ids,
        "columns": columns,
        "matrix": matrix,
        "row_totals": row_totals,
        "column_totals": column_totals,
        "protect": protect,
        "optional": optional,
    }


# --------------------------------------------------------------------------- #
# Residual-reachability protection test
# --------------------------------------------------------------------------- #

def _coordinate_order(coordinate):
    """Sort key: precinct id, then candidate id, blank column last."""
    precinct, candidate = coordinate
    if candidate is None:
        return (precinct, 1, "")
    return (precinct, 0, candidate)


def _residual_graph(matrix, suppressed):
    """Residual arcs of the true table under one plan.

    Visible cells are fixed at their published value and contribute no arc.
    A suppressed cell ``(i, j)`` with true value ``v`` can always grow (there
    is no individual upper bound -- row and column totals are preserved) and
    can shrink only while ``v > 0``: that is a forward arc ``row_i -> col_j``
    and, when ``v > 0``, a reverse arc ``col_j -> row_i`` of residual capacity
    ``v``.  Only the *presence* of a positive residual matters, so magnitudes
    are never enumerated.
    """
    row_count = len(matrix)
    column_count = len(matrix[0])
    adjacency = [[] for _ in range(row_count + column_count)]
    for i in range(row_count):
        for j in range(column_count):
            if not suppressed[i][j]:
                continue
            adjacency[i].append((row_count + j, i, j))
            if matrix[i][j] > 0:
                adjacency[row_count + j].append((i, i, j))
    return adjacency


def _reaches(adjacency, start, goal, skipped):
    """Is ``goal`` reachable from ``start`` without touching ``skipped``?"""
    if start == goal:
        return False
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for neighbour, cell_i, cell_j in adjacency[node]:
            if cell_i == skipped[0] and cell_j == skipped[1]:
                continue
            if neighbour == goal:
                return True
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return False


def _cell_forced(matrix, adjacency, row_count, cell):
    """Is this suppressed cell fixed to one value in every compatible table?"""
    i, j = cell
    column_node = row_count + j
    # Growing the cell needs a residual path from its column back to its row;
    # shrinking it needs the opposite path plus a positive current value.
    if _reaches(adjacency, column_node, i, cell):
        return False
    if matrix[i][j] > 0 and _reaches(adjacency, i, column_node, cell):
        return False
    return True


def _plan_is_valid(matrix, mandatory, extra):
    """Recompute protection for every mandatory cell under one plan."""
    row_count = len(matrix)
    column_count = len(matrix[0])
    suppressed = [[False] * column_count for _ in range(row_count)]
    for i, j in mandatory:
        suppressed[i][j] = True
    for i, j in extra:
        suppressed[i][j] = True
    adjacency = _residual_graph(matrix, suppressed)
    return all(
        not _cell_forced(matrix, adjacency, row_count, cell)
        for cell in mandatory
    )


def _select_plan(matrix, mandatory, optional):
    """Fewest extra suppressions; lexicographic tie-break on coordinates.

    At most ten optional cells give at most 1024 subsets, and each subset is a
    *plan* test -- the possible cell values are never enumerated.  Adding
    suppressions can only grow the feasible set, but the tie-break still needs
    the ordered enumeration below.
    """
    ordered = sorted(optional, key=_coordinate_order)
    for size in range(len(ordered) + 1):
        for extra in itertools.combinations(ordered, size):
            if _plan_is_valid(matrix, mandatory, extra):
                return list(extra)
    raise DisclosureError(
        "no suppression plan protects every required cell"
    )


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def _report(spec, extra):
    """Assemble the public report for the chosen plan."""
    row_ids = spec["rows"]
    columns = spec["columns"]
    matrix = spec["matrix"]
    row_index = {precinct: i for i, precinct in enumerate(row_ids)}
    column_index = {column: j for j, column in enumerate(columns)}
    mandatory_indices = {
        (row_index[p], column_index[c]) for p, c in spec["protect"]
    }
    extra_indices = {
        (row_index[p], column_index[c]) for p, c in extra
    }
    suppressed_indices = mandatory_indices | extra_indices

    rows_report = []
    for i, precinct in enumerate(row_ids):
        rows_report.append({
            "precinct": precinct,
            "total": spec["row_totals"][i],
            "values": [
                None if (i, j) in suppressed_indices else matrix[i][j]
                for j in range(len(columns))
            ],
        })

    suppressed = sorted(
        ((row_ids[i], columns[j]) for i, j in suppressed_indices),
        key=_coordinate_order,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "columns": list(columns),
        "column_totals": list(spec["column_totals"]),
        "rows": rows_report,
        "suppressed": [
            {"precinct": precinct, "candidate": candidate}
            for precinct, candidate in suppressed
        ],
    }


def disclosure_analysis(data):
    """Return the deterministic suppression report for parsed input.

    ``data`` is the already parsed ``{precincts, protect, optional}`` object.
    Performs no I/O and raises :class:`DisclosureError` when no plan exists.
    """
    spec = _validate(data)
    row_index = {precinct: i for i, precinct in enumerate(spec["rows"])}
    column_index = {
        column: j for j, column in enumerate(spec["columns"])
    }
    mandatory = [
        (row_index[precinct], column_index[candidate])
        for precinct, candidate in spec["protect"]
    ]
    optional = [
        (row_index[precinct], column_index[candidate])
        for precinct, candidate in spec["optional"]
    ]
    extra = _select_plan(spec["matrix"], mandatory, optional)
    coordinates = [
        (spec["rows"][i], spec["columns"][j]) for i, j in extra
    ]
    return _report(spec, coordinates)


def disclosure_report(source, output):
    """Path API: read ``source`` and atomically publish the report."""
    source_path = Path(source)
    output_path = Path(output)
    if source_path.resolve() == output_path.resolve():
        raise ValueError("input and output must differ")
    data, _digest = read(source_path)
    report = disclosure_analysis(data)
    save(output_path, report)
    return report


# Convenience alias matching the other read-only report modules.
disclosure = disclosure_report
