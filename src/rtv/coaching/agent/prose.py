"""Fact-check the figures a coach writes in prose: distances, speeds, gears, times, brake direction.

The structured fields of a coaching answer (``gain_s``, a citation's ``value``) are checked by
:mod:`rtv.coaching.agent.validation`. But a coach also writes free text — a diagnosis, cues, a
headline — and that is where the figures a driver actually acts on live: "brake at 6508 m",
"you're 10 km/h down at the apex", "brake earlier". Nothing checked those, so a plan that told
the driver to move the T7 brake point from 6540 m to ~6570 m (later, when the finding says he
was already 32 m late) still scored full factuality.

This module reads the prose with plain regular expressions (no LLM) and checks every figure it
finds against the corner the text is about:

- **metres**: a position must be one the findings carry (apex, window, a diagnostic's distance,
  or a brake/throttle point derived from one); a gap must be a gap the findings state.
- **km/h**: minimum speeds, their difference, the lock-up scrub and the stint spread.
- **gear**: the apex gears a gear diagnostic names.
- **seconds**: a corner's ``net_dt`` or a diagnostic's ``time_loss``, or a lap-level delta.
- **g**: the lateral-g figures of a grip diagnostic.
- **brake direction**: "brake earlier"/"later" (an instruction) must correct the brake-point
  finding, "N m late"/"early" (a description) must match it, and "from A m to B m" must move
  the right way.
- **corner labels**: a ``T<n>`` the findings don't have is hallucinated.

Each figure is a claim; :func:`check_prose` returns how many it checked and the ones it could
not ground. Tolerances live in :mod:`rtv.coaching.agent.validation` with the others.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rtv.coaching.agent.validation import (
    APPROX_FRACTION,
    DELTA_TOL_M,
    G_TOL,
    GAIN_TOL,
    POSITION_TOL_M,
    PROSE_SPEED_TOL,
    corner_map,
)
from rtv.grounding.units import G_RE as _G_RE
from rtv.grounding.units import GEAR_PROSE_RE as _GEAR_PROSE_RE
from rtv.grounding.units import KMH_RE as _KMH_RE
from rtv.grounding.units import METRES_RE as _METRES_RE
from rtv.grounding.units import ORDINALS as _ORDINALS
from rtv.grounding.units import SECONDS_RE as _SECONDS_RE

__all__ = ["CornerFacts", "ProseReport", "check_prose", "corner_facts", "result_prose"]

# ------------------------------------------------------------------ the findings, as facts
_BRAKE_RE = re.compile(r"Brake point (\d+(?:\.\d+)?) m (late|early)")
_THROTTLE_RE = re.compile(r"Full throttle (\d+(?:\.\d+)?) m (later|earlier) than ref")
_GEAR_RE = re.compile(r"Apex in gear (\d+) - reference used (\d+)")
_KMH_IN_DIAG_RE = re.compile(r"(\d+(?:\.\d+)?) km/h")
_G_IN_DIAG_RE = re.compile(r"(\d+\.\d+)g")


@dataclass
class CornerFacts:
    """What the findings say about one corner, in the units prose uses."""

    label: str
    positions: list[float] = field(default_factory=list)  # metres into the lap
    gaps: list[float] = field(default_factory=list)  # metre differences the findings state
    speeds: list[float] = field(default_factory=list)  # km/h
    seconds: list[float] = field(default_factory=list)  # magnitudes, s
    gears: set[int] = field(default_factory=set)
    gs: list[float] = field(default_factory=list)  # lateral g
    brake_main: float | None = None  # main lap's brake point (m)
    brake_ref: float | None = None  # reference brake point (m), derived from the diagnostic
    brake_gap: float | None = None  # main - ref (m): + = the main lap braked later


def corner_facts(corner: dict[str, Any]) -> CornerFacts:
    """Collect every figure a :class:`CornerFinding` (as a dict) supports."""
    f = CornerFacts(label=corner.get("label", "?"))
    for key in ("distance", "entry_m", "exit_m"):
        if corner.get(key) is not None:
            f.positions.append(float(corner[key]))
    lo, hi = corner.get("min_main"), corner.get("min_ref")
    for v in (lo, hi):
        if v is not None:
            f.speeds.append(float(v))
    if lo is not None and hi is not None:
        f.speeds.append(abs(float(lo) - float(hi)))
    if corner.get("net_dt") is not None:
        f.seconds.append(abs(float(corner["net_dt"])))

    for d in corner.get("diags", []):
        text, at = d.get("text", ""), float(d.get("distance", 0.0))
        f.positions.append(at)
        if d.get("time_loss"):
            f.seconds.append(abs(float(d["time_loss"])))
        if m := _BRAKE_RE.search(text):
            gap = float(m.group(1)) * (1 if m.group(2) == "late" else -1)
            f.brake_main, f.brake_gap, f.brake_ref = at, gap, at - gap
            f.positions.append(at - gap)
            f.gaps.append(abs(gap))
            continue
        if m := _THROTTLE_RE.search(text):
            gap = float(m.group(1)) * (1 if m.group(2) == "later" else -1)
            f.positions.append(at - gap)
            f.gaps.append(abs(gap))
            continue
        if m := _GEAR_RE.search(text):
            f.gears |= {int(m.group(1)), int(m.group(2))}
            continue
        f.speeds += [float(x) for x in _KMH_IN_DIAG_RE.findall(text)]
        f.gs += [float(x) for x in _G_IN_DIAG_RE.findall(text)]
    return f


def _lap_seconds(findings: dict[str, Any]) -> list[float]:
    """Lap-level time figures a headline may quote (total available, net lap, sectors)."""
    out: list[float] = []
    chief = findings.get("chief", {})
    for key in ("lost", "net_lap", "time_spread"):
        if chief.get(key) is not None:
            out.append(abs(float(chief[key])))
    for s in findings.get("sectors", []):
        if s.get("delta") is not None:
            out.append(abs(float(s["delta"])))
    return out


# ------------------------------------------------------------------ reading the prose
_LABEL_RE = re.compile(r"\b(?:T|Turn\s)(\d+)\b")
_FROM_TO_RE = re.compile(
    r"from\s+(?:~|about |around )?" + r"(?P<a>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(?:m\b)?"
    r"\s*(?:to|->|→)\s*(?:~|about |around )?(?P<b>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?m\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
# A clause boundary, so "brake earlier and get on the power later" reads as two claims.
_CLAUSE_RE = re.compile(r",|;|\s[-–—]\s|\b(?:and|but|then|while|so)\b", re.IGNORECASE)
_BRAKE_WORD = re.compile(r"\bbrak(?:e|es|ed|ing)\b", re.IGNORECASE)
# A clause about the other pedal, or about releasing the brake, says nothing about where
# braking starts.
_NOT_BRAKE_POINT = re.compile(
    r"\b(?:throttle|power|gas|accelerat\w*|releas\w*|off the brakes?|trail\w*)\b", re.IGNORECASE
)
_COMPARATIVE = re.compile(r"\b(?P<w>earlier|sooner|later)\b(?P<than>\s+than\b)?", re.IGNORECASE)
_ADJECTIVE = re.compile(r"\b(?P<w>late|early)\b", re.IGNORECASE)


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _labels(text: str) -> list[str]:
    return list(dict.fromkeys(f"T{n}" for n in _LABEL_RE.findall(text)))


def _near(value: float, truths: list[float], tol: float, approx: bool) -> bool:
    for t in truths:
        allowed = max(tol * 2, abs(t) * APPROX_FRACTION) if approx else tol
        if abs(value - t) <= allowed:
            return True
    return False


@dataclass
class ProseReport:
    claims: int = 0
    supported: int = 0
    issues: list[str] = field(default_factory=list)
    corners: list[str] = field(default_factory=list)  # the corner each issue is about

    def ok(self) -> None:
        self.claims += 1
        self.supported += 1

    def bad(self, message: str, corner: str) -> None:
        self.claims += 1
        self.issues.append(message)
        self.corners.append(corner)


def _merge(facts: list[CornerFacts]) -> CornerFacts:
    if len(facts) == 1:
        return facts[0]
    m = CornerFacts(label="/".join(f.label for f in facts))
    for f in facts:
        m.positions += f.positions
        m.gaps += f.gaps
        m.speeds += f.speeds
        m.seconds += f.seconds
        m.gears |= f.gears
        m.gs += f.gs
    return m


def _check_brake_clause(clause: str, f: CornerFacts, where: str, rep: ProseReport) -> None:
    """A brake-point direction claim in ``clause`` must agree with the brake finding."""
    claimed: tuple[str, str] | None = None  # (kind, direction): kind is "fix" or "state"
    if m := _FROM_TO_RE.search(clause):
        a, b = _num(m.group("a")), _num(m.group("b"))
        if a != b:
            claimed = ("fix", "earlier" if b < a else "later")
    elif m := _COMPARATIVE.search(clause):
        word = m.group("w").lower()
        word = "earlier" if word == "sooner" else word
        # "braked 32 m later than the reference" describes; "brake earlier" instructs.
        claimed = ("state" if m.group("than") else "fix", word)
    elif m := _ADJECTIVE.search(clause):
        claimed = ("state", "later" if m.group("w").lower() == "late" else "earlier")
    if claimed is None:
        return
    kind, direction = claimed
    if f.brake_gap is None:
        rep.bad(
            f"{where}: says the brake point should be or was {direction} at {f.label}, but the "
            f"findings have no brake-point difference there.",
            f.label,
        )
        return
    was = "later" if f.brake_gap > 0 else "earlier"
    want = was if kind == "state" else ("earlier" if was == "later" else "later")
    if direction == want:
        rep.ok()
        return
    ref = f"; the reference brakes at {f.brake_ref:.0f} m" if f.brake_ref is not None else ""
    verb = "says the driver braked" if kind == "state" else "tells the driver to brake"
    rep.bad(
        f"{where}: {verb} {direction} at {f.label}, but the finding is brake point "
        f"{abs(f.brake_gap):.0f} m {'late' if f.brake_gap > 0 else 'early'} "
        f"(main {f.brake_main:.0f} m{ref}).",
        f.label,
    )


def _check_sentence(
    sentence: str,
    owner: str | None,
    facts: dict[str, CornerFacts],
    lap_seconds: list[float],
    where: str,
    rep: ProseReport,
) -> None:
    labels = _labels(sentence)
    for label in labels:
        if label not in facts:
            rep.bad(f"{where}: mentions corner {label}, which is not in the findings.", label)
    scope = [facts[lb] for lb in labels if lb in facts]
    if labels and not scope:
        return  # only a hallucinated corner: already flagged, nothing true to check against
    if not scope and owner in facts:
        scope = [facts[owner]]
    if not scope:
        scope = list(facts.values())  # a headline with no corner: any corner's figure will do
    if not scope:
        return
    f = _merge(scope)

    # Brake-point direction, clause by clause, where the clause is about where braking starts
    # and the corner is known: a direction can't be checked against "some corner".
    for clause in _CLAUSE_RE.split(sentence):
        if _BRAKE_WORD.search(clause) and not _NOT_BRAKE_POINT.search(clause):
            named = [facts[lb] for lb in _labels(clause) if lb in facts]
            target = named or (scope if len(scope) == 1 else [])
            for corner in target:
                _check_brake_clause(clause, corner, where, rep)

    for m in _METRES_RE.finditer(sentence):
        v, approx = _num(m.group("num")), bool(m.group("approx"))
        # A position is a point on the track: "~6570 m" may round, not move, so its slack is
        # fixed; a gap ("~30 m late") may round by a fraction of itself.
        pos_tol = POSITION_TOL_M * (2 if approx else 1)
        if _near(v, f.positions, pos_tol, False) or _near(v, f.gaps, DELTA_TOL_M, approx):
            rep.ok()
        else:
            quoted = m.group(0).strip()
            rep.bad(f"{where}: {quoted} at {f.label} matches no distance in the findings.", f.label)

    for regex, truths, tol, noun in (
        (_KMH_RE, f.speeds, PROSE_SPEED_TOL, "speed"),
        (_SECONDS_RE, f.seconds + lap_seconds, GAIN_TOL, "time"),
        (_G_RE, f.gs, G_TOL, "lateral g"),
    ):
        for m in regex.finditer(sentence):
            if _near(_num(m.group("num")), truths, tol, bool(m.group("approx"))):
                rep.ok()
            else:
                quoted = m.group(0).strip()
                msg = f"{where}: {quoted} at {f.label} matches no {noun} in the findings."
                rep.bad(msg, f.label)

    for m in _GEAR_PROSE_RE.finditer(sentence):
        raw = m.group("a") or m.group("b") or m.group("c")
        gear = _ORDINALS[raw.lower()] if raw.lower() in _ORDINALS else int(raw)
        if gear in f.gears:
            rep.ok()
        else:
            msg = f"{where}: gear {gear} at {f.label} is not a gear the findings name there."
            rep.bad(msg, f.label)


def check_prose(texts: list[tuple[str, str | None, str]], findings: dict[str, Any]) -> ProseReport:
    """Check ``(where, corner_label_or_None, text)`` triples against the findings.

    The corner is the one the text belongs to (an advice's or a priority's); a sentence that
    names corners is checked against those instead, and a sentence with neither (a headline
    with no label) against every corner.
    """
    facts = {label: corner_facts(c) for label, c in corner_map(findings).items()}
    lap_seconds = _lap_seconds(findings)
    rep = ProseReport()
    for where, owner, text in texts:
        for sentence in _SENTENCE_RE.split(text or ""):
            if sentence.strip():
                _check_sentence(sentence, owner, facts, lap_seconds, where, rep)
    return rep


def result_prose(result: dict[str, Any]) -> list[tuple[str, str | None, str]]:
    """The prose of an orchestrator ``CoachResult.model_dump()``, tagged with its corner."""
    out: list[tuple[str, str | None, str]] = []
    plan = result.get("plan") or {}
    if plan:
        out.append(("plan.headline", None, plan.get("headline", "")))
        out.append(("plan.one_lap_focus", None, plan.get("one_lap_focus", "")))
        for p in plan.get("priorities", []):
            out.append((f"plan.priority[{p.get('corner')}].why", p.get("corner"), p.get("why", "")))
    for a in result.get("advices", []):
        corner = a.get("corner")
        out.append((f"advice[{corner}].diagnosis", corner, a.get("diagnosis", "")))
        for i, cue in enumerate(a.get("cues", [])):
            out.append((f"advice[{corner}].cue[{i}]", corner, cue))
    return out
