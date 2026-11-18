"""Candidate-withdrawal sensitivity analysis (``ranked-ballots-sensitivity``).

A read-only, single-shot report over the strict ``{candidates, ballots}``
contract used by the v1 IRV count.  It never changes the ballot ledger, the
archive, the paper-ballot audit, scan/group assignments or any earlier output.

Semantics
---------
The *baseline* is exactly the existing :func:`ranked_ballots.model.analyze`
result for the unmodified validated input.  For every candidate ``c``
(ascending by ID) one counterfactual scenario is built independently from the
same original validated input: ``c`` is removed from the candidate list and
from every ballot ranking, while every ballot id, every weight and every
(possibly now empty) ranking entry is kept.  Empty rankings remain ballot
groups: they are never dropped and their weight is never redistributed.  Each
non-empty scenario is counted by the same engine ``analyze`` uses, so
majority, elimination, tie-break and exhaustion rules cannot drift from the
v1 count.

The one-candidate input has a defined withdrawal edge case: its only scenario
has an empty candidate list, which the ordinary count cannot represent.  It
reports ``winner=null``, no rounds and ``total_weight=0``.  Every other
scenario goes through the existing counting semantics unchanged.

Input identity
--------------
``source_sha256`` identifies the original validated business input: the SHA256
of its canonical serialisation (candidates ascending by ID, ballots ascending
by id, each ranking in its original order, canonical JSON with one final
newline), following the archive's ``snapshot_sha256`` precedent.  Because a
``candidates``/``ballots`` array reordering is the same valid input in a
different presentation, the whole report -- including this field -- is
byte-for-byte identical after such a reordering.  Changing a ranking's order
does change the digest and, where the counting semantics are affected, the
counted result.

All arithmetic is exact integer arithmetic.  Ballot groups are never expanded
into individual ballots.  Output contains no timestamps, randomness or
machine-dependent fields.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .calculation import count
from .common import ident, read
from .model import analyze
from .pairwise import validate

FORMAT = 1

_CANONICAL_OPTIONS = dict(
    ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
)


def _canonical_bytes(obj) -> bytes:
    """Canonical JSON: UTF-8, sorted keys, compact separators, one final LF."""
    return (json.dumps(obj, **_CANONICAL_OPTIONS) + "\n").encode("utf-8")


def _prepare(data):
    """Validate via the existing v1 validator and normalise input order.

    Validation is delegated to :func:`ranked_ballots.pairwise.validate`, whose
    rules mirror ``analyze`` exactly (identifiers, unknown fields, duplicate
    keys already rejected while reading, integer weights, bounds).  Only the
    *representation* is normalised: candidates ascending by ID and ballots
    ascending by ID; ranking order is preserved because it carries counting
    semantics.
    """
    candidates, validated = validate(data)
    entries = data["ballots"]
    ballots = [
        {"id": ident(entry["id"]), "ranking": list(ranking), "weight": weight}
        for entry, (ranking, weight) in zip(entries, validated)
    ]
    ballots.sort(key=lambda ballot: ballot["id"])
    return sorted(candidates), ballots


def _canonical_digest(candidates, ballots) -> str:
    """SHA256 of the canonical normalised business input (API fallback)."""
    return hashlib.sha256(
        _canonical_bytes({"candidates": candidates, "ballots": ballots})
    ).hexdigest()


def _scenario_input(candidates, ballots, withdrawn):
    """Remove ``withdrawn`` from the candidate list and every ranking.

    The result is a fresh, complete ``{candidates, ballots}`` business input.
    Ballot ids and weights are untouched and empty rankings are retained.
    """
    return {
        "candidates": [candidate for candidate in candidates if candidate != withdrawn],
        "ballots": [
            {
                "id": ballot["id"],
                "ranking": [x for x in ballot["ranking"] if x != withdrawn],
                "weight": ballot["weight"],
            }
            for ballot in ballots
        ],
    }


def _empty_ledger_result():
    """Result for the empty-candidate withdrawal of a one-candidate election."""
    return {"winner": None, "rounds": [], "total_weight": 0}


def _last_round_weight(result, field):
    rounds = result["rounds"]
    return rounds[-1][field] if rounds else 0


def _baseline(data):
    """The unmodified validated input goes through existing ``analyze``."""
    return analyze(data)


def _scenario(candidates, ballots, withdrawn, baseline):
    """Build, count and self-check one withdrawal scenario."""
    scenario_input = _scenario_input(candidates, ballots, withdrawn)
    if scenario_input["candidates"]:
        # ``analyze`` is exactly strict validation followed by ``count`` on the
        # validated ``(ranking, weight)`` tuples.  This input was derived from
        # the single validated original by removing one candidate, so it is
        # valid by construction; feeding the same ``count`` engine yields
        # exactly what ``analyze(scenario_input)`` would (the test suite
        # asserts this equality) without re-validating every identifier once
        # per scenario.
        result = count(
            scenario_input["candidates"],
            [(ballot["ranking"], ballot["weight"])
             for ballot in scenario_input["ballots"]],
        )
    else:
        result = _empty_ledger_result()
    base_continuing = _last_round_weight(baseline, "continuing_weight")
    base_exhausted = _last_round_weight(baseline, "exhausted_weight")
    scenario = {
        "withdrawn_candidate": withdrawn,
        "input": scenario_input,
        "result": result,
        "changes": {
            "winner_changed": baseline["winner"] != result["winner"],
            "winner_before": baseline["winner"],
            "winner_after": result["winner"],
            "continuing_weight_delta":
                _last_round_weight(result, "continuing_weight") - base_continuing,
            "exhausted_weight_delta":
                _last_round_weight(result, "exhausted_weight") - base_exhausted,
        },
    }
    _verify_scenario(scenario, withdrawn, candidates, ballots, baseline)
    return scenario


def _verify_scenario(scenario, withdrawn, candidates, ballots, baseline):
    """Recheck one scenario's structure and cross-field invariants."""
    if set(scenario) != {"withdrawn_candidate", "input", "result", "changes"}:
        raise ValueError("scenario has unexpected fields")
    if scenario["withdrawn_candidate"] != withdrawn:
        raise ValueError("scenario is not for the expected candidate")
    if scenario["input"]["candidates"] != [
        candidate for candidate in candidates if candidate != withdrawn
    ]:
        raise ValueError("scenario input candidate list is not the single removal")
    scenario_ballots = scenario["input"]["ballots"]
    if len(scenario_ballots) != len(ballots):
        raise ValueError("scenario input dropped ballot groups")
    for original, changed in zip(ballots, scenario_ballots):
        if changed["id"] != original["id"] \
                or changed["weight"] != original["weight"] \
                or changed["ranking"] != [
                    x for x in original["ranking"] if x != withdrawn
                ]:
            raise ValueError("scenario input is not the mandated single removal")
    result = scenario["result"]
    changes = scenario["changes"]
    if set(changes) != {
        "winner_changed", "winner_before", "winner_after",
        "continuing_weight_delta", "exhausted_weight_delta",
    }:
        raise ValueError("scenario changes have unexpected fields")
    if changes["winner_changed"] != (
        changes["winner_before"] != changes["winner_after"]
    ):
        raise ValueError("winner_changed disagrees with winner fields")
    if changes["winner_before"] != baseline["winner"]:
        raise ValueError("winner_before disagrees with the baseline")
    if changes["winner_after"] != result["winner"]:
        raise ValueError("winner_after disagrees with the scenario result")
    if changes["continuing_weight_delta"] != (
        _last_round_weight(result, "continuing_weight")
        - _last_round_weight(baseline, "continuing_weight")
    ):
        raise ValueError("continuing weight delta is inconsistent")
    if changes["exhausted_weight_delta"] != (
        _last_round_weight(result, "exhausted_weight")
        - _last_round_weight(baseline, "exhausted_weight")
    ):
        raise ValueError("exhausted weight delta is inconsistent")


