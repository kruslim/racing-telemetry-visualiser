"""Hold every section of a debrief to its inputs (DB2): figures, laps, corners, priorities.

Four checks, all deterministic:

* **Figures**: every number written in a section must be a fact of the inputs, through the
  typed :class:`~rtv.pitwall.validator.FactSet` (plan task A1): a figure said with a unit
  ("2.4 s", "45.1 L", "P4", "lap 7") must match a fact of that unit family, so the right
  number for the wrong fact is refused. A rejected plan proposal's reason is left out of the
  fact set, so a number the engine refused cannot come back as a fact.
* **Laps**: a structured ``laps`` entry must be a recorded lap of the run, and a lap number in
  the prose ("lap 7", "laps 3 to 5") must be a recorded lap or a lap the plan named.
* **Corners**: a structured ``corner`` must be a corner the run knows (the lap findings, or a
  race event's place), and a name written next to its label must be that corner's name
  (``resolve_corner``, plan task E1). The prose goes through A2's
  :func:`~rtv.coaching.agent.prose.check_prose` in its post-race ``lap`` mode against the
  Layer-1 corners: a ``T<n>`` the findings lack and a brake direction they contradict are
  refused. Its figure rules are not used here: those are checked against every input above,
  not against one lap's corner.
* **Priorities**: each must rest on a section above it that was kept (not refused and not
  empty), and a priority about a corner must name a corner that section discusses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rtv.coaching.agent.prose import check_prose
from rtv.coaching.agent.validation import resolve_corner, split_corner
from rtv.debrief.contract import DebriefAnswer, DebriefItem, DebriefPriority
from rtv.grounding.units import read_figures
from rtv.pitwall.validator import FactSet

#: "lap 7", "Lap 12", "laps 3 to 5", "laps 3-5", "laps 4, 6 and 9".
_LAP_REF_RE = re.compile(
    r"\blaps?\s+(\d+(?:\s*(?:,|-|–|to|and|&)\s*\d+)*)", re.IGNORECASE
)
_LAP_NUM_RE = re.compile(r"\d+")
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
#: ProseReport kinds kept from check_prose: everything but its per-corner figure rule.
_PROSE_KINDS_KEPT = frozenset({"corner", "brake", "name"})


@dataclass
class SectionReport:
    """The verdict on one section."""

    ok: bool = True
    issues: list[str] = field(default_factory=list)
    figures: int = 0

    def bad(self, issue: str) -> None:
        self.ok = False
        if issue not in self.issues:
            self.issues.append(issue)


def fact_set(inputs: dict[str, Any]) -> FactSet:
    """Every figure the debrief may cite, typed by the field it came from."""
    facts = FactSet(typed=True)
    for key, value in inputs.items():
        if key == "plan":
            plan = dict(value or {})
            # The reason a proposal was rejected may quote the rejected numbers.
            plan["rejected"] = [
                {k: v for k, v in r.items() if k != "reason"} for r in plan.get("rejected") or []
            ]
            facts.add("plan", plan)
        else:
            facts.add(key, value)
    return facts


def known_corners(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Label -> corner (with its name) for every corner the run knows."""
    names = inputs.get("corner_names") or {}
    out: dict[str, dict[str, Any]] = {}
    for corner in (inputs.get("findings") or {}).get("corners") or []:
        label = corner.get("label")
        if label:
            out[label] = {**corner, "name": corner.get("name") or names.get(label)}
    for event in inputs.get("events") or []:
        label = event.get("corner")
        if label and not event.get("provisional") and label not in out:
            out[label] = {"label": label, "name": event.get("name") or names.get(label)}
    return out


def _prose_findings(inputs: dict[str, Any], corners: dict[str, dict[str, Any]]) -> dict:
    """What check_prose reads: the Layer-1 corners, plus bare labels the events know."""
    rows = []
    for label, corner in corners.items():
        rows.append(corner if "diags" in corner else {"label": label, "name": corner.get("name")})
    return {"corners": rows}


def allowed_prose_laps(inputs: dict[str, Any]) -> set[int]:
    """Recorded laps, plus the lap numbers the plan named (a planned stop may lie beyond)."""
    laps = set(inputs.get("recorded_laps") or [])
    plan = inputs.get("plan") or {}
    final = plan.get("final") or {}
    for key, value in final.items():
        if key.endswith("_lap") and isinstance(value, int | float):
            laps.add(int(value))
    for rev in plan.get("revisions") or []:
        for side in ("from", "to"):
            values = rev.get(side) if isinstance(rev.get(side), dict) else {}
            for key, value in values.items():
                if key.endswith("_lap") and isinstance(value, int | float):
                    laps.add(int(value))
    return laps


