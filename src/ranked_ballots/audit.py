"""Synthetic paper-ballot audit activity (``ranked-ballots-audit``).

A self-contained, single-operator *working file* that compares one fixed v2
counting snapshot with hand-read physical paper ballots.  It never edits an
electronic ledger, an archive store, export tasks or the publication pointer;
it only reads one immutable v2 report and one physical-container manifest.

Sampling design
---------------
**Without replacement** is the implemented (and fixed) design.  Physical
positions are drawn uniformly from the positions not issued yet, so a position
can never be drawn twice.  The with-replacement design is deliberately *not*
implemented; see ``docs/audit-design.md``.  The two rule sets are never mixed.

Statistical core
----------------
Every pre-declared strict-positive-difference assertion is tested with an
exact-rational e-process.  For a paper ticket whose first preference inside an
assertion's active set is the preferred candidate the score is ``x = +1``; if
it is the opposed candidate it is ``x = -1``; otherwise ``x = 0``.  An unread
(``missing``) ticket scores ``-1`` for *every* assertion and is not an extra
sample.  With ``y = (x + 1) / 2`` and ``S`` the consumed ``y``-sum of that
assertion, the without-replacement conditional mean before draw ``t`` is
``mu = min(1, (N/2 - S) / (N - t + 1))``.  ``E`` starts at ``1`` and is
multiplied by ``1 + (y - mu) / 2``.  ``E >= 1/alpha`` passes permanently; a
consumed ``y``-sum strictly above ``N/2`` passes through the population bound.
All passing comparisons use exact integers (cross multiplication), never
floating point.

The activity file stores only facts (pinned report, manifest, seed, issued
draws and hand-read observations) plus a cached derived view.  Every read
recomputes that view from the facts and refuses to continue when the cached
bytes do not match, so a forged ``passed`` flag or verdict can never
manufacture a certificate.

Only the Python standard library is used.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import tempfile
from fractions import Fraction
from pathlib import Path

from .archive import canonical_bytes
from .common import ident
from .model import analyze

PROTOCOL = "ranked-ballots-audit"
PROTOCOL_VERSION = 1
DESIGN = "without_replacement"
SUPPORTED_DESIGNS = (DESIGN,)

MAX_REPORT_BYTES = 32_000_000
MAX_INPUT_BYTES = 8_000_000
MAX_SESSION_BYTES = 128_000_000
MAX_CANDIDATES = 12
MAX_BALLOTS = 20_000
MAX_WEIGHT = 10**9
MAX_CONTAINERS = 10_000
MAX_CONTAINER_COUNT = 20_000_000_000_000
MAX_POSITIONS = 20_000_000_000_000
MAX_DRAWS = 2_000
MAX_OBSERVATIONS = 2_000
MAX_RISK = 10**6
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX64_ANY = re.compile(r"[0-9a-fA-F]{64}\Z")
RISK_RE = re.compile(r"[1-9][0-9]*/[1-9][0-9]*\Z")

SESSION_KEYS = {
    "protocol",
    "protocol_version",
    "design",
    "report_sha256",
    "report_path",
    "report",
    "manifest_sha256",
    "manifest_path",
    "manifest",
    "seed",
    "risk",
    "max_draws",
    "n",
    "budget",
    "assertions",
    "draws",
    "observations",
    "derived",
}
REPORT_KEYS = {
    "schema_version",
    "revision",
    "candidates",
    "ballots",
    "snapshot_sha256",
    "result",
}


class AuditError(Exception):
    """Expected, explainable audit failure.  The CLI maps this to exit 2."""


# --------------------------------------------------------------------------- #
# Strict input helpers
# --------------------------------------------------------------------------- #

def _reject_float(value):
    raise AuditError("JSON numbers must be integers, not floats: " + value)


def _reject_constant(value):
    raise AuditError("non-finite JSON number: " + value)


def _read_json(path, limit):
    """Read strict JSON with a byte limit, rejecting duplicate keys/floats."""
    target = Path(path)
    try:
        with target.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as error:
        raise AuditError("cannot read JSON input: " + str(error)) from error
    if len(raw) > limit:
        raise AuditError(f"JSON input exceeds {limit // 1_000_000} MB")
    if not raw:
        raise AuditError("empty JSON input")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AuditError("duplicate JSON key: " + key)
            result[key] = value
        return result

    try:
        data = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except AuditError:
        raise
    except (ValueError, UnicodeDecodeError) as error:
        raise AuditError("invalid JSON: " + str(error)) from error
    return data, raw


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise AuditError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _identifier(value, label):
    try:
        return ident(value)
    except (ValueError, TypeError) as error:
        raise AuditError(f"invalid {label}: {error}") from error


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fraction_text(numerator: int, denominator: int) -> str:
    value = Fraction(numerator, denominator)
    return f"{value.numerator}/{value.denominator}"


# --------------------------------------------------------------------------- #
# Atomic activity-file handling
# --------------------------------------------------------------------------- #

def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(parent: Path, payload: bytes) -> str:
    fd, temp = tempfile.mkstemp(prefix=".audit-", suffix=".tmp", dir=str(parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return temp


def _write_atomic(destination: Path, payload: bytes) -> None:
    """Atomically replace an existing activity file (symlink destinations die)."""
    parent = destination.parent
    if destination.is_symlink():
        raise AuditError("activity destination must not be a symlink: " + str(destination))
    temp = _write_temp(parent, payload)
    try:
        os.replace(temp, destination)
        _fsync_dir(parent)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _create_new(destination: Path, payload: bytes) -> None:
    """Create a brand-new activity file with its complete bytes; never clobber.

    A fully written temporary file is atomically hard-linked to the reservation
    name.  ``os.link`` fails if any directory entry already exists, so two
    concurrent starters cannot both win and an existing file is never
    overwritten.  The destination therefore never becomes visible half-written.
    """
    parent = destination.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise AuditError("cannot create activity directory: " + str(error)) from error
    if os.path.lexists(destination):
        raise AuditError("activity file already exists, start refuses to overwrite: "
                         + str(destination))
    temp = _write_temp(parent, payload)
    try:
        try:
            os.link(temp, destination)
        except FileExistsError as error:
            raise AuditError(
                "activity file already exists, start refuses to overwrite: "
                + str(destination)
            ) from error
        except OSError as error:
            raise AuditError("cannot create activity file: " + str(error)) from error
        _fsync_dir(parent)
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# v2 report validation
# --------------------------------------------------------------------------- #

def _validate_report(obj):
    """Validate an immutable v2 counting report and return its pinned facts."""
    if not isinstance(obj, dict) or set(obj) != REPORT_KEYS:
        raise AuditError(
            "report must have exactly schema_version, revision, candidates, "
            "ballots, snapshot_sha256, result"
        )
    if obj["schema_version"] != 2 or type(obj["schema_version"]) is not int:
        raise AuditError("report schema_version must be the integer 2")
    _integer(obj["revision"], 0, 2**63 - 1, "report revision")
    snapshot = obj["snapshot_sha256"]
    if not isinstance(snapshot, str) or not HEX64.fullmatch(snapshot) \
            or snapshot != snapshot.lower():
        raise AuditError("snapshot_sha256 must be 64 lowercase hex characters")

    candidates = obj["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise AuditError(f"candidates must contain 1..{MAX_CANDIDATES} identifiers")
    normalized = []
    for candidate in candidates:
        normalized.append(_identifier(candidate, "candidate id"))
    if len(set(normalized)) != len(normalized) or normalized != sorted(normalized):
        raise AuditError("candidates must be unique and sorted ascending")

    ballot_list = obj["ballots"]
    if not isinstance(ballot_list, list) or len(ballot_list) > MAX_BALLOTS:
        raise AuditError(f"ballots must be an array of at most {MAX_BALLOTS} entries")
    seen = set()
    ballot_ids = []
    for ballot in ballot_list:
        if not isinstance(ballot, dict) or set(ballot) != {"id", "ranking", "weight"}:
            raise AuditError("each ballot must have exactly id, ranking, weight")
        bid = _identifier(ballot["id"], "ballot id")
        ranking = ballot["ranking"]
        if not isinstance(ranking, list) or len(ranking) > MAX_CANDIDATES:
            raise AuditError("ballot ranking must be a bounded array")
        ranks = [_identifier(candidate, "ranking candidate") for candidate in ranking]
        if len(set(ranks)) != len(ranks):
            raise AuditError("ballot ranking contains a duplicate candidate: " + bid)
        if any(candidate not in normalized for candidate in ranks):
            raise AuditError("ballot ranking references an unknown candidate: " + bid)
        _integer(ballot["weight"], 1, MAX_WEIGHT, "ballot weight")
        if bid in seen:
            raise AuditError("duplicate ballot id: " + bid)
        seen.add(bid)
        ballot_ids.append(bid)
    if ballot_ids != sorted(ballot_ids):
        raise AuditError("ballots must be sorted ascending by id")

    ledger = {"candidates": normalized, "ballots": [
        {"id": b["id"], "ranking": list(b["ranking"]), "weight": b["weight"]}
        for b in ballot_list
    ]}
    recomputed = _sha256(canonical_bytes(ledger))
    if recomputed != snapshot:
        raise AuditError(
            "snapshot_sha256 does not match the canonical {candidates, ballots} bytes"
        )
    try:
        result = analyze(ledger)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        raise AuditError("ballots violate the v1 analyze contract: " + str(error)) from error
    try:
        report_result_bytes = canonical_bytes(obj["result"])
    except (TypeError, ValueError) as error:
        raise AuditError("report result is not canonical JSON: " + str(error)) from error
    if report_result_bytes != canonical_bytes(result):
        raise AuditError("report result does not equal analyze(candidates, ballots)")
    if result["winner"] is None:
        raise AuditError("audit requires a non-null report winner")
    total = result["total_weight"]
    if type(total) is not int or total <= 0 or total > MAX_POSITIONS:
        raise AuditError("report total_weight N must be a positive integer")
    return {
        "report": {
            "schema_version": 2,
            "revision": obj["revision"],
            "candidates": normalized,
            "ballots": ledger["ballots"],
            "snapshot_sha256": snapshot,
            "result": result,
        },
        "n": total,
        "winner": result["winner"],
    }


# --------------------------------------------------------------------------- #
# Physical manifest validation
# --------------------------------------------------------------------------- #

def _validate_manifest(obj, n):
    if not isinstance(obj, dict) or set(obj) != {"containers"}:
        raise AuditError("manifest must have exactly the containers field")
    containers = obj["containers"]
    if not isinstance(containers, list) or not 1 <= len(containers) <= MAX_CONTAINERS:
        raise AuditError(f"containers must contain 1..{MAX_CONTAINERS} entries")
    normalized = []
    seen = set()
    total = 0
    for entry in containers:
        if not isinstance(entry, dict) or set(entry) != {"id", "count"}:
            raise AuditError("each container must have exactly id, count")
        cid = _identifier(entry["id"], "container id")
        count = _integer(entry["count"], 1, MAX_CONTAINER_COUNT, "container count")
        if cid in seen:
            raise AuditError("duplicate container id: " + cid)
        seen.add(cid)
        normalized.append({"id": cid, "count": count})
        total += count
        if total > MAX_POSITIONS:
            raise AuditError("manifest position total exceeds the allowed maximum")
    normalized.sort(key=lambda item: item["id"])
    if total != n:
        raise AuditError(f"manifest position total {total} must equal report N {n}")
    return {"containers": normalized}, total


# --------------------------------------------------------------------------- #
# Assertion compilation from the fixed IRV path
# --------------------------------------------------------------------------- #

def _compile_assertions(result):
    """Flatten the IRV path into pre-declared strict-positive-difference tests."""
    rounds = result["rounds"]
    if not rounds:
        raise AuditError("report result has no rounds to audit")
    tallies_by_round = []
    for row in rounds:
        tallies = row.get("tallies")
        if not isinstance(tallies, dict):
            raise AuditError("report round is missing tallies")
        tallies_by_round.append(tallies)

    assertions = []

    def add(round_number, kind, active, preferred, opposed):
        assertions.append({
            "id": f"r{round_number}-{kind}-{opposed}-vs-{preferred}",
            "round": round_number,
            "kind": kind,
            "active": list(active),
            "preferred": preferred,
            "opposed": opposed,
        })

    for index, row in enumerate(rounds):
        if row.get("eliminated") is not None:
            active = sorted(tallies_by_round[index])
            eliminated = row["eliminated"]
            if eliminated not in active:
                raise AuditError("eliminated candidate not present in round tallies")
            for candidate in active:
                if candidate == eliminated:
                    continue
                add(index + 1, "elimination", active, candidate, eliminated)
            continue
        # A terminal majority round: one strict majority assertion for the winner.
        if row.get("outcome") == "majority" and result.get("winner") is not None:
            active = sorted(tallies_by_round[index])
            winner = result["winner"]
            if winner not in active or row["tallies"][winner] <= 0:
                raise AuditError("majority winner is not in the active tally")
            assertions.append({
                "id": f"r{index + 1}-majority-{winner}",
                "round": index + 1,
                "kind": "majority",
                "active": active,
                "preferred": winner,
                "opposed": None,
            })
            break
        # A non-majority, non-elimination round cannot define a path.
        raise AuditError("report round has an unsupported outcome")

    if not assertions:
        raise AuditError("the IRV path yields no auditable assertions")

    for assertion in assertions:
        definition = {
            "round": assertion["round"],
            "active": assertion["active"],
            "preferred": assertion["preferred"],
            "opposed": assertion["opposed"],
            "score": "+1 if active first preference is preferred, "
                     "-1 if it is opposed, 0 otherwise; missing is -1",
            "null": "sum over all N true paper tickets <= 0",
        }
        assertion["definition"] = definition
    return assertions


# --------------------------------------------------------------------------- #
# Deterministic without-replacement sampling
# --------------------------------------------------------------------------- #

TWO_256 = 1 << 256


def _seed_bytes(seed: str) -> bytes:
    return bytes.fromhex(seed)


def _block_value(seed: bytes, counter: int) -> int:
    """The mandated 256-bit unsigned block value for one counter."""
    digest = hashlib.sha256(seed + counter.to_bytes(16, "big")).digest()
    return int.from_bytes(digest, "big")


def _next_block(seed: bytes, counter: int, modulus: int, block=None):
    """Draw one rejection-sampled uniform index in ``0..modulus-1``.

    Block = SHA256(seed[32 bytes] || counter[16-byte big-endian unsigned]).
    Returns ``(index, next_counter)``; rejected blocks also advance the counter,
    so the rejection state is continuous across draw calls.  ``block`` is an
    optional test seam; production always uses the mandated SHA256 stream.
    """
    if block is None:
        block = lambda n: _block_value(seed, n)  # noqa: E731
    while True:
        value = block(counter)
        counter += 1
        limit = TWO_256 - (TWO_256 % modulus)
        if value >= limit:
            continue
        return value % modulus, counter


def _resolve_remaining(drawn_offsets, count, index):
    """Resolve the ``index``-th (0-based) local position not drawn yet."""
    if not 0 <= index < count - len(drawn_offsets):
        raise AuditError("remaining position index out of range")

    def non_drawn_up_to(point):
        return (point + 1) - bisect.bisect_right(drawn_offsets, point)

    low, high = index, min(count - 1, index + len(drawn_offsets))
    while low < high:
        middle = (low + high) // 2
        if non_drawn_up_to(middle) >= index + 1:
            high = middle
        else:
            low = middle + 1
    return low


class _Fenwick:
    """Fenwick tree over per-container remaining position counts."""

    def __init__(self, values):
        self.size = len(values)
        self.tree = [0] * (self.size + 1)
        for position, value in enumerate(values):
            self.add(position, value)

    def add(self, position, delta):
        position += 1
        while position <= self.size:
            self.tree[position] += delta
            position += position & -position

    def prefix(self, position):
        """Sum of the first ``position + 1`` entries (``-1`` means none)."""
        total = 0
        position += 1
        while position > 0:
            total += self.tree[position]
            position -= position & -position
        return total

    def total(self):
        return self.prefix(self.size - 1)

    def select(self, index):
        """Index of the container whose cumulative remaining exceeds ``index``."""
        if index < 0 or index >= self.total():
            return None
        position = 0
        step = 1 << (self.size.bit_length() - 1) if self.size else 0
        while step:
            candidate = position + step
            if candidate <= self.size and self.tree[candidate] <= index:
                position = candidate
                index -= self.tree[candidate]
            step >>= 1
        return position


def _draw_sequence(containers, n, seed, count, block=None):
    """Deterministically derive the first ``count`` without-replacement draws.

    The returned draws are a prefix of one fixed seed stream; batches never
    change earlier positions.  Work is O(containers + draws) times a
    logarithmic factor and never expands per-ballot arrays (N can be 2*10^13).
    """
    if count > n:
        raise AuditError("cannot draw more positions than the population N")
    if count > MAX_DRAWS:
        raise AuditError(f"cannot issue more than {MAX_DRAWS} draws")
    counts = [entry["count"] for entry in containers]
    starts = []
    running = 0
    for entry in containers:
        starts.append(running)
        running += entry["count"]
    drawn_offsets = [[] for _ in containers]
    tree = _Fenwick(counts)
    seed_bytes = _seed_bytes(seed)
    counter = 0
    draws = []
    for index in range(1, count + 1):
        modulus = n - index + 1
        k, counter = _next_block(seed_bytes, counter, modulus, block)
        container_index = tree.select(k)
        if container_index is None:  # pragma: no cover - defensive
            raise AuditError("remaining position index out of range")
        before = tree.prefix(container_index - 1) if container_index else 0
        local = _resolve_remaining(
            drawn_offsets[container_index], counts[container_index], k - before
        )
        draws.append({
            "index": index,
            "global_position": starts[container_index] + local,
            "container": containers[container_index]["id"],
            "position_in_container": local + 1,
        })
        tree.add(container_index, -1)
        bisect.insort(drawn_offsets[container_index], local)
    return draws, counter


# --------------------------------------------------------------------------- #
# Assertions, e-processes and the derived view
# --------------------------------------------------------------------------- #

def _active_preference(ranking, active):
    """Return the first candidate of ``active`` in ranking, or None."""
    active_set = set(active)
    for candidate in ranking:
        if candidate in active_set:
            return candidate
    return None


def _score(assertion, ranking):
    """Map one paper ticket to ``Y = 2y`` for this assertion.

    ``x = +1`` when the ticket's first active preference is the preferred
    candidate, ``x = -1`` when it is the opposed candidate (or, for the final
    majority assertion, any other active candidate), and ``x = 0`` otherwise
    including tickets with no active preference.
    """
    first = _active_preference(ranking, assertion["active"])
    if first is None:
        return 1  # x = 0
    if first == assertion["preferred"]:
        return 2  # x = +1
    if assertion["opposed"] is None or first == assertion["opposed"]:
        return 0  # x = -1
    return 1  # x = 0: another active candidate for an elimination assertion


def _observation_score(assertion, observation):
    if observation.get("missing"):
        return 0  # x = -1 -> y = 0
    return _score(assertion, observation["ranking"])


class _AssertionState:
    __slots__ = ("assertion", "e_num", "e_den", "y_sum", "passed",
                 "first_pass_draw", "passing_rule")

    def __init__(self, assertion):
        self.assertion = assertion
        self.e_num = 1
        self.e_den = 1
        self.y_sum = 0  # 2 * S, integer
        self.passed = False
        self.first_pass_draw = None
        self.passing_rule = None

    def export(self):
        return {
            "state": "passed" if self.passed else "pending",
            "passed": self.passed,
            "e": _fraction_text(self.e_num, self.e_den),
            "y_sum": _fraction_text(self.y_sum, 2),
            "first_pass_draw": self.first_pass_draw,
            "passing_rule": self.passing_rule,
        }


def _advance_assertion(state, n, draw_number, y_value, threshold_p, threshold_q):
    """Consume one observation for an assertion; populate the pass state."""
    if state.passed:
        return
    remaining = n - draw_number + 1
    if state.y_sum > n:  # population bound already reached (defensive)
        state.passed = True
        state.first_pass_draw = draw_number - 1
        state.passing_rule = "population_bound"
        return
    # 2 * (N/2 - S); the y_sum > n guard above already handled S > N/2, so a
    # negative raw mu can never reach this multiplication (which stays positive).
    raw_num = n - state.y_sum
    raw_den = 2 * remaining
    if raw_num >= raw_den:  # a raw mu above 1 is clamped to exactly 1
        mu_num, mu_den = 1, 1
    else:
        mu_num, mu_den = raw_num, raw_den
    factor_num = (4 + y_value) * mu_den - 2 * mu_num
    factor_den = 4 * mu_den
    state.e_num *= factor_num
    state.e_den *= factor_den
    state.y_sum += y_value
    if state.y_sum > n:
        state.passed = True
        state.first_pass_draw = draw_number
        state.passing_rule = "population_bound"
    elif state.e_num * threshold_p >= state.e_den * threshold_q:
        state.passed = True
        state.first_pass_draw = draw_number
        state.passing_rule = "threshold"


def _derive(session):
    """Recompute the whole derived view from the stored facts."""
    assertions = session["assertions"]
    draws = session["draws"]
    observations = {entry["position"]: entry for entry in session["observations"]}
    n = session["n"]
    budget = session["budget"]
    p = session["risk"]["p"]
    q = session["risk"]["q"]

    states = [_AssertionState(assertion) for assertion in assertions]
    consumed = 0
    stop_draw = None
    for draw in draws:
        observation = observations.get(draw["global_position"])
        if observation is None:
            break  # unexplained early draw: only the contiguous prefix counts
        consumed = draw["index"]
        for state in states:
            if state.passed:
                continue
            _advance_assertion(
                state, n, draw["index"],
                _observation_score(state.assertion, observation), p, q,
            )
        if all(state.passed for state in states):
            stop_draw = draw["index"]
            break

    if stop_draw is not None:
        verdict = "certified"
        reason = "all_assertions_passed"
    elif consumed >= budget:
        verdict = "not_certified"
        if budget == n and consumed == n:
            reason = "population_exhausted_without_all_assertions"
        else:
            reason = "effective_budget_exhausted_without_all_assertions"
    elif len(draws) > consumed:
        verdict = "continue"
        reason = "awaiting_explanation_of_earlier_draws"
    else:
        verdict = "continue"
        reason = "can_continue"

    explained_positions = sum(
        1 for draw in draws if draw["global_position"] in observations
    )
    derived = {
        "consumed": consumed,
        "verdict": verdict,
        "stop_draw": stop_draw,
        "reason": reason,
        "distinct_explained_positions": explained_positions,
        "assertions": {
            assertion["id"]: state.export()
            for assertion, state in zip(assertions, states)
        },
    }
    return derived


# --------------------------------------------------------------------------- #
# Activity file construction and re-verification
# --------------------------------------------------------------------------- #

def _session_bytes(session) -> bytes:
    return canonical_bytes(session)


def _build_session(report_path, manifest_path, report_obj, report_raw_sha,
                   manifest, manifest_sha, seed, p, q, max_draws, n,
                   assertions):
    session = {
        "protocol": PROTOCOL,
        "protocol_version": PROTOCOL_VERSION,
        "design": DESIGN,
        "report_sha256": report_raw_sha,
        "report_path": str(report_path),
        "report": report_obj,
        "manifest_sha256": manifest_sha,
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "seed": seed,
        "risk": {"p": p, "q": q},
        "max_draws": max_draws,
        "n": n,
        "budget": min(max_draws, n),
        "assertions": assertions,
        "draws": [],
        "observations": [],
    }
    session["derived"] = _derive(session)
    return session


def _status_draws(session):
    explained = {entry["position"] for entry in session["observations"]}
    consumed = session["derived"]["consumed"]
    return [
        {
            "draw_index": draw["index"],
            "position": draw["global_position"],
            "container": draw["container"],
            "position_in_container": draw["position_in_container"],
            "explained": draw["global_position"] in explained,
            "consumed": draw["index"] <= consumed,
        }
        for draw in session["draws"]
    ]


def status_payload(session):
    """Render the public status object from one verified session."""
    report = session["report"]
    derived = session["derived"]
    assertions = []
    for assertion in session["assertions"]:
        state = derived["assertions"][assertion["id"]]
        entry = {
            "id": assertion["id"],
            "round": assertion["round"],
            "kind": assertion["kind"],
            "active": assertion["active"],
            "preferred": assertion["preferred"],
            "opposed": assertion["opposed"],
            "definition": assertion["definition"],
        }
        entry.update(state)
        assertions.append(entry)
    p = session["risk"]["p"]
    q = session["risk"]["q"]
    return {
        "protocol": session["protocol"],
        "protocol_version": session["protocol_version"],
        "design": session["design"],
        "revision": report["revision"],
        "snapshot_sha256": report["snapshot_sha256"],
        "report_sha256": session["report_sha256"],
        "report_path": session["report_path"],
        "manifest_sha256": session["manifest_sha256"],
        "manifest_path": session["manifest_path"],
        "candidates": report["candidates"],
        "seed": session["seed"],
        "alpha": {"numerator": p, "denominator": q, "text": f"{p}/{q}"},
        "n": session["n"],
        "max_draws": session["max_draws"],
        "effective_budget": session["budget"],
        "draw_count": len(session["draws"]),
        "distinct_explained_positions": derived["distinct_explained_positions"],
        "consumed": derived["consumed"],
        "verdict": derived["verdict"],
        "stop_draw": derived["stop_draw"],
        "reason": derived["reason"],
        "draws": _status_draws(session),
        "assertions": assertions,
        "observations": [
            {
                "position": entry["position"],
                "container": entry["container"],
                "position_in_container": entry["position_in_container"],
                "draw_indices": entry["draw_indices"],
                **({"missing": True} if entry.get("missing")
                   else {"ranking": entry["ranking"]}),
            }
            for entry in session["observations"]
        ],
    }


def _validate_seed(seed):
    if not isinstance(seed, str) or not HEX64_ANY.fullmatch(seed):
        raise AuditError("seed must be exactly 64 hexadecimal characters")
    return seed.lower()


def _parse_risk(risk):
    if not isinstance(risk, str) or not RISK_RE.fullmatch(risk):
        raise AuditError("risk must be an integer fraction P/Q with 0<P<Q")
    numerator_text, denominator_text = risk.split("/")
    p = int(numerator_text)
    q = int(denominator_text)
    if not (1 <= p <= MAX_RISK and 1 <= q <= MAX_RISK) or not p < q:
        raise AuditError(f"risk fraction parts must be in 1..{MAX_RISK} with P<Q")
    divisor = _gcd(p, q)
    return p // divisor, q // divisor


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def _validate_observation_input(item):
    if not isinstance(item, dict):
        raise AuditError("each observation must be an object")
    if set(item) == {"draw_index", "ranking"}:
        draw_index = _integer(item["draw_index"], 1, MAX_DRAWS, "draw_index")
        ranking = item["ranking"]
        if not isinstance(ranking, list):
            raise AuditError("observation ranking must be an array")
        return draw_index, {"ranking": list(ranking)}
    if set(item) == {"draw_index", "missing"}:
        draw_index = _integer(item["draw_index"], 1, MAX_DRAWS, "draw_index")
        if item["missing"] is not True:
            raise AuditError("missing must be exactly true")
        return draw_index, {"missing": True}
    raise AuditError(
        "each observation must be exactly {draw_index, ranking} or "
        "{draw_index, missing:true}"
    )


def _normalize_observation_ranking(ranking, candidates, label):
    ranks = [_identifier(candidate, "ranking candidate") for candidate in ranking]
    if len(set(ranks)) != len(ranks):
        raise AuditError("observation ranking contains a duplicate candidate: " + label)
    if any(candidate not in candidates for candidate in ranks):
        raise AuditError("observation ranking references an unknown candidate: " + label)
    return ranks


def _merge_observations(session, batch, draws_by_position):
    """Merge validated observations atomically and return the new list."""
    draws = session["draws"]
    existing = {entry["position"]: entry for entry in session["observations"]}
    pending = {}
    for draw_index, fact in batch:
        draw = draws[draw_index - 1]
        position = draw["global_position"]
        key = ("missing",) if fact.get("missing") else ("ranking", tuple(fact["ranking"]))
        if position in pending and pending[position][0] != key:
            raise AuditError(
                "conflicting explanations for physical position " + str(position)
            )
        if position in existing:
            stored = existing[position]
            stored_key = ("missing",) if stored.get("missing") else (
                "ranking", tuple(stored["ranking"])
            )
            if stored_key != key:
                raise AuditError(
                    "conflicting explanations for physical position " + str(position)
                )
        if position in pending:
            pending[position][1].add(draw_index)
        else:
            pending[position] = (key, {draw_index}, fact)

    updated = []
    for entry in session["observations"]:
        position = entry["position"]
        if position in pending:
            key, indices, fact = pending.pop(position)
            combined = sorted(set(entry["draw_indices"]) | indices)
            entry = dict(entry)
            entry["draw_indices"] = combined
        updated.append(entry)
    for position in sorted(pending):
        key, indices, fact = pending[position]
        draw = draws_by_position[position]
        entry = {
            "position": position,
            "container": draw["container"],
            "position_in_container": draw["position_in_container"],
            "draw_indices": sorted(indices),
        }
        entry.update(fact)
        updated.append(entry)
    updated.sort(key=lambda entry: entry["position"])
    return updated


def _validate_stored_observation(entry, draws_by_position, candidates):
    keys = set(entry)
    if keys == {"position", "container", "position_in_container",
                "draw_indices", "ranking"}:
        ranking = _normalize_observation_ranking(
            entry["ranking"], candidates, str(entry["position"])
        )
        fact = {"ranking": ranking}
    elif keys == {"position", "container", "position_in_container",
                  "draw_indices", "missing"}:
        if entry["missing"] is not True:
            raise AuditError("stored missing flag must be true")
        fact = {"missing": True}
    else:
        raise AuditError("stored observation has an unsupported shape")
    position = _integer(entry["position"], 0, MAX_POSITIONS - 1, "observation position")
    draw = draws_by_position.get(position)
    if draw is None:
        raise AuditError("stored observation refers to a position never drawn")
    if entry["container"] != draw["container"] \
            or entry["position_in_container"] != draw["position_in_container"]:
        raise AuditError("stored observation disagrees with the draw position")
    indices = entry["draw_indices"]
    if not isinstance(indices, list) or len(indices) != 1:
        raise AuditError(
            "without-replacement observations cite exactly one draw index"
        )
    index = _integer(indices[0], 1, MAX_DRAWS, "stored draw index")
    if draw["index"] != index:
        raise AuditError("stored observation cites a draw for another position")
    return position, fact


# --------------------------------------------------------------------------- #
# Loading and verifying an existing activity file
# --------------------------------------------------------------------------- #

def _load_session(path):
    target = Path(path)
    if target.is_symlink():
        raise AuditError("activity file must not be a symlink: " + str(target))
    obj, raw = _read_json(path, MAX_SESSION_BYTES)
    if not isinstance(obj, dict) or set(obj) != SESSION_KEYS:
        raise AuditError("activity file does not have the expected fields")
    if obj["protocol"] != PROTOCOL or obj["protocol_version"] != PROTOCOL_VERSION:
        raise AuditError("unsupported activity protocol/version")
    if obj["design"] not in SUPPORTED_DESIGNS:
        raise AuditError("unsupported sampling design: " + repr(obj["design"]))
    if obj["seed"] != _validate_seed(obj["seed"]):
        raise AuditError("stored seed must be canonical lowercase hex")
    risk = obj["risk"]
    if not isinstance(risk, dict) or set(risk) != {"p", "q"}:
        raise AuditError("stored risk must have exactly p, q")
    p, q = risk["p"], risk["q"]
    if type(p) is not int or type(q) is not int or not 1 <= p < q <= MAX_RISK:
        raise AuditError("stored risk fraction is invalid")
    if _gcd(p, q) != 1:
        raise AuditError("stored risk fraction must be reduced")
    max_draws = _integer(obj["max_draws"], 1, MAX_DRAWS, "max_draws")
    report_obj = obj["report"]
    if not isinstance(report_obj, dict):
        raise AuditError("stored report is not an object")
    verified = _validate_report(report_obj)
    manifest_obj, manifest_total = _validate_manifest(obj["manifest"], verified["n"])
    _integer(obj["n"], 1, MAX_POSITIONS, "stored N")
    if obj["n"] != verified["n"]:
        raise AuditError("stored N disagrees with the pinned report")
    _integer(obj["budget"], 1, MAX_DRAWS, "stored budget")
    if obj["budget"] != min(max_draws, verified["n"]):
        raise AuditError("stored effective budget is inconsistent")
    for key in ("report_path", "manifest_path"):
        if not isinstance(obj[key], str):
            raise AuditError("stored " + key + " must be a string")
    if not isinstance(obj["report_sha256"], str) \
            or not HEX64.fullmatch(obj["report_sha256"]):
        raise AuditError("stored report_sha256 must be 64 lowercase hex characters")
    if obj["manifest_sha256"] != _sha256(canonical_bytes(manifest_obj)):
        raise AuditError("stored manifest_sha256 does not match the pinned manifest bytes")
    expected_assertions = _compile_assertions(verified["report"]["result"])
    if obj["assertions"] != expected_assertions:
        raise AuditError("stored assertions do not match the pinned counting path")

    draws = obj["draws"]
    if not isinstance(draws, list) or len(draws) > MAX_DRAWS:
        raise AuditError("stored draws must be a bounded array")
    if len(draws) > obj["budget"]:
        raise AuditError("stored draws exceed the effective budget")
    for expected_index, draw in enumerate(draws, start=1):
        if not isinstance(draw, dict) or set(draw) != {
            "index", "global_position", "container", "position_in_container"
        }:
            raise AuditError("stored draw has an unsupported shape")
        if draw["index"] != expected_index:
            raise AuditError("stored draw indices must be consecutive from 1")
        _integer(draw["global_position"], 0, verified["n"] - 1, "draw global position")
        _integer(draw["position_in_container"], 1, MAX_CONTAINER_COUNT,
                 "draw position in container")
    issued, _counter = _draw_sequence(
        manifest_obj["containers"], verified["n"], obj["seed"], len(draws)
    )
    if issued != draws:
        raise AuditError("stored draws do not match the fixed seed stream")

    draws_by_position = {}
    seen_positions = set()
    for draw in draws:
        if draw["global_position"] in seen_positions:
            raise AuditError("without-replacement draws must not repeat a position")
        seen_positions.add(draw["global_position"])
        draws_by_position[draw["global_position"]] = draw

    observations = obj["observations"]
    if not isinstance(observations, list) or len(observations) > MAX_OBSERVATIONS:
        raise AuditError("stored observations must be a bounded array")
    candidates = set(verified["report"]["candidates"])
    normalized_observations = []
    for entry in observations:
        position, fact = _validate_stored_observation(entry, draws_by_position, candidates)
        normalized = {
            "position": position,
            "container": entry["container"],
            "position_in_container": entry["position_in_container"],
            "draw_indices": list(entry["draw_indices"]),
        }
        normalized.update(fact)
        normalized_observations.append(normalized)
    normalized_observations.sort(key=lambda entry: entry["position"])
    if normalized_observations != observations:
        raise AuditError("stored observations are not in canonical order")

    session = dict(obj)
    session["report"] = verified["report"]
    session["manifest"] = manifest_obj
    session["risk"] = {"p": p, "q": q}
    session["observations"] = normalized_observations
    session["derived"] = _derive(session)
    if obj["derived"] != session["derived"]:
        raise AuditError(
            "cached derived view does not match recomputation from stored facts"
        )
    return session


def _save_session(path, session):
    _write_atomic(Path(path), _session_bytes(session))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def start(report_path, manifest_path, seed, risk, max_draws, session_path):
    """Create a new audit activity.  Refuses to overwrite any existing file."""
    try:
        report_obj, report_raw = _read_json(report_path, MAX_REPORT_BYTES)
        verified = _validate_report(report_obj)
        # Provenance: the SHA256 of the report input bytes as supplied.  The
        # pinned content is independently re-verified on every read through
        # snapshot_sha256, result and the derived counting path.
        report_sha = _sha256(report_raw)
        manifest_obj, _manifest_raw = _read_json(manifest_path, MAX_INPUT_BYTES)
        manifest, total = _validate_manifest(manifest_obj, verified["n"])
        manifest_sha = _sha256(canonical_bytes(manifest))
        seed = _validate_seed(seed)
        p, q = _parse_risk(risk)
        max_draws = _integer(max_draws, 1, MAX_DRAWS, "max_draws")
        assertions = _compile_assertions(verified["report"]["result"])
        session = _build_session(
            report_path, manifest_path, verified["report"], report_sha,
            manifest, manifest_sha, seed, p, q, max_draws, verified["n"],
            assertions,
        )
        _create_new(Path(session_path), _session_bytes(session))
        return _status(session)
    except AuditError:
        raise
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as error:
        raise AuditError("start failed: " + str(error)) from error


def draw(session_path, count):
    """Register ``count`` more draws from the fixed seed stream."""
    try:
        session = _load_session(session_path)
        count = _integer(count, 1, MAX_DRAWS, "count")
        verdict = session["derived"]["verdict"]
        if verdict in ("certified", "not_certified"):
            raise AuditError("activity is terminal (" + verdict + "); draw is refused")
        budget = session["budget"]
        remaining = budget - len(session["draws"])
        if count > remaining:
            raise AuditError(
                f"draw of {count} exceeds the remaining effective budget {remaining}"
            )
        new_draws, _counter = _draw_sequence(
            session["manifest"]["containers"], session["n"],
            session["seed"], len(session["draws"]) + count,
        )
        session["draws"] = new_draws
        session["derived"] = _derive(session)
        _save_session(session_path, session)
        return _status(session)
    except AuditError:
        raise
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as error:
        raise AuditError("draw failed: " + str(error)) from error


def record(session_path, source_path):
    """Atomically accept hand-read observations for already-issued draws."""
    try:
        session = _load_session(session_path)
        obj, _raw = _read_json(source_path, MAX_INPUT_BYTES)
        if not isinstance(obj, dict) or set(obj) != {"observations"}:
            raise AuditError("record source must have exactly the observations field")
        items = obj["observations"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_OBSERVATIONS:
            raise AuditError(
                f"observations must contain 1..{MAX_OBSERVATIONS} items"
            )
        verdict = session["derived"]["verdict"]
        if verdict in ("certified", "not_certified"):
            terminal = verdict
        else:
            terminal = None
        issued = len(session["draws"])
        candidates = set(session["report"]["candidates"])
        parsed = []
        seen_indices = set()
        for item in items:
            draw_index, fact = _validate_observation_input(item)
            if draw_index > issued:
                raise AuditError(f"draw_index {draw_index} has not been issued")
            if draw_index in seen_indices:
                raise AuditError("draw_index appears twice in one record request")
            seen_indices.add(draw_index)
            if "ranking" in fact:
                fact = {"ranking": _normalize_observation_ranking(
                    fact["ranking"], candidates, "draw " + str(draw_index)
                )}
            parsed.append((draw_index, fact))
        if terminal is not None:
            _require_known_facts_only(session, parsed)
            return _status(session)
        draws_by_position = {
            draw["global_position"]: draw for draw in session["draws"]
        }
        merged = _merge_observations(session, parsed, draws_by_position)
        if merged == session["observations"]:
            return _status(session)  # identical retry: leave every byte untouched
        session["observations"] = merged
        session["derived"] = _derive(session)
        _save_session(session_path, session)
        return _status(session)
    except AuditError:
        raise
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as error:
        raise AuditError("record failed: " + str(error)) from error


def _require_known_facts_only(session, parsed):
    """Reject any new/changed fact once the activity is terminal."""
    existing = {entry["position"]: entry for entry in session["observations"]}
    draws = session["draws"]
    for draw_index, fact in parsed:
        position = draws[draw_index - 1]["global_position"]
        stored = existing.get(position)
        key = ("missing",) if fact.get("missing") else ("ranking", tuple(fact["ranking"]))
        if stored is None:
            raise AuditError(
                "activity is terminal; only identical retries of known facts "
                "are accepted (draw " + str(draw_index) + " was never explained)"
            )
        stored_key = ("missing",) if stored.get("missing") else (
            "ranking", tuple(stored["ranking"])
        )
        if stored_key != key:
            raise AuditError(
                "activity is terminal; only identical retries of known facts "
                "are accepted"
            )


def status(session_path):
    """Read-only: reproduce the same status without mutating the activity."""
    try:
        session = _load_session(session_path)
        return _status(session)
    except AuditError:
        raise
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as error:
        raise AuditError("status failed: " + str(error)) from error


def _status(session):
    return status_payload(session)
