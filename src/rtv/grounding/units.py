"""Numbers with units, as prose writes them and as the engine reports them.

Two readers share this module:

- the Layer-3b prose check (:mod:`rtv.coaching.agent.prose`) uses the figure patterns at the
  top (metres, km/h, seconds, g, gears) exactly as it always has;
- the pitwall validator (:mod:`rtv.pitwall.validator`) uses :func:`read_figures` to find every
  number an agent says *with the unit next to it* ("6 laps", "2.4 s", "P4", "28 psi", "three
  tenths"), and :func:`field_unit` to know which unit an engine field is reported in
  (``fuel.laps_remaining`` is laps, ``tyres.pressures.LF`` is kPa, ``player.speed`` is m/s).

A unit belongs to a *family* (pressure: kPa, psi, bar). Two figures can only be the same fact
when they are in the same family; :func:`convert` moves a value between units of one family.
Nothing here calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "APPROX",
    "FAMILIES",
    "G_RE",
    "GEAR_PROSE_RE",
    "KMH_RE",
    "METRES_RE",
    "NUM",
    "ORDINALS",
    "SECONDS_RE",
    "Figure",
    "convert",
    "field_unit",
    "parse_unit_label",
    "read_figures",
]

# ------------------------------------------------------------------ the prose check's patterns
# Moved here unchanged from rtv.coaching.agent.prose, which imports them back.
APPROX = r"(?P<approx>~|≈|about |around |roughly |approx\.? |approximately |some )?"
NUM = r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
METRES_RE = re.compile(APPROX + r"[+-]?" + NUM + r"\s?(?:m|metres|meters)\b(?!/)")
KMH_RE = re.compile(APPROX + r"[+-]?" + NUM + r"\s?(?:km/h|kph|kmh)\b")
SECONDS_RE = re.compile(APPROX + r"[+-]?" + NUM + r"\s?(?:s|sec|secs|seconds)\b")
G_RE = re.compile(APPROX + r"(?P<num>\d\.\d+)\s?g\b")
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}
GEAR_PROSE_RE = re.compile(
    r"\bgear\s+(?P<a>\d)\b|\b(?P<b>\d)(?:st|nd|rd|th)?\s+gear\b"
    r"|\b(?P<c>first|second|third|fourth|fifth|sixth)\s+gear\b",
    re.IGNORECASE,
)

# ------------------------------------------------------------------ unit families
#: family -> unit -> (scale, offset) into the family's first (canonical) unit:
#: ``canonical = value * scale + offset``.
FAMILIES: dict[str, dict[str, tuple[float, float]]] = {
    "laps": {"laps": (1.0, 0.0)},  # a count of laps: "6 laps of fuel"
    "lap": {"lap": (1.0, 0.0)},  # a lap number: "box on lap 12", "L12"
    "s": {"s": (1.0, 0.0), "tenth": (0.1, 0.0)},
    "position": {"pos": (1.0, 0.0)},  # "P4", "4th place", "lose 2 places"
    "pressure": {"kpa": (1.0, 0.0), "psi": (6.894757, 0.0), "bar": (100.0, 0.0)},
    "temp": {"c": (1.0, 0.0), "f": (5.0 / 9.0, -160.0 / 9.0)},
    "speed": {"kmh": (1.0, 0.0), "mph": (1.609344, 0.0), "ms": (3.6, 0.0)},
    "percent": {"pct": (1.0, 0.0), "fraction": (100.0, 0.0)},
    "litres": {"l": (1.0, 0.0)},
    "metres": {"m": (1.0, 0.0)},
    "volts": {"v": (1.0, 0.0)},
    "rpm": {"rpm": (1.0, 0.0)},
    "gear": {"gear": (1.0, 0.0)},
    "g": {"g": (1.0, 0.0)},
    "incidents": {"x": (1.0, 0.0)},
}


def convert(value: float, family: str, unit: str, to_unit: str) -> float:
    """``value`` in ``unit`` expressed in ``to_unit`` (both of ``family``)."""
    if unit == to_unit:
        return value
    units = FAMILIES[family]
    scale, offset = units[unit]
    canonical = value * scale + offset
    to_scale, to_offset = units[to_unit]
    return (canonical - to_offset) / to_scale


# ------------------------------------------------------------------ engine fields
_TYRE_CORNERS = frozenset({"lf", "rf", "lr", "rr", "front", "rear", "left", "right"})
_LAP_NUMBER_KEYS = frozenset({"lap", "current_lap", "target_lap", "stop_lap"})
_LAPS_KEYS = frozenset({"every", "laps_completed", "stint_laps", "laps_on_tyres"})
_LITRE_KEYS = frozenset(
    {"per_lap", "per_lap_std", "fuel_per_lap", "level", "capacity", "fuel", "fuel_to_finish"}
)
_POSITION_KEYS = frozenset(
    {"position", "class_position", "running_order", "field_size", "positions_lost"}
)
_SECONDS_KEYS = frozenset(
    {"gap", "gap_ahead", "gap_behind", "gap_to_player", "gap_to_car_ahead_after"}
)
_SPEED_MS_KEYS = frozenset({"speed", "mean_speed", "wheel_speed"})
_TEMP_KEYS = frozenset(
    {
        "temps",
        "temp_trend",
        "oil_temp",
        "water_temp",
        "air_temp",
        "track_temp",
        "air_temp_trend",
        "track_temp_trend",
        "tyre_temp",
        "tyre_temp_trend",
        "tyre_axle_imbalance",
        "temp_change_since_stint_start",
        "temp_change_vs_previous_lap",
    }
)
_PRESSURE_KPA_KEYS = frozenset(
    {
        "pressures",
        "pressure_trend",
        "tyre_pressure",
        "pressure_change_since_stint_start",
        "pressure_change_vs_previous_lap",
    }
)
_PRESSURE_BAR_KEYS = frozenset({"oil_pressure", "fuel_pressure"})
_PERCENT_KEYS = frozenset({"slip", "mean_slip", "worst_slip", "brake"})
_SEGMENT_RE = re.compile(r"\[[^\]]*\]")


def field_unit(path: str) -> tuple[str, str] | None:
    """The (family, unit) an engine field at ``path`` is reported in, or ``None``.

    ``path`` is dotted (``race_state.fuel.laps_remaining``, ``event.temps_c.LF``,
    ``get_standings_around_player.cars[2].gap_to_player_s``); the last meaningful key decides.
    ``None`` means "a number with no unit we can vouch for" (an index, a count of samples, a
    raw flag word): it can still ground a bare number, never one said with a unit.
    """
    segments = [s for s in _SEGMENT_RE.sub("", path).lower().split(".") if s]
    while segments and (segments[-1] in _TYRE_CORNERS or segments[-1].isdigit()):
        segments.pop()
    if not segments:
        return None
    key = segments[-1]
    if key.endswith("_kmh"):
        return ("speed", "kmh")
    if key.endswith("_kpa"):
        return ("pressure", "kpa")
    if key.endswith("_psi"):
        return ("pressure", "psi")
    if key.endswith("_c") or "_c_per_" in key:
        return ("temp", "c")
    if key.endswith("_s") or key.endswith("_s_per_s"):
        return ("s", "s")
    if key.endswith("_l"):
        return ("litres", "l")
    if key.endswith("_m"):
        return ("metres", "m")
    if key.endswith("_pct") or key in _PERCENT_KEYS:
        return ("percent", "fraction")
    if key in _LITRE_KEYS:
        return ("litres", "l")
    if key in _LAP_NUMBER_KEYS or (key.endswith("_lap") and not key.endswith("per_lap")):
        return ("lap", "lap")
    if key == "laps":
        # A list of laps is lap numbers ("laps": [1, 2, 4]); a lone figure is a count.
        return ("lap", "lap") if path.rstrip().endswith("]") else ("laps", "laps")
    if key.startswith("laps_") or key.endswith("_laps") or key in _LAPS_KEYS:
        return ("laps", "laps")
    if key in _POSITION_KEYS or key.endswith("_position") or key.startswith("position_"):
        return ("position", "pos")
    if key in _SECONDS_KEYS or key.endswith("_time") or key.startswith("time_"):
        return ("s", "s")
    if key in _SPEED_MS_KEYS:
        return ("speed", "ms")
    if key in _TEMP_KEYS:
        return ("temp", "c")
    if key in _PRESSURE_KPA_KEYS:
        return ("pressure", "kpa")
    if key in _PRESSURE_BAR_KEYS:
        return ("pressure", "bar")
    if key == "voltage":
        return ("volts", "v")
    if key == "rpm":
        return ("rpm", "rpm")
    if key == "gear":
        return ("gear", "gear")
    if key == "incidents":
        return ("incidents", "x")
    return None


_UNIT_LABELS: dict[str, tuple[str, str]] = {
    "c": ("temp", "c"),
    "°c": ("temp", "c"),
    "f": ("temp", "f"),
    "kpa": ("pressure", "kpa"),
    "psi": ("pressure", "psi"),
    "bar": ("pressure", "bar"),
    "s": ("s", "s"),
    "v": ("volts", "v"),
    "l": ("litres", "l"),
    "m": ("metres", "m"),
    "km/h": ("speed", "kmh"),
    "m/s": ("speed", "ms"),
    "%": ("percent", "pct"),
    "laps": ("laps", "laps"),
    "rpm": ("rpm", "rpm"),
}


def parse_unit_label(label: str) -> tuple[str, str] | None:
    """A detector payload's ``"unit"`` string (``"C"``, ``"kPa/lap"``) as (family, unit)."""
    head = label.strip().lower().split("/lap")[0].strip()
    return _UNIT_LABELS.get(head)