def _iter_scenarios(candidates, ballots, baseline):
    """Yield one verified scenario per candidate in ascending ID order."""
    for withdrawn in candidates:
        yield _scenario(candidates, ballots, withdrawn, baseline)


def _report_chunks(candidates, ballots, digest, baseline, scenarios):
    """Yield the canonical report bytes in bounded chunks.

    The four top-level keys are fixed and sort as ``baseline``, ``format``,
    ``scenarios``, ``source_sha256``.  Serialising each value separately with
    the same canonical options and joining with the fixed punctuation
    produces exactly the bytes of one ``json.dumps(report)`` call, while peak
    memory stays at one scenario rather than the whole report.
    """
    yield b'{"baseline":'
    yield json.dumps(baseline, **_CANONICAL_OPTIONS).encode("utf-8")
    yield b',"format":'
    yield json.dumps(FORMAT, **_CANONICAL_OPTIONS).encode("utf-8")
    yield b',"scenarios":['
    expected = iter(candidates)
    count_seen = 0
    for scenario in scenarios:
        withdrawn = next(expected, None)
        if withdrawn is None:
            raise ValueError("sensitivity report has too many scenarios")
        if scenario["withdrawn_candidate"] != withdrawn:
            raise ValueError("sensitivity scenarios are not in candidate ID order")
        if count_seen:
            yield b","
        count_seen += 1
        yield json.dumps(scenario, **_CANONICAL_OPTIONS).encode("utf-8")
    if count_seen != len(candidates):
        raise ValueError("sensitivity report must contain one scenario per candidate")
    yield b'],"source_sha256":'
    yield json.dumps(digest, **_CANONICAL_OPTIONS).encode("utf-8")
    yield b"}\n"


