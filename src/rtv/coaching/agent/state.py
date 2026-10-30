"""The graph state for the coach agent (racing analog of the reference ``agent/state.py``).

What is tracked beyond ``messages`` is the **trace**: ``tools_called``, ``corners_seen`` and
``findings_by_key`` are the provenance a reviewer (and the eval harness) needs to attribute a
failure to retrieval, tool use, or generation — not just observe that "the advice was bad."
The trace is built here so downstream phases can render it; they cannot retroactively invent
it.

What must *not* live here: raw 60 Hz telemetry. The whole tiered design keeps that out of the
context window — the tools return the ~20-finding model, never the samples.
"""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from rtv.coaching.agent.contracts import CoachingAnswer, CoachRefusal


class CoachState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    question: str = ""

    # Loop control.
    iteration: int = 0
    max_iterations: int = 6
    forced_final: bool = False  # the cap tripped and the degraded-honest turn was taken
    validation_retries: int = 0
    max_validation_retries: int = 2

    # Provenance — everything the advice rests on.
    tools_called: list[str] = Field(default_factory=list)
    corners_seen: list[str] = Field(default_factory=list)  # corner labels retrieved
    # Raw ``LapFindings.to_dict()`` payloads harvested from get_lap_findings results, keyed
    # "session:main:ref". This is the validator's ground truth — the numbers actually returned.
    findings_by_key: dict[str, dict[str, Any]] = Field(default_factory=dict)
    channels_available: list[str] | None = None  # from the last list_available_channels result

    # The identity of the most recently retrieved findings — code-known provenance the answer
    # is stamped with (the model is never asked for it).
    session_id: str | None = None
    main_lap: int | None = None
    ref_lap: int | None = None

    # Outcome — exactly one of these is set at END.
    answer: CoachingAnswer | None = None
    refusal: CoachRefusal | None = None