# ------------------------------------------------------------------ reading figures from prose
_WORD_VALUES: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_ORDINAL_WORDS: dict[str, int] = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
    "twentieth": 20,
}
_WORD = "|".join(sorted(_WORD_VALUES, key=len, reverse=True))
_DIGIT_WORD = "zero|one|two|three|four|five|six|seven|eight|nine"
_TENS_WORD = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_ORD_WORD = "|".join(sorted(_ORDINAL_WORDS, key=len, reverse=True))
_HALF = r"(?P<half>\s+and\s+a\s+half)?"

#: One token per figure, tried in this order at each position.
_TOKEN_RE = re.compile(
    # "a lap and a half", "a second and a half", "a tenth and a half"
    r"(?P<a_half>\ban?\s+(?=(?:lap|second|tenth)\s+and\s+a\s+half\b))"
    # "half a lap", "half a second"
    r"|(?P<half_a>\bhalf\s+an?\s+)"
    # lap-time notation 1:32.4
    r"|(?<![\w.:])(?P<clock_m>\d{1,2}):(?P<clock_s>\d{1,2}(?:\.\d+)?)(?![\d:])"
    # P4, P 12 (position) and L12 (lap number)
    r"|\b(?P<pfx>[PL])\s?(?P<pfx_num>\d{1,3})\b"
    # "fourth place", "second gear"
    r"|\b(?P<ord_word>" + _ORD_WORD + r")\s+(?P<ord_unit>place|position|gear)\b"
    # digits, optionally signed / decimal / exponent, and "2 and a half"
    r"|(?<![\w.])(?P<digits>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?P<ordsfx>(?:st|nd|rd|th)\b)?"
    + _HALF
    # spelled out: "six", "seventy-two", "two point four", "two and a half"
    + r"|\b(?P<word>(?:(?:" + _TENS_WORD + r")[\s-](?:"
    + _DIGIT_WORD
    + r")\b|"
    + _WORD
    + r")"
    r"(?:\s+point(?:\s+(?:" + _DIGIT_WORD + r"))+)?)\b" + _HALF.replace("half>", "whalf>"),
    re.IGNORECASE,
)

