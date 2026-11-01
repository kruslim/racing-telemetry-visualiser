"""The in-loop citation validator: every number an agent says must be traceable.

This is the Layer-3b coach's adversarial verify step, moved in-loop and made
deterministic. The coach could afford a second model call to fact-check a lap
report; a race engineer on the radio cannot. So instead of asking a model whether
the numbers are right, we collect every numeric value the agent was actually shown
-- the ``RaceState`` slice, the triggering event, and every tool result -- and
check the numbers in its output against that set.

A figure that does not match is not a style problem, it is an invention, and
:class:`~rtv.pitwall.framework.AgentRuntime` turns it into a grounded refusal.

Deliberately permissive in exactly one direction: a *rounded* form of a grounded
number is still grounded ("2.5 laps left" for 2.487) because rounding is reporting,
not inventing. Everything else has to match within tolerance.

Typed grounding (task A1, ``RTV_PITWALL_TYPED_GROUNDING``, on by default): every fact
carries the unit family and path it came from (``fuel.laps_remaining = 6.2 laps``), and
a figure said *with* a unit ("6 laps", "2.4 s", "P4", "28 psi", "three tenths") must match
a fact of the same family. So a tyre pressure can no longer ground "laps of fuel" just by
being numerically close. A bare figure ("2.5 a lap") keeps the looser any-fact match, and
the small integers 0-3 are only waved through when bare. Units are read by
:mod:`rtv.grounding.units`, which the Layer-3b prose check shares.

The coach is held to that prose check too (task A2): when a tool result handed it a
Layer-1 corner (``get_corner_detail`` with telemetry available), the fact set keeps the
corner, and ``validate_output(..., prose_check=True)`` runs
:func:`rtv.coaching.agent.prose.check_prose` in its live mode over everything the agent
wrote about that corner. A brake direction the corner analysis contradicts ("brake later"
where the driver already brakes late) is a :attr:`GroundingReport.contradictions` entry and
goes through the same repair and refusal path as an invented figure.

Corner names are facts too (task A4), for every agent: the ``{label, name}`` places of the
live corner map the agent was shown (state slice, event, tool results) are the only names it
can ground. A name next to a label must be that label's ("T7 Campus"), a name on its own must
be the corner the call is about (its ``corner`` field, the event's place, or a label in the
sentence), and a name of no corner it was shown ("La Source" before the map exists, or
another circuit's corner) is not traceable. These land in
:attr:`GroundingReport.contradictions` with the same repair and refusal path
(:mod:`rtv.grounding.corner_names`).

Another check is about words, not numbers: a call that says rain or a wet track
(:data:`WEATHER_CLAIM_RE`) needs a weather fact -- a ``weather_change`` event or the
race director's injected weather condition (eval rehearsals), or a wet
``track_wetness`` reading. Nothing in a live session announces rain that has not
arrived, and a falling temperature trend is not a forecast.
"""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, Field

from rtv.grounding.units import Figure, convert, field_unit, parse_unit_label, read_figures

#: A bare number, optionally signed/decimal/exponent, with thousands separators removed
#: first. The exponent matters: a model that echoes a raw ``4.66e-06`` from a tool would
#: otherwise be scored on the mantissa alone and refused for a figure it never invented.
_NUMBER_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
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

#: Words that claim rain or water on the track. Each needs a weather fact (see
#: :meth:`FactSet.has_weather`); otherwise the call is refused like an invented figure.
WEATHER_CLAIM_RE = re.compile(
    r"\b(rain\w*|showers?|drizzle|downpour|storm\w*|wet|wetter|wetness|damp)\b",
    re.IGNORECASE,
)
#: Payload keys and ``measure`` values that make a weather fact.
_WEATHER_MEASURES = frozenset({"track_wetness", "precipitation", "rain"})
_WEATHER_KEYS = frozenset({"forecast_minutes", "precipitation"})
#: iRacing's ``TrackWetness`` enum: 1 is dry, 2 and up is some water on the track.
_WET_READING = 2.0


