"""The debrief's output contract: what the model is forced to answer with (DB2).

One structured answer through the forced respond tool (never ``messages.parse``). Every
figure is written in prose, where the typed FactSet checks it; the structured fields are
only references (lap numbers, a corner label, which section a priority rests on), and
those are checked for existence: a lap that was not recorded, or a corner the findings
do not have, fails its section.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: The sections, in reading order. ``priorities`` comes last and rests on the others.
SECTIONS = ("summary", "plan", "time_lost", "recurring", "tyres_fuel", "priorities")

SECTION_TITLES = {
    "summary": "Race summary",
    "plan": "The plan and where it changed",
    "time_lost": "Where time was lost",
    "recurring": "Recurring issues by corner",
    "tyres_fuel": "Tyres and fuel against the projection",
    "priorities": "What to work on",
}

#: The sections a priority may rest on.
Basis = Literal["time_lost", "recurring", "tyres_fuel", "plan"]


class DebriefItem(BaseModel):
    """One point of a list section, with the laps and corner it is about."""

    text: str = Field(description="One or two sentences. Every figure must be in the inputs.")
    corner: str | None = Field(
        default=None,
        description="The corner label as the inputs write it ('T5', or 'T5 Pouhon'); "
        "null when the point is not about one corner.",
    )
    laps: list[int] = Field(
        default_factory=list,
        description="The recorded laps the point is about (from inputs.recorded_laps).",
    )


class DebriefPriority(BaseModel):
    """One thing to work on, resting on a section above it."""

    text: str = Field(description="What to work on and why, in one or two sentences.")
    basis: Basis = Field(description="The section above that supports this priority.")
    corner: str | None = Field(
        default=None,
        description="The corner, when the priority is about one; it must appear in the "
        "basis section.",
    )


class DebriefAnswer(BaseModel):
    """The analysis half of a debrief."""

    summary: str = Field(description="The race in three to five sentences: result, pace, events.")
    plan: str = Field(
        description="What the race plan was and where it changed (accepted revisions, "
        "rejected proposals), or that there was none."
    )
    time_lost: list[DebriefItem] = Field(
        default_factory=list,
        description="Where time was lost, from the lap findings and lap times; most first.",
    )
    recurring: list[DebriefItem] = Field(
        default_factory=list,
        description="Issues that came back at the same corner on several laps.",
    )
    tyres_fuel: str = Field(
        description="Fuel use and tyres against the projection, from fuel_by_lap, "
        "tyres_by_lap and the summary."
    )
    priorities: list[DebriefPriority] = Field(
        default_factory=list,
        max_length=5,
        description="At most five, most important first.",
    )