_FILLER = r"(?:(?:more|full|extra|clear|clean|green|whole|racing)\s+)?"
#: Units written after a figure. Single letters are case-sensitive (C, V, L, g, x); words are not.
_SUFFIX_RE = re.compile(
    r"\s?" + _FILLER + r"(?:"
    r"(?P<laps>(?i:laps?)\b)"
    r"|(?P<tenth>(?i:tenths?)\b)"
    r"|(?P<s>(?i:seconds?|secs?)\b|s\b)"
    r"|(?P<kmh>(?i:km/h|kph|kmh)\b)"
    r"|(?P<mph>(?i:mph)\b)"
    r"|(?P<ms>m/s\b|(?i:metres|meters)\s+(?i:per|a)\s+(?i:second)\b)"
    r"|(?P<psi>(?i:psi)\b)"
    r"|(?P<kpa>(?i:kpa)\b)"
    r"|(?P<bar>(?i:bar)\b)"
    r"|(?P<f>°\s?F\b|(?i:degrees?\s+fahrenheit)\b)"
    r"|(?P<c>°\s?C?|(?i:degrees?)(?:\s+(?i:celsius|centigrade)|\s?C)?\b|C\b)"
    r"|(?P<pct>%|(?i:per\s?cent)\b)"
    r"|(?P<l>(?i:litres?|liters?|ltrs?)\b|[lL]\b)"
    r"|(?P<m>(?i:metres?|meters?)\b|m\b(?!/))"
    r"|(?P<v>(?i:volts?)\b|V\b)"
    r"|(?P<rpm>(?i:rpm)\b)"
    r"|(?P<gear>(?i:gear)\b)"
    r"|(?P<g>g\b)"
    r"|(?P<x>x\b|(?i:incidents?)\b)"
    r"|(?P<pos>(?i:places?|positions?|spots?)\b)"
    r")"
)
_SUFFIX_FAMILY = {
    "laps": ("laps", "laps"),
    "tenth": ("s", "tenth"),
    "s": ("s", "s"),
    "kmh": ("speed", "kmh"),
    "mph": ("speed", "mph"),
    "ms": ("speed", "ms"),
    "psi": ("pressure", "psi"),
    "kpa": ("pressure", "kpa"),
    "bar": ("pressure", "bar"),
    "f": ("temp", "f"),
    "c": ("temp", "c"),
    "pct": ("percent", "pct"),
    "l": ("litres", "l"),
    "m": ("metres", "m"),
    "v": ("volts", "v"),
    "rpm": ("rpm", "rpm"),
    "gear": ("gear", "gear"),
    "g": ("g", "g"),
    "x": ("incidents", "x"),
    "pos": ("position", "pos"),
}
#: Units written before a figure: "lap 12", "position 4", "gear 3".
_PREFIX_RE = re.compile(r"\b(?P<w>lap|position|pos|gear)\s?$", re.IGNORECASE)
_PREFIX_FAMILY = {
    "lap": ("lap", "lap"),
    "position": ("position", "pos"),
    "pos": ("position", "pos"),
    "gear": ("gear", "gear"),
}
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


