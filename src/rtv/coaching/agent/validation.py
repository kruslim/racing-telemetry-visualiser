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

from typing import Any

GAIN_TOL = 0.05  # seconds; how close a claimed time gain must be to the finding
SPEED_TOL = 0.5  # km/h; tolerance for a cited minimum-speed figure

# Which citable figures map to a direct field on a CornerFinding, and each field's tolerance.
# ``time_loss`` (per-diagnostic) and ``brake_m`` (free-form diagnostic text) are validated only
# for corner-existence here — matching their exact value is a deliberate follow-up.
_FIGURE_FIELD = {"net_dt": "net_dt", "min_main": "min_main", "min_ref": "min_ref"}
_FIGURE_TOL = {"net_dt": GAIN_TOL, "min_main": SPEED_TOL, "min_ref": SPEED_TOL}


def corner_map(findings: dict[str, Any]) -> dict[str, dict]:
    """Label -> corner-finding, across a (possibly merged) findings dict."""
    return {c["label"]: c for c in findings.get("corners", [])}


def gain_issue(
    corners: dict[str, dict], corner_label: str, claimed: float, where: str, gain_tol: float
) -> str | None:
    """Return an issue message if ``claimed`` gain for ``corner_label`` is unsupported, else None.

    ``where`` labels the source of the claim (e.g. ``priority``) in the message."""
    c = corners.get(corner_label)
    if c is None:
        return f"{where}: corner {corner_label!r} is not in the findings (hallucinated)."
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
            corner = corners.get(label)
            if corner is None:
                issues.append(f"citation: corner {label!r} is not in the findings (hallucinated).")
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