def check_text(
    text: str,
    where: str,
    *,
    owner: str | None,
    facts: FactSet,
    inputs: dict[str, Any],
    corners: dict[str, dict[str, Any]],
    rep: SectionReport,
) -> None:
    """Figures, lap references and corner prose of one piece of text."""
    for sentence in _SENTENCE_RE.split(text or ""):
        if not sentence.strip():
            continue
        figures = read_figures(sentence)
        for figure in figures:
            rep.figures += 1
            if facts.supports_figure(figure) or facts.supports_difference(figure, figures):
                continue
            rep.bad(f"{where}: {facts.explain(figure)}")
    laps = allowed_prose_laps(inputs)
    for m in _LAP_REF_RE.finditer(text or ""):
        for n in _LAP_NUM_RE.findall(m.group(1)):
            if int(n) not in laps:
                rep.bad(f"{where}: lap {n} was not recorded in this run.")
    prose = check_prose(
        [(where, owner, text or "")], _prose_findings(inputs, corners), mode="lap"
    )
    for issue, kind in zip(prose.issues, prose.kinds, strict=True):
        if kind in _PROSE_KINDS_KEPT:
            rep.bad(issue)


def _check_corner(
    cited: str | None, where: str, corners: dict[str, dict[str, Any]], rep: SectionReport
) -> str | None:
    if cited is None or not str(cited).strip():
        return None
    corner, issue = resolve_corner(corners, cited, where)
    if issue is not None:
        rep.bad(issue)
        return None
    return corner["label"] if corner else None


def check_items(
    items: list[DebriefItem],
    section: str,
    *,
    facts: FactSet,
    inputs: dict[str, Any],
    corners: dict[str, dict[str, Any]],
) -> SectionReport:
    rep = SectionReport()
    recorded = set(inputs.get("recorded_laps") or [])
    for i, item in enumerate(items):
        where = f"{section}[{i}]"
        label = _check_corner(item.corner, where, corners, rep)
        for lap in item.laps:
            if lap not in recorded:
                rep.bad(f"{where}: lap {lap} was not recorded in this run.")
        check_text(
            item.text,
            where,
            owner=label,
            facts=facts,
            inputs=inputs,
            corners=corners,
            rep=rep,
        )
    return rep


def check_prose_section(
    text: str,
    section: str,
    *,
    facts: FactSet,
    inputs: dict[str, Any],
    corners: dict[str, dict[str, Any]],
) -> SectionReport:
    rep = SectionReport()
    if not (text or "").strip():
        rep.bad(f"{section}: empty.")
        return rep
    check_text(text, section, owner=None, facts=facts, inputs=inputs, corners=corners, rep=rep)
    return rep


def section_corners(content: Any) -> set[str]:
    """The corner labels a kept section discusses."""
    out: set[str] = set()
    if isinstance(content, list):
        for item in content:
            data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            label = split_corner(data.get("corner"))[0]
            if label:
                out.add(label)
            out.update(_labels(data.get("text") or ""))
    elif isinstance(content, str):
        out.update(_labels(content))
    return out


_LABEL_RE = re.compile(r"\b(?:T|Turn\s)(\d+)\b")


def _labels(text: str) -> set[str]:
    return {f"T{n}" for n in _LABEL_RE.findall(text or "")}


def check_priorities(
    priorities: list[DebriefPriority],
    kept: dict[str, Any],
    *,
    facts: FactSet,
    inputs: dict[str, Any],
    corners: dict[str, dict[str, Any]],
) -> SectionReport:
    """``kept``: section name -> its kept content (refused or empty sections are absent)."""
    rep = SectionReport()
    if not priorities:
        rep.bad("priorities: empty.")
    for i, prio in enumerate(priorities):
        where = f"priorities[{i}]"
        content = kept.get(prio.basis)
        if content in (None, "", []):
            rep.bad(f"{where}: rests on '{prio.basis}', which has nothing kept to support it.")
            continue
        label = _check_corner(prio.corner, where, corners, rep)
        if label is not None and label not in section_corners(content):
            rep.bad(f"{where}: {label} is not discussed in '{prio.basis}'.")
        check_text(
            prio.text, where, owner=label, facts=facts, inputs=inputs, corners=corners, rep=rep
        )
    return rep


def check_section(
    name: str,
    answer: DebriefAnswer,
    *,
    facts: FactSet,
    inputs: dict[str, Any],
    corners: dict[str, dict[str, Any]],
) -> SectionReport:
    """One non-priority section of ``answer``."""
    value = getattr(answer, name)
    if isinstance(value, list):
        return check_items(value, name, facts=facts, inputs=inputs, corners=corners)
    return check_prose_section(value, name, facts=facts, inputs=inputs, corners=corners)
