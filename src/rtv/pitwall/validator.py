"""The in-loop citation validator: every number an agent says must be traceable.

This is the Layer-3b coach's adversarial verify step, moved in-loop and made
deterministic. The coach could afford a second Claude call to fact-check a lap
report; a race engineer on the radio cannot. So instead of asking a model whether
the numbers are right, we collect every numeric value the agent was actually shown
-- the ``RaceState`` slice, the triggering event, and every tool result -- and
check the numbers in its output against that set.

A figure that does not match is not a style problem, it is an invention, and
:class:`~rtv.pitwall.framework.AgentRuntime` turns it into a grounded refusal.

Deliberately permissive in exactly one direction: a *rounded* form of a grounded
number is still grounded ("2.5 laps left" for 2.487) because rounding is reporting,
not inventing. Everything else has to match within tolerance.
"""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, Field

#: A bare number, optionally signed/decimal, with thousands separators removed first.
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?")
#: Lap-time notation: 1:32.4 -> 92.4 seconds. Matched before bare numbers so the
#: minutes and seconds are not scored as two unrelated figures.
_CLOCK_RE = re.compile(r"(?<![\w.])(\d{1,2}):(\d{1,2}(?:\.\d+)?)")

#: Absolute and relative tolerance when matching a spoken figure to a fact.
ABS_TOL = 0.05
REL_TOL = 0.02

#: Small integers that are units of speech rather than claims ("one lap", "P1"
#: is still checked; but 0 and 1 appear in almost any sentence as articles/counts).
_ALWAYS_ALLOWED = frozenset({0.0, 1.0, 2.0, 3.0})

MAX_SPOKEN_WORDS = 25


class GroundingReport(BaseModel):
    """Verdict on one agent output."""

    ok: bool = True
    numbers: list[float] = Field(default_factory=list)
    #: Rendered as they appeared in the text, so an operator can find them.
    unsupported: list[str] = Field(default_factory=list)
    word_count: int = 0
    over_word_limit: bool = False


class FactSet:
    """Every number the agent was shown, with where it came from.

    Sources are kept so a rejection can say *what* was available, which is the
    difference between a useful log line and "hallucination detected".
    """

    def __init__(self) -> None:
        #: Exactly what the agent was shown.
        self._raw: dict[float, str] = {}
        #: Raw facts plus the rounded/absolute forms that are the same fact spoken.
        self._values: dict[float, str] = {}

    def add(self, source: str, payload: Any) -> None:
        for value in _walk_numbers(payload):
            self._raw.setdefault(value, source)
            for variant in _variants(value):
                self._values.setdefault(variant, source)

    @property
    def values(self) -> dict[float, str]:
        return dict(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def supports(self, number: float) -> bool:
        """Tolerant match, for prose. Saying "2.5 laps" for 2.487 is reporting."""
        if number in _ALWAYS_ALLOWED:
            return True
        for fact in self._values:
            if abs(number - fact) <= max(ABS_TOL, REL_TOL * abs(fact)):
                return True
        return False

    def supports_exact(self, number: float) -> bool:
        """Strict match against the *raw* facts, for structured fields.

        A ``rejoin_position`` or a ``target_lap`` is not paraphrase -- it is meant
        to be copied out of a tool result verbatim. The rounded variants are
        deliberately excluded here: a tyre pressure of 18.9 rounds to 19, and
        letting that "support" a claimed P19 is exactly the false negative that
        would make this whole check theatre.
        """
        return any(abs(number - fact) <= 1e-6 for fact in self._raw)

    def source_of(self, number: float) -> str | None:
        for fact, source in self._values.items():
            if abs(number - fact) <= max(ABS_TOL, REL_TOL * abs(fact)):
                return source
        return None


def _variants(value: float) -> list[float]:
    """A grounded number plus the forms of it that are still the same fact.

    Rounding is reporting, not inventing, and so is dropping a sign: a fuel margin
    of -6.298 laps spoken as "6.3 laps short" carries the sign in the words. The
    alternative -- rejecting every "N seconds behind" phrased off a negative gap --
    would make the validator fire on exactly the sentences an engineer says.
    """
    out: list[float] = []
    if not math.isfinite(value):
        return [value]
    for base in (value, abs(value)):
        out.extend([base, round(base), round(base, 1), round(base, 2)])
        # Lap counts are habitually spoken floored ("two laps of fuel" on 2.4).
        if abs(base) < 1e6:
            out.extend([float(math.floor(base)), float(math.ceil(base))])
    return [float(v) for v in out]


def _walk_numbers(payload: Any) -> list[float]:
    out: list[float] = []
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, (int, float)):
            value = float(item)
            if math.isfinite(value):
                out.append(value)
        elif isinstance(item, dict):
            stack.extend(item.values())
            # Keys can be numeric too (e.g. per-corner maps keyed by index).
            stack.extend(k for k in item if isinstance(k, (int, float)))
        elif isinstance(item, (list, tuple, set)):
            stack.extend(item)
        elif isinstance(item, BaseModel):
            stack.append(item.model_dump(mode="json"))
        elif isinstance(item, str):
            # Numbers embedded in a string a tool returned are still facts the
            # agent legitimately saw (e.g. an assumption line "Pit loss 25.0s").
            out.extend(n for n, _ in extract_numbers(item))
    return out


