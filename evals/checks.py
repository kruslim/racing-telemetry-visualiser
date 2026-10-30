"""Deterministic ground-truth cross-checks for coach output — no LLM, no network.

The coach's structured output carries explicit figures (which corner, how much time). The
deterministic findings carry the true figures. So factuality is a numeric comparison, not a
vibe check: a claimed ``gain_s`` must match the corner's ``net_dt``; a cited corner must exist;
the top priority should be the corner that actually loses the most time.

The per-figure numeric core lives in :mod:`rtv.coaching.agent.validation` (in the installed
package) so it is shared with the graph coach's in-loop validate node — the same truth that
scores a finished result here also bounces a hallucinated figure mid-run. This module keeps the
result-level scoring (``cross_check``/``aggregate``) the orchestrator eval harness uses.
"""

from __future__ import annotations

from typing import Any

from rtv.coaching.agent.validation import GAIN_TOL, corner_map, gain_issue

__all__ = ["GAIN_TOL", "cross_check", "aggregate"]


def cross_check(
    result: dict[str, Any], findings: dict[str, Any], *, gain_tol: float = GAIN_TOL
) -> dict:
    """Score a coach result (``CoachResult.model_dump()``) against the findings.

    Returns factuality metrics and a list of human-readable issues. Each issue is a
    figure the coach asserted that the ground-truth findings do not support.
    """
    corners = corner_map(findings)
    top3 = findings.get("chief", {}).get("top3", [])
    truth_top = top3[0]["label"] if top3 else None

    issues: list[str] = []
    claims = 0
    supported = 0

    plan = result.get("plan") or {}
    priorities = plan.get("priorities", []) if plan else []

    picked_top = priorities[0]["corner"] if priorities else None
    outcome_ok = picked_top == truth_top
    if not outcome_ok:
        issues.append(
            f"Top priority is {picked_top!r} but the data's biggest loss is {truth_top!r}."
        )

    def check_gain(corner_label: str, claimed: float, where: str) -> None:
        nonlocal claims, supported
        claims += 1
        issue = gain_issue(corners, corner_label, claimed, where, gain_tol)
        if issue is None:
            supported += 1
        else:
            issues.append(issue)

    for p in priorities:
        check_gain(p.get("corner", "?"), float(p.get("gain_s", 0.0)), "plan.priority")
    for a in result.get("advices", []):
        check_gain(a.get("corner", "?"), float(a.get("est_gain_s", 0.0)), "advice")

    factuality = supported / claims if claims else 1.0
    return {
        "outcome_accuracy": outcome_ok,
        "claim_factuality": factuality,
        "claims": claims,
        "supported": supported,
        "hallucinations": len(issues) - (0 if outcome_ok else 1),
        "issues": issues,
    }


def aggregate(rows: list[dict]) -> dict:
    """Aggregate per-case cross_check results into headline metrics."""
    if not rows:
        return {"cases": 0}
    n = len(rows)
    return {
        "cases": n,
        "outcome_accuracy": sum(1 for r in rows if r["outcome_accuracy"]) / n,
        "mean_claim_factuality": sum(r["claim_factuality"] for r in rows) / n,
        "total_claims": sum(r["claims"] for r in rows),
        "total_hallucinations": sum(max(0, r["hallucinations"]) for r in rows),
    }
