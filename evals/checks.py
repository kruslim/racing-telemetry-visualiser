"""Deterministic ground-truth cross-checks for coach output — no LLM, no network.

The coach's structured output carries explicit figures (which corner, how much time).
The deterministic findings carry the true figures. So factuality is a numeric comparison,
not a vibe check: a claimed ``gain_s`` must match the corner's ``net_dt``; a cited corner
must exist; the top priority should be the corner that actually loses the most time.

This is what makes the eval rigorous and cheap — it catches a coach that invents a
brake-point metre value or a time gain the data doesn't support.
"""

from __future__ import annotations

from typing import Any

GAIN_TOL = 0.05  # seconds; how close a claimed time gain must be to the finding


def cross_check(
    result: dict[str, Any], findings: dict[str, Any], *, gain_tol: float = GAIN_TOL
) -> dict:
    """Score a coach result (``CoachResult.model_dump()``) against the findings.

    Returns factuality metrics and a list of human-readable issues. Each issue is a
    figure the coach asserted that the ground-truth findings do not support.
    """
    corners = {c["label"]: c for c in findings.get("corners", [])}
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
        c = corners.get(corner_label)
        if c is None:
            issues.append(
                f"{where}: corner {corner_label!r} is not in the findings (hallucinated)."
            )
            return
        truth = c.get("net_dt", 0.0)
        if abs(claimed - truth) <= gain_tol:
            supported += 1
        else:
            issues.append(
                f"{where}: {corner_label} claimed +{claimed:.3f}s "
                f"but finding net_dt is {truth:+.3f}s."
            )

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