class GroundingReport(BaseModel):
    """Verdict on one agent output."""

    ok: bool = True
    numbers: list[float] = Field(default_factory=list)
    #: Rendered as they appeared in the text, so an operator can find them.
    unsupported: list[str] = Field(default_factory=list)
    #: The same figures with their unit and why they failed, for the repair turn and logs.
    unsupported_detail: list[str] = Field(default_factory=list)
    #: Statements the Layer-1 corner analysis contradicts (the prose check), for the repair
    #: turn, the audit and the refusal. Empty unless the run held a Layer-1 corner.
    contradictions: list[str] = Field(default_factory=list)
    #: Whether figures said with a unit had to match a fact of that unit family.
    typed: bool = False
    word_count: int = 0
    over_word_limit: bool = False


class FactSet:
    """Every number the agent was shown, with where it came from.

    Sources are kept so a rejection can say *what* was available, which is the
    difference between a useful log line and "hallucination detected".
    """

    def __init__(self, typed: bool | None = None) -> None:
        #: Exactly what the agent was shown.
        self._raw: dict[float, str] = {}
        #: Raw facts plus the rounded/absolute forms that are the same fact spoken.
        self._values: dict[float, str] = {}
        #: family -> [(value, unit it is reported in, path)], for figures said with a unit.
        self._typed: dict[str, list[tuple[float, str, str]]] = {}
        self.typed = _typed_default() if typed is None else bool(typed)
        #: Layer-1 corner findings a tool returned, by label, in the shape the prose check
        #: reads (:func:`layer1_corner`).
        self.corners: dict[str, dict[str, Any]] = {}
        #: Corner label -> mapped name (or None) for every ``{label, name}`` place the agent
        #: was shown: the live corner map, event places, tool results.
        self.places: dict[str, str | None] = {}
        #: Where each map label's apex is (lap fraction), to line up Layer-1 labels.
        self.place_pct: dict[str, float] = {}
        #: The corner the triggering event happened at, when its place is on the map.
        self.event_corner: str | None = None
        #: Sources that announce rain or a wet track (see :meth:`has_weather`).
        self.weather_sources: list[str] = []

    @property
    def has_weather(self) -> bool:
        """A source says there is (or will be) water on the track: a weather claim is grounded."""
        return bool(self.weather_sources)

    def add(self, source: str, payload: Any) -> None:
        if _says_weather(payload):
            self.weather_sources.append(source)
        corner = layer1_corner(payload)
        if corner is not None:
            self.corners.setdefault(corner["label"], corner)
        for label, name, pct in _walk_places(payload):
            if name or label not in self.places:
                self.places[label] = name
            if pct is not None:
                self.place_pct.setdefault(label, pct)
        if source == "event" and isinstance(payload, dict):
            place = payload.get("place")
            if isinstance(place, dict) and not place.get("provisional") and place.get("label"):
                self.event_corner = str(place["label"])
        for value in _walk_numbers(payload):
            self._raw.setdefault(value, source)
            for variant in _variants(value):
                self._values.setdefault(variant, source)
        for value, unit, path in _walk_facts(payload, source):
            if unit is not None:
                self._typed.setdefault(unit[0], []).append((value, unit[1], path))
            elif _is_count(path, value):
                self._typed.setdefault("count", []).append((value, "n", path))

    def facts(self, family: str) -> list[tuple[float, str, str]]:
        """Every ``(value, unit, path)`` of one unit family, in the order they were added."""
        return list(self._typed.get(family, ()))

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

    def supports_figure(self, figure: Figure) -> bool:
        """A figure as spoken: with a unit it must match a fact of that unit family.

        The fact is converted into the unit the agent used (kPa to psi, m/s to km/h,
        a lap fraction to percent) before the same rounding and tolerance as
        :meth:`supports` apply, so "28 psi" for 190.3 kPa is reporting, not inventing.
        """
        if not self.typed or figure.family is None or figure.unit is None:
            return self.supports(figure.value)
        for value, unit, _path in self._typed.get(figure.family, ()):
            spoken = convert(value, figure.family, unit, figure.unit)
            for form in _rounded_forms(spoken):
                if abs(figure.value - form) <= max(ABS_TOL, REL_TOL * abs(form)):
                    return True
        if figure.family == "laps" and _spelled_out(figure):
            # "three laps running wide" for a recurring issue with occurrences: 3, or
            # "second off in two laps" for two events at the corner: on the radio a
            # spelled-out count of occurrences is said as laps. It is a count fact, so
            # it matches a count (exactly), never a laps quantity like fuel.
            return any(value == figure.value for value, _u, _p in self._typed.get("count", ()))
        return False

    def fact_values(
        self, figure: Figure, family: str | None, unit: str | None
    ) -> list[float]:
        """The facts ``figure`` stands for, expressed in ``unit`` of ``family``.

        With no family the figure is matched bare against the raw facts. A figure
        said in another family than the one asked for stands for nothing.
        """
        if family is None or not self.typed:
            return [fact for fact in self._raw if _matches(figure.value, _variants(fact))]
        if figure.family not in (None, family) or unit is None:
            return []
        out: list[float] = []
        for value, fact_unit, _path in self._typed.get(family, ()):
            spoken = convert(value, family, fact_unit, figure.unit or unit)
            if _matches(figure.value, _rounded_forms(spoken)):
                out.append(convert(value, family, fact_unit, unit))
        return out

    def supports_difference(self, figure: Figure, others: list[Figure]) -> bool:
        """``figure`` is the difference of two facts that are both said next to it.

        "130 versus 139, 8 km/h down" is arithmetic on the record, not an invention,
        but only when both terms are in the same sentence: a bare "8 km/h" could be
        the difference of any two speeds in the data. The terms must be grounded
        themselves and of the figure's unit family (or bare).
        """
        terms = [f for f in others if f is not figure and self.supports_figure(f)]
        if len(terms) < 2:
            return False
        for i, a in enumerate(terms):
            for b in terms[i + 1 :]:
                family = figure.family or a.family or b.family
                unit = figure.unit or a.unit or b.unit
                if family is not None and unit is None:
                    continue
                if family is not None and (
                    a.family not in (None, family) or b.family not in (None, family)
                ):
                    continue
                xs = self.fact_values(a, family, unit)
                ys = self.fact_values(b, family, unit)
                for x in xs:
                    for y in ys:
                        if x != y and _matches(abs(figure.value), _rounded_forms(abs(x - y))):
                            return True
        return False

    def explain(self, figure: Figure) -> str:
        """Why ``figure`` failed, in words an agent can act on in its repair turn."""
        if figure.family is None or not self.typed:
            return f"{figure.written} (no figure in the data matches)"
        unit = _UNIT_WORDS.get(figure.unit or "", figure.family)
        text = f"{figure.written} {unit} (no {figure.family} figure in the data matches"
        other_unit = self._other_unit_near(figure)
        elsewhere = self._typed_path_near(figure.value, exclude=figure.family)
        if other_unit:
            text += f"; {other_unit}"
        elif elsewhere:
            text += f"; {_render(_round(figure.value))} is {elsewhere}"
        return text + ")"

    def _other_unit_near(self, figure: Figure) -> str | None:
        """A same-family fact that is ``figure`` in another unit: "27.7 m/s, not km/h"."""
        for value, fact_unit, path in self._typed.get(figure.family or "", ()):
            if fact_unit == figure.unit or not _matches(figure.value, _rounded_forms(value)):
                continue
            spoken = convert(value, figure.family or "", fact_unit, figure.unit or fact_unit)
            return (
                f"{_render(_round(value))} is {path} in {_UNIT_WORDS.get(fact_unit, fact_unit)}"
                f", which is {_render(_round(spoken))} {_UNIT_WORDS.get(figure.unit or '', '')}"
            ).rstrip()
        return None

    def _typed_path_near(self, number: float, exclude: str | None = None) -> str | None:
        """The first typed fact of another family that is numerically ``number``."""
        for family, rows in self._typed.items():
            if family == exclude:
                continue
            for value, _unit, path in rows:
                for form in _variants(value):
                    if abs(number - form) <= max(ABS_TOL, REL_TOL * abs(form)):
                        return f"{path} ({family})"
        return None

    def source_of(self, number: float) -> str | None:
        for fact, source in self._values.items():
            if abs(number - fact) <= max(ABS_TOL, REL_TOL * abs(fact)):
                return source
        return None