@dataclass(frozen=True)
class Figure:
    """One number read out of prose."""

    value: float
    #: The figure as written, without its unit ("6", "1:32.4", "six", "P4").
    written: str
    #: The unit family, or ``None`` for a bare number ("2.5 a lap").
    family: str | None = None
    unit: str | None = None

    def describe(self) -> str:
        """The figure with its unit, for a log line or a repair prompt."""
        if self.family is None:
            return self.written
        return f"{self.written} ({self.family})"


def _word_value(text: str) -> float:
    words = re.split(r"[\s-]+", text.lower())
    if "point" in words:
        i = words.index("point")
        whole = _word_value(" ".join(words[:i]))
        frac = "".join(str(_WORD_VALUES.get(w, 0) % 10) for w in words[i + 1 :])
        return float(f"{int(whole)}.{frac}") if frac else whole
    total = 0.0
    for w in words:
        total += _WORD_VALUES.get(w, 0)
    return total


def read_figures(text: str) -> list[Figure]:
    """Every figure in ``text``, with the unit written next to it when there is one.

    Read in order, never twice: "1:32.4" is one lap time, "P4" one position, "a lap and a
    half" one count of 1.5 laps. "A couple" is not a number and is not read.
    """
    text = _THOUSANDS_RE.sub("", text or "")
    out: list[Figure] = []
    for m in _TOKEN_RE.finditer(text):
        family: tuple[str, str] | None = None
        if m.group("a_half") is not None:
            value, written = 1.5, "a ... and a half"
            after = text[m.end() :]
            unit_word = re.match(r"(lap|second|tenth)\s+and\s+a\s+half", after, re.IGNORECASE)
            name = unit_word.group(1).lower() if unit_word else "lap"
            family = {"lap": ("laps", "laps"), "second": ("s", "s"), "tenth": ("s", "tenth")}[name]
            written = f"a {name} and a half"
            out.append(Figure(value, written, *family))
            continue
        if m.group("half_a") is not None:
            value, written, end = 0.5, "half", m.end()
            sfx = _SUFFIX_RE.match(text, end)
            if sfx is None or sfx.group(0).strip() == "":
                continue  # "half a dozen", "half a chance": not a figure we can check
            name = next(k for k, v in sfx.groupdict().items() if v is not None)
            out.append(Figure(value, written, *_SUFFIX_FAMILY[name]))
            continue
        if m.group("clock_m") is not None:
            value = int(m.group("clock_m")) * 60.0 + float(m.group("clock_s"))
            out.append(Figure(value, m.group(0), "s", "s"))
            continue
        if m.group("pfx") is not None:
            family = ("position", "pos") if m.group("pfx").upper() == "P" else ("lap", "lap")
            out.append(Figure(float(m.group("pfx_num")), m.group(0), *family))
            continue
        if m.group("ord_word") is not None:
            value = float(_ORDINAL_WORDS[m.group("ord_word").lower()])
            unit = m.group("ord_unit").lower()
            family = ("gear", "gear") if unit == "gear" else ("position", "pos")
            out.append(Figure(value, m.group(0), *family))
            continue

        if m.group("digits") is not None:
            written = m.group("digits")
            try:
                value = float(written)
            except ValueError:  # pragma: no cover - the pattern guarantees a float
                continue
            if m.group("half"):
                value += 0.5 if value >= 0 else -0.5
                written += " and a half"
            ordinal = m.group("ordsfx")
            if ordinal:
                written += ordinal
        else:
            written = m.group("word")
            value = _word_value(written)
            if m.group("whalf"):
                value += 0.5
                written += " and a half"
            ordinal = None

        sfx = _SUFFIX_RE.match(text, m.end())
        if sfx is not None and sfx.group(0).strip():
            name = next(k for k, v in sfx.groupdict().items() if v is not None)
            family = _SUFFIX_FAMILY[name]
            if ordinal and family[0] not in ("position", "gear"):
                family = None  # "the 3rd lap", "4th stint": an ordinal, not a measurement
        elif ordinal:
            family = None
        if family is None and not ordinal:
            pre = _PREFIX_RE.search(text, 0, m.start())
            if pre is not None:
                family = _PREFIX_FAMILY[pre.group("w").lower()]
        if family is None:
            out.append(Figure(value, written))
        else:
            out.append(Figure(value, written, *family))
    return out
