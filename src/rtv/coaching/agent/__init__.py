"""Layer 3b — the LangGraph coach agent.

A model-driven, tool-calling loop over the deterministic ``LapFindings`` ground truth, with
grounded refusal and an in-loop citation validator. It sits *alongside* the async
``orchestrator.py`` (the multi-agent showcase), not in place of it — the two share only the
findings and the eval harness.

Public surface:

    run_coach(question, provider, model) -> CoachState

``provider`` is a :class:`~rtv.coaching.agent.provider.FindingsProvider` — the live
``RepositoryFindingsProvider`` in production, or a synthetic fixture in evals. ``model`` is any
LangChain chat model (or a scripted fake in tests).
"""

from __future__ import annotations

from rtv.coaching.agent.graph import (
    REFUSE_TOOL,
    SUBMIT_COACHING_TOOL,
    build_graph,
    run_coach,
)
from rtv.coaching.agent.provider import FindingsProvider, RepositoryFindingsProvider

__all__ = [
    "run_coach",
    "build_graph",
    "SUBMIT_COACHING_TOOL",
    "REFUSE_TOOL",
    "FindingsProvider",
    "RepositoryFindingsProvider",
]