def _says_weather(payload: Any, depth: int = 0) -> bool:
    """A weather_change payload, an injected weather condition, or a wet reading."""
    if depth > 8:
        return False
    if isinstance(payload, dict):
        if payload.get("event_type") == "weather_change" or payload.get("kind") == "weather":
            return True
        if payload.get("measure") in _WEATHER_MEASURES or _WEATHER_KEYS & payload.keys():
            return True
        wetness = payload.get("track_wetness")
        if _is_number(wetness) and float(wetness) >= _WET_READING:
            return True
        return any(_says_weather(v, depth + 1) for v in payload.values())
    if isinstance(payload, list | tuple):
        return any(_says_weather(v, depth + 1) for v in payload)
    return False


def weather_claims(text: str) -> list[str]:
    """The rain / wet-track words in ``text``, as written."""
    return [m.group(0) for m in WEATHER_CLAIM_RE.finditer(text or "")]


def _typed_default() -> bool:
    """``RTV_PITWALL_TYPED_GROUNDING``; on unless the settings say otherwise."""
    try:
        from rtv.config import get_settings

        return bool(get_settings().pitwall_typed_grounding)
    except Exception:  # pragma: no cover - a broken settings file must not unground
        return True


#: How a unit is written back in an explanation.
_UNIT_WORDS = {
    "laps": "laps",
    "lap": "(lap number)",
    "s": "s",
    "tenth": "tenths",
    "pos": "(position)",
    "kpa": "kPa",
    "psi": "psi",
    "bar": "bar",
    "c": "°C",
    "f": "°F",
    "kmh": "km/h",
    "mph": "mph",
    "ms": "m/s",
    "pct": "%",
    "l": "litres",
    "m": "m",
    "v": "V",
    "rpm": "rpm",
    "gear": "(gear)",
    "g": "g",
    "x": "incidents",
}