def _save_canonical(path, chunks):
    """Stage, verify and atomically install canonical report bytes.

    ``chunks`` is an iterable of bytes.  Each chunk is written and hashed as it
    is produced, so the full report need not be materialised.  The staged file
    is fsynced, read back and its SHA256 and length checked before the single
    ``os.replace``.  Any failure removes the temporary file and leaves
    existing output bytes untouched.
    """
    dest = Path(path)
    if dest.is_symlink():
        raise ValueError("output cannot be a symlink")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".sensitivity-", suffix=".tmp", dir=str(dest.parent)
    )
    try:
        expected = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as stream:
            for chunk in chunks:
                expected.update(chunk)
                size += len(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        staged = hashlib.sha256()
        staged_size = 0
        with open(temp_name, "rb") as stream:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                staged.update(chunk)
                staged_size += len(chunk)
        if staged_size != size or staged.digest() != expected.digest():
            raise OSError("staged sensitivity report failed verification")
        os.replace(temp_name, dest)
        temp_name = None
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)


def _build_report(data):
    """Return ``(report, candidates, ballots)`` for parsed input.

    Any contract violation raises ``ValueError``/``TypeError``; no partial
    report is ever returned.
    """
    candidates, ballots = _prepare(data)
    source_sha256 = _canonical_digest(candidates, ballots)
    baseline = _baseline(data)
    scenarios = list(_iter_scenarios(candidates, ballots, baseline))
    return {
        "format": FORMAT,
        "source_sha256": source_sha256,
        "baseline": baseline,
        "scenarios": scenarios,
    }, candidates, ballots


def sensitivity_analysis(data, output=None):
    """Return the complete candidate-withdrawal report.

    Two call shapes are supported:

    * ``sensitivity_analysis(data)`` for an already parsed
      ``{candidates, ballots}`` object: returns the report, performs no I/O.
    * ``sensitivity_analysis(source_path, output_path)`` -- the path-based
      entry point matching the existing CLI/API convention; it reads the
      source, publishes the canonical report and returns it.
    """
    if isinstance(data, dict):
        report, _candidates, _ballots = _build_report(data)
        return report
    if not isinstance(output, (str, Path)):
        raise TypeError("source and output paths are required")
    return sensitivity_report(data, output)


def _check_destination(output):
    output_path = Path(output)
    if output_path.is_symlink():
        raise ValueError("output cannot be a symlink")
    return output_path


def _publish_streaming(data, output_path):
    """Write a report chunk-by-chunk and atomically install it."""
    candidates, ballots = _prepare(data)
    digest = _canonical_digest(candidates, ballots)
    baseline = _baseline(data)
    _save_canonical(
        output_path,
        _report_chunks(
            candidates, ballots, digest, baseline,
            _iter_scenarios(candidates, ballots, baseline),
        ),
    )


def sensitivity_report(source, output, return_report=True):
    """Path API: read ``source`` and atomically publish the report.

    ``return_report`` (default true) returns the complete report object.  Pass
    false to stream publication without retaining every scenario, which keeps
    memory bounded for maximum-size inputs.
    """
    source_path = Path(source)
    output_path = Path(output)
    if source_path.resolve() == output_path.resolve():
        raise ValueError("input and output must differ")
    data, _raw_digest = read(source_path)
    _check_destination(output_path)
    if return_report:
        report, _candidates, _ballots = _build_report(data)
        _save_canonical(output_path, _report_chunks(
            _candidates, _ballots, report["source_sha256"],
            report["baseline"], iter(report["scenarios"]),
        ))
        return report
    _publish_streaming(data, output_path)
    return None


# Convenience alias: ``ranked_ballots.sensitivity.sensitivity(path, output)``.
sensitivity = sensitivity_report