def extract_numbers(text: str) -> list[tuple[float, str]]:
    """Every figure in ``text`` as (value, as-written), clock times folded in."""
    found: list[tuple[float, str]] = []
    remaining = text
    for match in _CLOCK_RE.finditer(text):
        minutes, seconds = int(match.group(1)), float(match.group(2))
        found.append((minutes * 60.0 + seconds, match.group(0)))
        remaining = remaining.replace(match.group(0), " ", 1)
    remaining = remaining.replace(",", "")
    for match in _NUMBER_RE.finditer(remaining):
        try:
            found.append((float(match.group(0)), match.group(0)))
        except ValueError:  # pragma: no cover - regex guarantees parseability
            continue
    return found


def _render(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _payload_numbers(payload: Any, prefix: str = "") -> list[tuple[str, float]]:
    """Numeric leaves of a structured output, with dotted field names."""
    out: list[tuple[str, float]] = []
    if payload is None:
        return out
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    if isinstance(payload, dict):
        for key, value in payload.items():
            out.extend(_payload_numbers(value, f"{prefix}{key}"))
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            out.extend(_payload_numbers(value, f"{prefix}[{i}]"))
    elif isinstance(payload, bool) or isinstance(payload, str):
        return out  # strings in the payload are prose, already covered above
    elif isinstance(payload, (int, float)) and math.isfinite(float(payload)):
        out.append((prefix or "value", float(payload)))
    return out


def validate_output(
    spoken_text: str,
    detail_text: str,
    facts: FactSet,
    payload: Any | None = None,
) -> GroundingReport:
    """Check an agent's whole answer -- prose *and* structured payload.

    Both halves assert numbers, and a fabricated ``rejoin_position`` in the
    payload would reach the pitwall UI just as surely as one in the spoken call.
    Prose is matched tolerantly (rounding is speech); payload fields are matched
    exactly (they are meant to be copied from a tool result).
    """
    numbers: list[float] = []
    unsupported: list[str] = []
    seen: set[str] = set()
    for text in (spoken_text or "", detail_text or ""):
        for value, written in extract_numbers(text):
            numbers.append(value)
            if facts.supports(value):
                continue
            if written in seen:
                continue
            seen.add(written)
            unsupported.append(written)

    for field_name, value in _payload_numbers(payload):
        numbers.append(value)
        if facts.supports_exact(value):
            continue
        written = f"{field_name}={_render(value)}"
        if written in seen:
            continue
        seen.add(written)
        unsupported.append(written)

    words = len((spoken_text or "").split())
    over = words > MAX_SPOKEN_WORDS
    return GroundingReport(
        ok=not unsupported and not over,
        numbers=numbers,
        unsupported=unsupported,
        word_count=words,
        over_word_limit=over,
    )