def _matches(number: float, forms: list[float]) -> bool:
    """``number`` is one of ``forms`` within the shared tolerance."""
    return any(abs(number - form) <= max(ABS_TOL, REL_TOL * abs(form)) for form in forms)


def _round(value: float) -> float:
    """A value as an operator reads it: two decimals at most."""
    return round(float(value), 2) if math.isfinite(value) else value


def _rounded_forms(value: float) -> list[float]:
    """``value`` plus the rounded, floored and unsigned forms that are still that fact."""
    if not math.isfinite(value):
        return [value]
    out: list[float] = []
    for base in (value, abs(value)):
        out.extend([base, round(base), round(base, 1), round(base, 2)])
        if abs(base) < 1e6:
            out.extend([float(math.floor(base)), float(math.ceil(base))])
    return [float(v) for v in out]


def _variants(value: float) -> list[float]:
    """A grounded number plus the forms of it that are still the same fact.

    Rounding is reporting, not inventing, and so is dropping a sign: a fuel margin
    of -6.298 laps spoken as "6.3 laps short" carries the sign in the words. The
    alternative -- rejecting every "N seconds behind" phrased off a negative gap --
    would make the validator fire on exactly the sentences an engineer says.
    """
    if not math.isfinite(value):
        return [value]
    # Lap counts are habitually spoken floored ("two laps of fuel" on 2.4).
    out = _rounded_forms(value)
    # A fraction said as a percentage ("off at 94.98 %" for lap_dist_pct 0.9498):
    # on the second Spa replay this was most of the refusals.
    if 0 < abs(value) <= 1:
        pct = abs(value) * 100.0
        out.extend([pct, round(pct), round(pct, 1), round(pct, 2), float(math.floor(pct))])
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


