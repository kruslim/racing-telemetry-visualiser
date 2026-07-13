"""The regression runner — replay each case against the current coach.

    seed cases ──► run each against the graph coach
                          │
                          ▼
                deterministic fixture (evals.fixtures)
                          │
                          ▼
                assert: outcome, citations, priority, refusal reason  (assertions.py)
                          │
                          ▼
                report: pass rate, regressions vs. last run

This is the ratchet: a case that passes today and fails after a change is a merge-blocker; a
case that starts passing is the flywheel paying off. The runner is model-agnostic — it takes a
``model_for`` factory, so the same code runs a live model (one model reused for every case) or a
hermetic scripted model per case in CI. The determinism the suite relies on lives in the
*fixture* (the data), which is why the assertions are stable even when the model is not.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from evals.assertions import CaseResult, check_case
from evals.fixtures import build_fixture
from evals.schemas import EvalCase
from evals.trace import CoachTrace
from rtv.coaching.agent.graph import run_coach

# A factory that yields the model to run a given case against. A live run returns the same model
# for every case (``lambda _case: chat_model``); a hermetic test returns a scripted model tailored
# to each case's expected tool sequence.
ModelFor = Callable[[EvalCase], Any]


class RegressionReport(BaseModel):
    """The outcome of one regression run over a set of cases."""

    results: list[CaseResult] = Field(default_factory=list)

    @property
    def n_total(self) -> int:
        return len(self.results)

    @property
    def n_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.n_passed / self.n_total if self.results else 0.0

    def baseline_map(self) -> dict[str, bool]:
        """A ``{case_id: passed}`` snapshot, to persist and diff a future run against."""
        return {r.case_id: r.passed for r in self.results}

    def regressions(self, baseline: dict[str, bool]) -> list[str]:
        """Cases that passed in ``baseline`` but fail now — the merge-blockers."""
        return [r.case_id for r in self.results if baseline.get(r.case_id) and not r.passed]

    def newly_fixed(self, baseline: dict[str, bool]) -> list[str]:
        """Cases that failed in ``baseline`` and pass now — the flywheel paying off."""
        return [r.case_id for r in self.results if r.passed and baseline.get(r.case_id) is False]

    def render(self) -> str:
        lines = [f"regression: {self.n_passed}/{self.n_total} passed ({self.pass_rate:.0%})", ""]
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{mark}] {r.case_id}  ({r.outcome})")
            for f in r.failures:
                lines.append(f"         ✗ {f.name}: {f.detail}")
        return "\n".join(lines)


def run_case(
    case: EvalCase, model: Any, *, max_iterations: int = 6
) -> tuple[CoachTrace, CaseResult]:
    """Run one case end to end: fixture → coach → trace → hard assertions."""
    provider = build_fixture(case.source_fixture)
    state = run_coach(case.question, provider, model, max_iterations=max_iterations)
    trace = CoachTrace.from_state(state, trace_id=f"trace_{case.case_id}")
    return trace, check_case(case, trace)


def run_regression(
    cases: Sequence[EvalCase],
    model_for: ModelFor,
    *,
    max_iterations: int = 6,
) -> RegressionReport:
    """Replay ``cases`` against the coach and score each with hard assertions."""
    results = [run_case(case, model_for(case), max_iterations=max_iterations)[1] for case in cases]
    return RegressionReport(results=results)
