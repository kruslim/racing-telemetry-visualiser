"""The numeric citation validator — shared by the in-loop validate node and the eval harness.

The coach's structured output carries explicit figures (which corner, how much time, what
minimum speed). The deterministic findings carry the true figures. So grounding is a numeric
comparison, not a vibe check: a claimed ``gain_s`` must match the corner's ``net_dt``; a cited
corner must exist; a cited min-speed must match the finding.

This lives in the installed ``rtv`` package (not in the top-level ``evals/``) precisely so both
consumers can reach it: the graph's ``validate_node`` bounces a hallucinated figure *mid-run*,
and ``evals.checks.cross_check`` scores a finished result *after the fact*. Same truth, one
place — they can never disagree.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

GAIN_TOL = 0.05  # seconds; how close a claimed time gain must be to the finding
SPEED_TOL = 0.5  # km/h; tolerance for a cited minimum-speed figure

# Figures written in prose (``rtv.coaching.agent.prose``). The findings' own texts round to whole
# metres and km/h, so prose quoting them must be allowed that rounding and no more.
POSITION_TOL_M = 5.0  # metres; a quoted track position (brake point, apex)
DELTA_TOL_M = 1.5  # metres; a quoted gap ("32 m late")
PROSE_SPEED_TOL = 1.0  # km/h; a speed in prose, against the finding or its rounded text
G_TOL = 0.02  # g; a quoted lateral-g figure
APPROX_FRACTION = 0.1  # "~30 m", "about 0.3 s": the slack a hedged gap or time gets

# Which citable figures map to a direct field on a CornerFinding, and each field's tolerance.
# ``time_loss`` (per-diagnostic) and ``brake_m`` (free-form diagnostic text) are validated only
# for corner-existence here — matching their exact value is a deliberate follow-up.
_FIGURE_FIELD = {"net_dt": "net_dt", "min_main": "min_main", "min_ref": "min_ref"}
_FIGURE_TOL = {"net_dt": GAIN_TOL, "min_main": SPEED_TOL, "min_ref": SPEED_TOL}


def corner_map(findings: dict[str, Any]) -> dict[str, dict]:
    """Label -> corner-finding, across a (possibly merged) findings dict."""
    return {c["label"]: c for c in findings.get("corners", [])}


#: ``T7``, ``T7 La Source``, ``T7 — Campus``, ``T7: Pouhon``, ``Turn 7 (Eau Rouge)``.
_CITED_RE = re.compile(r"^\s*(?:T|Turn\s*)(\d+)\b[\s\-\u2013\u2014:,(]*(.*?)[\s)]*$", re.I)


def name_key(name: str | None) -> str:
    """A corner name folded for comparison: case, accents and punctuation do not count."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.casefold()).split())


def split_corner(cited: str | None) -> tuple[str | None, str | None]:
    """``"T7 — Campus"`` -> ``("T7", "Campus")``; ``"T7"`` -> ``("T7", None)``.

    Anything that is not a ``T<n>`` label comes back as ``(cited, None)`` unchanged, so it
    still fails the lookup as the hallucinated label it is.
    """
    if cited is None:
        return None, None
    m = _CITED_RE.match(str(cited))
    if not m:
        return str(cited), None
    return f"T{int(m.group(1))}", (m.group(2).strip() or None)


def resolve_corner(
    corners: dict[str, dict], cited: str | None, where: str
) -> tuple[dict | None, str | None]:
    """The finding a cited corner refers to, or an issue saying why it refers to none.

    The label decides which corner it is. A name written next to it must be that corner's
    mapped name: ``T7 Campus`` for a T7 the findings call Pouhon (or leave unnamed) is a
    made-up name, not a different way of writing T7.
    """
    label, name = split_corner(cited)
    corner = corners.get(label) if label is not None else None
    if corner is None:
        return None, f"{where}: corner {cited!r} is not in the findings (hallucinated)."
    if name is not None and name_key(name) != name_key(corner.get("name")):
        truth = corner.get("name")
        said = f"{label} is {truth}" if truth else f"{label} has no mapped name"
        return None, f"{where}: corner {cited!r} names it {name!r}, but {said}."
    return corner, None


def gain_issue(
    corners: dict[str, dict], corner_label: str, claimed: float, where: str, gain_tol: float
) -> str | None:
    """Return an issue message if ``claimed`` gain for ``corner_label`` is unsupported, else None.

    ``where`` labels the source of the claim (e.g. ``priority``) in the message. The label may
    carry the corner's name (``T7 Pouhon``); see :func:`resolve_corner`."""
    c, issue = resolve_corner(corners, corner_label, where)
    if c is None:
        return issue
    truth = c.get("net_dt", 0.0)
    if abs(claimed - truth) <= gain_tol:
        return None
    return f"{where}: {corner_label} claimed +{claimed:.3f}s but finding net_dt is {truth:+.3f}s."


def check_figures(
    priorities: list[dict],
    claims: list[dict],
    findings: dict[str, Any],
    *,
    gain_tol: float = GAIN_TOL,
) -> list[str]:
    """Return the offending figures in a coaching answer — empty when every figure is grounded.

    ``priorities`` are ``{corner, gain_s}`` dicts; ``claims`` are ``{corner, citations:[{corner,
    figure, value, unit}]}`` dicts (the graph payload, dumped). Each priority's ``gain_s`` must
    match its corner's ``net_dt``; each citation's figure must match the corresponding field on
    the cited corner (or, for figures with no direct field, the corner must at least exist).
    """
    corners = corner_map(findings)
    issues: list[str] = []

    for p in priorities:
        issue = gain_issue(
            corners, p.get("corner", "?"), float(p.get("gain_s", 0.0)), "priority", gain_tol
        )
        if issue:
            issues.append(issue)

    for claim in claims:
        for cit in claim.get("citations", []):
            label = cit.get("corner", "?")
            corner, issue = resolve_corner(corners, label, "citation")
            if corner is None:
                issues.append(issue or "")
                continue
            figure = cit.get("figure")
            field = _FIGURE_FIELD.get(figure)
            if field is None:
                continue  # time_loss / brake_m: corner-existence only (see module docstring)
            claimed = float(cit.get("value", 0.0))
            truth = float(corner.get(field, 0.0))
            if abs(claimed - truth) > _FIGURE_TOL[figure]:
                issues.append(
                    f"citation: {label}.{figure} claimed {claimed:g} but finding is {truth:g}."
                )
    return issues