_MEASURED_KEYS = frozenset({"value", "threshold", "excess"})


def _walk_facts(
    payload: Any, path: str
) -> list[tuple[float, tuple[str, str] | None, str]]:
    """Every number in ``payload`` as ``(value, (family, unit) or None, path)``.

    The unit comes from the field's path (:func:`~rtv.grounding.units.field_unit`); a
    detector finding's generic ``value``/``threshold``/``excess`` take it from the
    finding's own ``unit`` or ``measure``. Numbers inside strings carry the unit written
    next to them ("Pit-lane loss 25.0s").
    """
    out: list[tuple[float, tuple[str, str] | None, str]] = []
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    if isinstance(payload, bool) or payload is None:
        return out
    if isinstance(payload, (int, float)):
        value = float(payload)
        if math.isfinite(value):
            out.append((value, field_unit(path), path))
        return out
    if isinstance(payload, str):
        for figure in read_figures(payload):
            unit = (figure.family, figure.unit) if figure.family and figure.unit else None
            out.append((figure.value, unit, f"{path}~text"))
        return out
    if isinstance(payload, dict):
        measured: tuple[str, str] | None = None
        if isinstance(payload.get("unit"), str):
            measured = parse_unit_label(payload["unit"])
        if measured is None and isinstance(payload.get("measure"), str):
            measured = field_unit(payload["measure"])
        for key, value in payload.items():
            child = f"{path}.{key}"
            if key in _MEASURED_KEYS and measured is not None and _is_number(value):
                out.append((float(value), measured, child))
                continue
            out.extend(_walk_facts(value, child))
        return out
    if isinstance(payload, (list, tuple, set)):
        for i, value in enumerate(payload):
            out.extend(_walk_facts(value, f"{path}[{i}]"))
    return out


#: Keys whose integer value counts occurrences ("occurrences": 3, a corner's "count": 2).
_COUNT_KEYS = frozenset({"occurrences", "count"})


def _is_count(path: str, value: float) -> bool:
    key = path.rsplit(".", 1)[-1]
    return key in _COUNT_KEYS and float(value).is_integer() and value >= 0


def _spelled_out(figure: Figure) -> bool:
    """Written as a word ("three"), not digits ("3")."""
    return figure.written[:1].isalpha()


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


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


#: A sentence ends at . ! ? or ; followed by space; "2.4 s" and "1:32.4" never split.
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+")


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_RE.split(text or "") if part.strip()]


def _render(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


_MAP_LABEL_RE = re.compile(r"T\d{1,2}")


def _walk_places(payload: Any) -> list[tuple[str, str | None, float | None]]:
    """Every ``{label: "T7", name: ...}`` place in ``payload`` (corner map rows, event places),
    with its lap fraction when it has one."""
    out: list[tuple[str, str | None, float | None]] = []
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, BaseModel):
            item = item.model_dump(mode="json")
        if isinstance(item, dict):
            label = item.get("label")
            if (
                isinstance(label, str)
                and _MAP_LABEL_RE.fullmatch(label)
                and "name" in item
                and (item["name"] is None or isinstance(item["name"], str))
                and not item.get("provisional")
            ):
                pct = item.get("lap_dist_pct")
                pct = float(pct) if _is_number(pct) else None
                out.append((label, item["name"] or None, pct))
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return out


