"""The scenario-script schema: what a scripted race is allowed to say.

A script is a list of :class:`ScriptedInjection` entries. Each one names a race
event to inject, the conditions under which it fires, and the payload it carries.
Nothing here schedules anything -- see :mod:`rtv.director.engine` for why the
scheduler is deliberately absent.

Design rules, all inherited from the rest of the pitwall:

* **Injections produce ordinary events.** The type is a real
  :class:`~rtv.racestate.models.EventType`, so every downstream consumer -- agent
  triggers, ``/ws/pitwall``, the ring buffer, the UI -- already handles it.
* **Conditions are conjunctive and explicit.** Every field on
  :class:`InjectionTrigger` that is set must hold. A script with no condition at
  all is rejected rather than fired immediately: "when?" is the whole point of a
  timed injection, and a silent default of "now" would be a guess.
* **Payload keys are the detector's keys.** A scripted ``flag_change`` carries
  ``from`` / ``to`` / ``active`` because that is what the real one carries. The
  script is not a second vocabulary.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from rtv.racestate.models import EventType, FlagPhase, Severity


class InjectionKind(StrEnum):
    """What sort of intervention an entry represents.

    Purely descriptive: it groups a script for a human reading it and lets a UI
    label "the director made it rain" differently from "the director threw a
    yellow". The behaviour comes from :attr:`ScriptedInjection.event_type`.
    """

    FLAG = "flag"
    WEATHER = "weather"
    REGULATION = "regulation"
    HAZARD = "hazard"
    CUSTOM = "custom"


class InjectionTrigger(BaseModel):
    """When one injection fires. Every field that is set must hold (AND).

    At least one condition is required. Combining them is how a script says
    something a single clock cannot: ``at_lap: 7`` plus ``at_lap_dist_pct: 0.30``
    is "as they come through the first sector on lap 7", and ``after`` plus
    ``delay_s`` is "sixty seconds after the yellow", whenever that turned out
    to be.
    """

    at_session_time: float | None = Field(
        default=None,
        ge=0.0,
        description="Fire once the session clock has passed this many seconds.",
    )
    at_lap: int | None = Field(
        default=None,
        ge=0,
        description="Fire on this lap number (the player's lap counter).",
    )
    at_lap_dist_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Fire once the player is at or past this fraction of a lap. "
        "Combine with at_lap to name a point on the track rather than a moment.",
    )
    after: str | None = Field(
        default=None,
        description="The id of another injection that must have fired first.",
    )
    delay_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Seconds to wait after the 'after' entry fired. Session "
        "seconds, not wall clock, so a 4x replay behaves like the live race.",
    )
    while_flag: FlagPhase | None = Field(
        default=None,
        description="Only fire while the race is in this flag phase. A forced "
        "pit regulation under a red flag is not a rehearsal, it is noise.",
    )
    once: bool = Field(
        default=True,
        description="False lets an entry fire every time its conditions hold "
        "again -- a weather step that repeats, for instance.",
    )

    @model_validator(mode="after")
    def _needs_a_condition(self) -> InjectionTrigger:
        if not any(
            (
                self.at_session_time is not None,
                self.at_lap is not None,
                self.at_lap_dist_pct is not None,
                self.after is not None,
            )
        ):
            raise ValueError(
                "An injection trigger needs at least one of at_session_time, "
                "at_lap, at_lap_dist_pct or after. 'while_flag' and 'once' "
                "constrain a condition; they are not one."
            )
        if self.delay_s and self.after is None:
            raise ValueError("delay_s is measured from 'after', so it needs one.")
        return self

    def describe(self) -> str:
        """A one-line human rendering, for logs and the status payload."""
        parts: list[str] = []
        if self.at_lap is not None:
            parts.append(f"lap {self.at_lap}")
        if self.at_lap_dist_pct is not None:
            parts.append(f"{self.at_lap_dist_pct:.0%} of a lap")
        if self.at_session_time is not None:
            parts.append(f"t>={self.at_session_time:g}s")
        if self.after is not None:
            parts.append(
                f"after {self.after}" + (f"+{self.delay_s:g}s" if self.delay_s else "")
            )
        if self.while_flag is not None:
            parts.append(f"while {self.while_flag.value}")
        return ", ".join(parts) or "never"


class ScriptedInjection(BaseModel):
    """One intervention: what to inject, when, and what it says."""

    id: str = Field(
        min_length=1,
        max_length=64,
        description="Unique within the script. Other entries reference it via "
        "'after', and it is echoed into the injected event's payload.",
    )
    kind: InjectionKind = Field(
        default=InjectionKind.CUSTOM,
        description="flag | weather | regulation | hazard | custom. Descriptive "
        "grouping only; the behaviour comes from event_type.",
    )
    event_type: EventType = Field(
        description="The race event this injection produces. A real member of "
        "the deterministic event catalog, so nothing downstream needs a branch."
    )
    severity: Severity = Field(
        default=Severity.INFO,
        description="Matches what the detector would have used: a full-course "
        "yellow is critical, a weather step is advisory.",
    )
    when: InjectionTrigger = Field(description="The firing conditions.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="The event payload, using the same keys the real detector "
        "emits (a flag_change carries from/to/active). The director adds its own "
        "provenance keys on top; it never overwrites these.",
    )
    note: str = Field(
        default="",
        max_length=500,
        description="Why this entry exists. Free text, for whoever reads the script.",
    )

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "when": self.when.describe(),
            "note": self.note,
        }


class ScenarioScript(BaseModel):
    """A named, ordered set of interventions -- one rehearsable race.

    Load one with :meth:`load`; validation is the point of this class, so a
    malformed script fails at load rather than three laps into a demo.
    """

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    version: int = Field(default=1, ge=1, description="Script schema version.")
    injections: list[ScriptedInjection] = Field(
        default_factory=list, description="The interventions, in reading order."
    )

    @model_validator(mode="after")
    def _ids_are_unique_and_references_resolve(self) -> ScenarioScript:
        seen: set[str] = set()
        for injection in self.injections:
            if injection.id in seen:
                raise ValueError(f"Duplicate injection id {injection.id!r}.")
            seen.add(injection.id)
        for injection in self.injections:
            ref = injection.when.after
            if ref is None:
                continue
            if ref == injection.id:
                raise ValueError(f"Injection {injection.id!r} waits on itself.")
            if ref not in seen:
                raise ValueError(
                    f"Injection {injection.id!r} waits on {ref!r}, which is not "
                    "in this script."
                )
        return self

    # ---- loading ---------------------------------------------------------
    @classmethod
    def from_json(cls, text: str | bytes) -> ScenarioScript:
        return cls.model_validate(json.loads(text))

    @classmethod
    def load(cls, path: str | Path) -> ScenarioScript:
        """Read and validate a script from a JSON file."""
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    # ---- lookup ----------------------------------------------------------
    def injection(self, injection_id: str) -> ScriptedInjection:
        for injection in self.injections:
            if injection.id == injection_id:
                return injection
        raise KeyError(f"No injection {injection_id!r} in script {self.name!r}.")

    def of_kind(self, kind: InjectionKind | str) -> list[ScriptedInjection]:
        return [i for i in self.injections if i.kind == kind]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "injections": [i.describe() for i in self.injections],
        }
