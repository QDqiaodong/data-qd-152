"""Independent pairwise preference analysis (``ranked-ballots-pairwise``).

A read-only, single-shot view over the same strict ``{candidates, ballots}``
contract used by the v1 IRV count.  It never replaces the IRV tally or the
paper-ballot audit: it only reports, for every unordered candidate pair, the
exact integer weight that ranks one candidate over the other.

Semantics
---------
For one ballot group with ranking ``r`` and weight ``w`` only the pairs that
are *expressed* -- both candidates present in ``r`` -- are counted: the
candidate earlier in ``r`` receives the full ``w`` for that pair.  A candidate
missing from ``r`` is never interpreted as a last-place preference, a tie or
an abstention that still counts; the group simply contributes nothing to any
pair that touches the missing candidate.  Empty rankings express nothing, and
groups with identical rankings each contribute their own weight.  All
accumulated values are exact integers; no floating point is involved.

Outputs (all derived from the one fully accumulated matrix, never frozen
before every ballot group has been read):

* ``comparisons`` -- every unordered pair exactly once, with both expressed
  weights, the unexpressed weight and the pairwise winner (null on a tie);
* ``majority_graph`` -- the strict majority digraph: an edge only where one
  side's expressed weight strictly exceeds the other's;
* ``strongly_connected_components`` -- a partition of all candidates;
* ``condorcet_winner`` -- the unique candidate with a direct strict-majority
  edge over every other candidate, else null (distinct from the IRV winner).

Only the Python standard library is used.
"""
from __future__ import annotations

from .common import ident, number, require, rows

MAX_CANDIDATES = 100
MAX_BALLOTS = 20_000
MAX_RANKING = 100
MAX_WEIGHT = 10**9


def validate(data):
    """Enforce the strict v1 ``{candidates, ballots}`` contract.

    Returns ``(candidates, ballots)`` with ballots as ``(ranking, weight)``
    tuples.  Raises :class:`ValueError` on any violation; the rules and error
    type deliberately mirror the v1 model (identifiers, unknown fields,
    duplicate keys already rejected while reading, integer weights).
    """
    require(data, "candidates ballots")
    candidates = [ident(x) for x in rows(data["candidates"], MAX_CANDIDATES)]
    if not candidates or len(set(candidates)) != len(candidates):
        raise ValueError("unique candidates required")
    known = set(candidates)
    ballots = []
    seen = set()
    for entry in rows(data["ballots"], MAX_BALLOTS):
        require(entry, "id ranking weight")
        bid = ident(entry["id"])
        ranking = [ident(x) for x in rows(entry["ranking"], MAX_RANKING)]
        weight = number(entry["weight"], 1, MAX_WEIGHT)
        if bid in seen:
            raise ValueError("duplicate ballot id: " + bid)
        if len(set(ranking)) != len(ranking):
            raise ValueError("duplicate candidate in ranking of ballot: " + bid)
        if any(x not in known for x in ranking):
            raise ValueError("ranking references an unknown candidate: " + bid)
        seen.add(bid)
        ballots.append((ranking, weight))
    return candidates, ballots


def _accumulate(ordered, ballots):
    """Build the complete expressed-preference matrix.

    ``matrix[i][j]`` is the exact weight ranking ``ordered[i]`` ahead of
    ``ordered[j]``.  Each ballot group touches only the pairs its ranking
    actually expresses, and each expressed pair exactly once (ranking entries
    are unique and positions are scanned with ``i < j``).  Identical rankings
    are first merged by weight: ``(w1 + w2)`` added to a pair equals each
    group contributing its own weight, and integer addition is commutative,
    so input order never affects the result.
    """
    n = len(ordered)
    position = {candidate: i for i, candidate in enumerate(ordered)}
    merged = {}
    total_weight = 0
    for ranking, weight in ballots:
        total_weight += weight
        if len(ranking) >= 2:
            key = tuple(ranking)
            merged[key] = merged.get(key, 0) + weight
    matrix = [[0] * n for _ in range(n)]
    for ranking, weight in merged.items():
        idx = [position[candidate] for candidate in ranking]
        for i in range(len(idx) - 1):
            row = matrix[idx[i]]
            for j in range(i + 1, len(idx)):
                row[idx[j]] += weight
    return matrix, total_weight


def _strongly_connected(adjacency):
    """Partition node indices into strongly connected components.

    Each component is ascending and components are ordered by their smallest
    member; isolated nodes form singleton components.  With at most 100 nodes
    a per-source reachability scan is exact and bounded -- no permutations
    and no path enumeration are involved.
    """
    n = len(adjacency)
    reach = []
    for source in range(n):
        seen = [False] * n
        seen[source] = True
        stack = [source]
        while stack:
            u = stack.pop()
            for v in adjacency[u]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(v)
        reach.append(seen)
    assigned = [False] * n
    components = []
    for u in range(n):
        if assigned[u]:
            continue
        group = [v for v in range(n) if reach[u][v] and reach[v][u]]
        for v in group:
            assigned[v] = True
        components.append(group)
    components.sort(key=lambda group: group[0])
    return components


def pairwise_analysis(data):
    """Return the deterministic pairwise preference analysis for valid input.

    The result is a plain JSON-ready object with exactly ``candidates``,
    ``total_weight``, ``comparisons``, ``majority_graph``,
    ``strongly_connected_components`` and ``condorcet_winner``.
    """
    candidates, ballots = validate(data)
    ordered = sorted(candidates)
    n = len(ordered)
    matrix, total_weight = _accumulate(ordered, ballots)

    comparisons = []
    edges = []
    out_degree = [0] * n
    adjacency = [[] for _ in range(n)]
    for a in range(n):
        for b in range(a + 1, n):
            ab = matrix[a][b]
            ba = matrix[b][a]
            if ab > ba:
                winner = ordered[a]
                edges.append((a, b))
            elif ba > ab:
                winner = ordered[b]
                edges.append((b, a))
            else:
                winner = None
            comparisons.append({
                "candidate_a": ordered[a],
                "candidate_b": ordered[b],
                "a_over_b_weight": ab,
                "b_over_a_weight": ba,
                "unexpressed_weight": total_weight - ab - ba,
                "pairwise_winner": winner,
            })
    # Index order equals id order because ``ordered`` is sorted ascending.
    edges.sort()
    for a, b in edges:
        adjacency[a].append(b)
        out_degree[a] += 1

    champions = [i for i in range(n) if out_degree[i] == n - 1]
    condorcet = ordered[champions[0]] if len(champions) == 1 else None

    return {
        "candidates": list(ordered),
        "total_weight": total_weight,
        "comparisons": comparisons,
        "majority_graph": {
            "nodes": list(ordered),
            "edges": [
                {"from": ordered[a], "to": ordered[b]} for a, b in edges
            ],
        },
        "strongly_connected_components": [
            [ordered[i] for i in group]
            for group in _strongly_connected(adjacency)
        ],
        "condorcet_winner": condorcet,
    }