def name_issues(
    spoken_text: str, detail_text: str, facts: FactSet, payload: Any | None = None
) -> list[str]:
    """Corner names in the answer that are not the corner it is about (task A4).

    Only once the agent was shown the session's corner map (any ``{label, name}`` place):
    before that there is no map to hold a name to. The corner a call is about binds what the
    driver hears (``spoken_text`` and the cue fields); the operator-facing ``detail_text`` and
    ``rationale`` may discuss other corners ("Speaker's Corner already has three calls"), so
    there a name only has to be the label it is written next to and a corner of the map.
    """
    if not facts.places:
        return []
    from rtv.coaching.agent.validation import split_corner
    from rtv.grounding.corner_names import CornerNames

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    data = payload if isinstance(payload, dict) else {}
    cited = data.get("corner")
    about = [split_corner(cited)[0] if isinstance(cited, str) and cited.strip() else None]
    about.append(facts.event_corner)
    names = CornerNames.build(facts.places)
    for label, other in _layer1_aliases(facts).items():
        names.alias(label, other)
    texts = [("spoken_text", spoken_text or "", True), ("detail_text", detail_text or "", False)]
    for key, value in data.items():
        if isinstance(value, str) and key not in _NOT_PROSE and key not in _BASE_TEXT:
            texts.append((key, value, key not in _CONTEXT_FIELDS))
    if isinstance(cited, str) and cited.strip():
        texts.append(("corner", cited, True))
    issues: list[str] = []
    for where, text, bound in texts:
        issues += [i.message for i in names.check(text, about if bound else (), where)]
    return list(dict.fromkeys(issues))


def _layer1_aliases(facts: FactSet) -> dict[str, str]:
    """Layer-1 labels a tool returned that the live map numbers differently, by position.

    Since R3 Layer 1 labels every lap pair from the session corner map, so this is empty
    unless the stored map and the live one disagree. Journals recorded before R3 carry
    ``get_corner_detail`` results numbered per lap pair (its ``T3`` could be the map's
    ``T2``); for those the map corner nearest the Layer-1 apex is the one it means.
    """
    out: dict[str, str] = {}
    if not facts.place_pct:
        return out
    for label, corner in facts.corners.items():
        pct = corner.get("lap_dist_pct")
        if not _is_number(pct):
            continue
        nearest = min(
            facts.place_pct,
            key=lambda lb: min(abs(facts.place_pct[lb] - pct), 1 - abs(facts.place_pct[lb] - pct)),
        )
        if nearest != label:
            out[label] = nearest
    return out


#: The answer's own text fields, passed separately (a flat answer dict carries them too).
_BASE_TEXT = frozenset({"spoken_text", "detail_text"})
#: Payload prose for the operator, which may discuss corners other than the call's.
_CONTEXT_FIELDS = frozenset({"rationale", "setup_note"})


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


def layer1_corner(payload: Any) -> dict[str, Any] | None:
    """The Layer-1 corner in a ``get_corner_detail`` result, as a ``CornerFinding`` dict.

    ``None`` for any other payload, and when the telemetry half is unavailable. Field
    names are mapped back to the findings' own (``min_speed_kmh`` -> ``min_main``), which
    is what :func:`rtv.coaching.agent.prose.corner_facts` reads.
    """
    if not isinstance(payload, dict):
        return None
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict) or telemetry.get("available") is not True:
        return None
    corner = telemetry.get("corner")
    if not isinstance(corner, dict) or not corner.get("label"):
        return None
    return {
        "label": str(corner["label"]),
        "distance": corner.get("distance_m"),
        "lap_dist_pct": corner.get("lap_dist_pct"),
        "min_main": corner.get("min_speed_kmh"),
        "min_ref": corner.get("ref_min_speed_kmh"),
        "net_dt": corner.get("time_delta_s"),
        "diags": [
            {
                "text": d.get("text", ""),
                # Journals recorded before diagnostics carried distance_m have none; the
                # brake direction only needs the text.
                "distance": d.get("distance_m") or 0.0,
                "time_loss": d.get("time_loss_s") or 0.0,
            }
            for d in corner.get("diagnostics") or []
            if isinstance(d, dict)
        ],
    }


#: Payload fields that are not prose: the corner label and the agents' enum fields.
_NOT_PROSE = frozenset({"corner", "theme", "confidence", "priority", "kind", "action"})


def prose_contradictions(
    spoken_text: str, detail_text: str, facts: FactSet, payload: Any | None = None
) -> list[str]:
    """What the Layer-1 corners in ``facts`` contradict in the answer (the A2 prose check).

    Runs only when the fact set holds a Layer-1 corner and the answer is about one of them
    (its ``corner`` field names it, or names no corner). An answer about another corner of
    the map, or about a provisional bucket, has nothing in the lap findings to be held to.
    """
    if not facts.corners:
        return []
    from rtv.coaching.agent.prose import check_prose  # no langgraph needed
    from rtv.coaching.agent.validation import split_corner

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    data = payload if isinstance(payload, dict) else {}
    cited = data.get("corner")
    owner = split_corner(cited)[0] if isinstance(cited, str) and cited.strip() else None
    if owner is not None and owner not in facts.corners:
        return []
    texts: list[tuple[str, str | None, str]] = [
        ("spoken_text", owner, spoken_text or ""),
        ("detail_text", owner, detail_text or ""),
    ]
    for key, value in data.items():
        if isinstance(value, str) and key not in _NOT_PROSE:
            texts.append((key, owner, value))
    report = check_prose(texts, {"corners": list(facts.corners.values())}, mode="live")
    return list(dict.fromkeys(report.issues))


def validate_output(
    spoken_text: str,
    detail_text: str,
    facts: FactSet,
    payload: Any | None = None,
    *,
    prose_check: bool = False,
) -> GroundingReport:
    """Check an agent's whole answer -- prose *and* structured payload.

    Both halves assert numbers, and a fabricated ``rejoin_position`` in the
    payload would reach the pitwall UI just as surely as one in the spoken call.
    Prose is matched tolerantly (rounding is speech); payload fields are matched
    exactly (they are meant to be copied from a tool result). ``prose_check``
    (the coach) adds :func:`prose_contradictions`.
    """
    numbers: list[float] = []
    unsupported: list[str] = []
    detail: list[str] = []
    seen: set[str] = set()
    for text in (spoken_text or "", detail_text or ""):
        for sentence in _sentences(text):
            if facts.typed:
                figures = read_figures(sentence)
            else:
                figures = [
                    Figure(value, written) for value, written in extract_numbers(sentence)
                ]
            for figure in figures:
                numbers.append(figure.value)
                if facts.supports_figure(figure):
                    continue
                if facts.supports_difference(figure, figures):
                    continue
                key = f"{figure.written}|{figure.family}"
                if key in seen:
                    continue
                seen.add(key)
                unsupported.append(figure.written)
                detail.append(facts.explain(figure))

    if not facts.has_weather:
        for text in (spoken_text or "", detail_text or ""):
            for word in weather_claims(text):
                key = f"weather|{word.lower()}"
                if key in seen:
                    continue
                seen.add(key)
                unsupported.append(word)
                detail.append(
                    f"{word} (a weather claim needs a weather fact: no weather_change event, "
                    "injected weather condition or wet track reading was shown; a "
                    "temperature trend is not a forecast)"
                )

    for field_name, value in _payload_numbers(payload):
        numbers.append(value)
        if facts.supports_exact(value):
            continue
        written = f"{field_name}={_render(value)}"
        if written in seen:
            continue
        seen.add(written)
        unsupported.append(written)
        detail.append(f"{written} (structured fields must be copied exactly from a tool result)")

    contradictions = (
        prose_contradictions(spoken_text, detail_text, facts, payload) if prose_check else []
    )
    contradictions += name_issues(spoken_text, detail_text, facts, payload)

    words = len((spoken_text or "").split())
    over = words > MAX_SPOKEN_WORDS
    return GroundingReport(
        ok=not unsupported and not contradictions and not over,
        numbers=numbers,
        unsupported=unsupported,
        unsupported_detail=detail,
        contradictions=contradictions,
        typed=facts.typed,
        word_count=words,
        over_word_limit=over,
    )
