"""Corner names as checkable facts: a name in prose must be the corner being discussed.

A coach that says "brake earlier into La Source" about T7 has put the driver at the wrong end
of the lap, and no figure check catches it: the numbers can all be T7's. This module reads the
circuit names out of prose (plain string matching, no model) and checks them against the
session's own corner map:

- **a name next to a label** ("T7 Campus", "T7 (Campus)", "T7 - Campus") must be the name the
  map gives that label. A label the map leaves unnamed (the trace-numbered venues: Fuji,
  Hungaroring, Okayama, Barcelona, Road Atlanta have few or no mapped names) takes no name at
  all, so "T3 Dunlop" there is invented. Corner-type words ("T8 Hairpin", "T2 entry") and
  ordinary sentence words ("T7 Brake earlier") are not names;
- **a name on its own** ("Speaker's Corner again") is resolved through the map to the corner
  that owns it. When the text is about a corner (its owner, or the labels it names), the name
  must belong to one of them. A name the map places on no detected corner (a straight such as
  Kemmel, a flat-out curve such as Eau Rouge) is only checked to belong to this circuit;
- **another circuit's name** ("Copse" at Spa) is not a corner of this session. Only names
  that read as names count here: generic words that several circuits use ("the Esses",
  "Hairpin", "Turn 1", "Club") are not treated as another venue's corner.

The vocabulary is the pre-loaded venue reference (:mod:`rtv.tracks.reference`, OpenStreetMap
names for 33 venues). Two readers share it: the Layer-3b prose check
(:func:`rtv.coaching.agent.prose.check_prose`) on all three coaches' lap prose, with the
named places the Layer-1 findings carry; and the pitwall validator
(:func:`rtv.pitwall.validator.validate_output`) on every agent's call, with the
``{label, name}`` places of the live corner map it was shown.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any

__all__ = [
    "CornerNames",
    "NameIssue",
    "fold",
    "name_key",
    "venue_names",
]


def fold(text: str) -> str:
    """Accents dropped, curly quotes straightened; one character out per character in."""
    out: list[str] = []
    for ch in text or "":
        if ch in "‘’ʼ`´":
            out.append("'")
            continue
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append(base[:1] if base else ch)
    return "".join(out)


def name_key(name: str | None) -> str:
    """A corner name folded for comparison: case, accents and punctuation do not count."""
    text = fold(name or "").casefold().replace("'", "")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


#: Words a corner name may be made of without being anyone's name in particular. A name made
#: only of these ("The Esses", "Hairpin", "Turn 1", "Main straight", "Club") is not taken as
#: another circuit's corner, and at the session's own circuit it is matched only capitalised.
_GENERIC = frozenset(
    """
    the a of de la le du des del della di turn turns corner corners curve curves bend bends
    hairpin chicane straight main first second third last final inner outer loop kink esses
    esse s left right lefthander righthander fast slow medium big up down hill south north
    east west hook tower club village vale abbey sunset spiral snake horseshoe carousel dogleg
    bypass chase dipper cutting chute boot toe heel spoon hollywood ninety quartermile cafe
    gate kurve nascar mg az nissan honda mclaren porsche ford bridge pit lane complex
    """.split()
)

#: Capitalised words that start an ordinary sentence or clause right after a label
#: ("T7 Brake earlier", "T4 You're losing it"): not a name next to the label.
_NOT_A_NAME = frozenset(
    """
    a an and as at be both brake braking but by carry commit copy costs do don't dont down
    entry exit apex for from gains get go good got great had has hold i i'm if in into is it
    it's its keep let lift look loses lost min minimum need needs next nice no not now of ok
    okay on or out pick power push ref reference release remains same sector so speed stay
    still take than that that's the then there they this through throttle to trail try turn
    up use was watch we we're well what when where while with without you you're your
    yellow green box lap laps lock lockup lock-up again here last main mid early late
    earlier later understeer oversteer slow medium fast hairpin corner bend chicane kink
    left right """.split()
)

_LABEL_RE = re.compile(r"\b(?:T|Turn\s)(\d{1,2})\b")
#: What may sit between a label and the name written next to it.
_SEP_RE = re.compile(r"[ \t]*(?:\(|[-–—:])?[ \t]*|[ \t]+")
#: A capitalised word sequence, as a name next to a label is written ("Les Combes").
_CAP_WORD = r"[A-Z][A-Za-z'\-\.]*"
_CONNECTOR = r"(?:de|du|la|le|des|del|della|di|d'|of|the)"
_CANDIDATE_RE = re.compile(rf"{_CAP_WORD}(?:[ ](?:{_CONNECTOR}[ ])?{_CAP_WORD}){{0,3}}")


_TURN_N = re.compile(r"turn \d+")


def _generic(name: str) -> bool:
    return all(w in _GENERIC or w.isdigit() for w in name_key(name).split())


@dataclass(frozen=True)
class VenueName:
    name: str
    kind: str  # "corner" | "straight"
    venues: frozenset[str]


@cache
def venue_names() -> dict[str, VenueName]:
    """Every name in the venue reference, by :func:`name_key`, with the venues that use it."""
    from rtv.tracks.reference import load_all

    found: dict[str, tuple[str, str, set[str]]] = {}
    for key, ref in load_all().items():
        for seg in ref.segments:
            k = name_key(seg.name)
            if not k:
                continue
            entry = found.setdefault(k, (seg.name, seg.kind, set()))
            entry[2].add(key)
    return {k: VenueName(n, kind, frozenset(v)) for k, (n, kind, v) in found.items()}


@cache
def _venue_sets() -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = {}
    for k, vn in venue_names().items():
        for v in vn.venues:
            out.setdefault(v, set()).add(k)
    return {v: frozenset(s) for v, s in out.items()}


def detect_venue(names: Iterable[str]) -> str | None:
    """The one venue whose reference has every one of ``names``, or ``None``."""
    keys = {name_key(n) for n in names if n}
    keys.discard("")
    if not keys:
        return None
    hits = [v for v, s in _venue_sets().items() if keys <= s]
    return hits[0] if len(hits) == 1 else None


def _pattern(name: str) -> str:
    """A regex for ``name`` in folded prose: apostrophes optional, spaces or hyphens."""
    parts = re.split(r"[\s\-]+", fold(name).strip())
    words = [re.escape(p).replace("'", "'?") for p in parts if p]
    return r"[\s\-]+".join(words)


@dataclass
class NameIssue:
    message: str
    corner: str  # the label the issue is about, or the name when there is none


@dataclass
class _Known:
    name: str  # as the venue writes it
    label: str | None  # the session corner that owns it, if any
    kind: str
    here: bool  # a name of this session's circuit


@dataclass
class CornerNames:
    """The session's corner names as facts: label -> name, and every name -> its corner."""

    labels: dict[str, str | None] = field(default_factory=dict)
    venue: str | None = None
    #: name_key -> owning label, for names of this circuit placed on the lap.
    owners: dict[str, str | None] = field(default_factory=dict)
    #: label -> other map labels it may also stand for. Layer 1 numbers corners per lap
    #: pair, so a ``T3`` from another lap pair (a tool result, a second retrieved pair) can be
    #: the map's ``T2``; a name is then held to either. Filled by :meth:`alias`.
    aliases: dict[str, set[str]] = field(default_factory=dict)
    _compiled: tuple[re.Pattern[str] | None, dict[str, _Known]] | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def build(
        cls,
        labels: Mapping[str, str | None],
        places: Iterable[Mapping[str, Any]] = (),
        venue: str | None = None,
    ) -> CornerNames:
        """From the label -> name map, plus named places ``{name, corner}`` on the lap."""
        labels = {str(k): (v or None) for k, v in labels.items()}
        owners: dict[str, str | None] = {}
        for p in places:
            k = name_key(p.get("name"))
            if k:
                owners.setdefault(k, p.get("corner"))
        for label, name in labels.items():
            if name:
                owners[name_key(name)] = label
        if venue is None:
            venue = detect_venue(
                [n for n in labels.values() if n]
                + [str(p.get("name")) for p in places if p.get("name")]
            )
        return cls(labels=labels, venue=venue, owners=owners)

    @classmethod
    def from_findings(cls, findings: Mapping[str, Any]) -> CornerNames:
        """From a Layer-1 findings dict (``corners`` with ``name``, optional ``places``).

        A merged dict (the graph coach's several lap pairs) can hold one label twice, each
        pair numbering its own lap: the first one names the label, and a later one with
        another name makes the label an alias of the corner that name belongs to.
        """
        labels: dict[str, str | None] = {}
        later: list[tuple[str, str]] = []
        for c in findings.get("corners", []):
            if "label" not in c:
                continue
            if c["label"] not in labels:
                labels[c["label"]] = c.get("name")
            elif c.get("name") and name_key(c["name"]) != name_key(labels[c["label"]]):
                later.append((c["label"], c["name"]))
        names = cls.build(labels, findings.get("places") or (), findings.get("venue"))
        for label, name in later:
            owner = names.owners.get(name_key(name))
            if owner:
                names.alias(label, owner)
        return names

    def alias(self, label: str, other: str) -> None:
        """``label`` may also mean the map's ``other`` (another lap pair's numbering)."""
        if label != other:
            self.aliases.setdefault(label, set()).add(other)
            self._compiled = None

    def _also(self, label: str) -> set[str]:
        return {label} | self.aliases.get(label, set())

    def _knows(self, label: str) -> bool:
        return label in self.labels or label in self.aliases

    # ---- the vocabulary for one session --------------------------------------------------
    def _known(self) -> dict[str, _Known]:
        vocab = venue_names()
        out: dict[str, _Known] = {}
        for k, vn in vocab.items():
            if _TURN_N.fullmatch(k):
                continue  # "Turn 5" is read as a label, never as a name
            here = self.venue in vn.venues if self.venue else k in self.owners
            if not here and _generic(vn.name):
                continue
            out[k] = _Known(vn.name, self.owners.get(k), vn.kind, here)
        for k, label in self.owners.items():  # a session name the reference lacks
            if k not in out:
                name = self.labels.get(label or "") or k
                out[k] = _Known(name, label, "corner", True)
        return out

    def _matcher(self) -> tuple[re.Pattern[str] | None, dict[str, _Known]]:
        if self._compiled is None:
            self._compiled = self._compile()
        return self._compiled

    def _compile(self) -> tuple[re.Pattern[str] | None, dict[str, _Known]]:
        known = self._known()
        if not known:
            return None, known
        alts = sorted(known, key=lambda k: -len(known[k].name))
        groups = [f"(?P<n{i}>{_pattern(known[k].name)})" for i, k in enumerate(alts)]
        pattern = re.compile(r"(?<![\w'])(?:" + "|".join(groups) + r")(?![\w'])", re.IGNORECASE)
        index = {f"n{i}": known[k] for i, k in enumerate(alts)}
        return pattern, index

    # ---- the check -----------------------------------------------------------------------
    def check(
        self, text: str, about: Iterable[str | None] = (), where: str = "text"
    ) -> list[NameIssue]:
        """Every corner name in ``text`` that is not the corner it is said about.

        ``about`` is the corner the text belongs to (an advice's, a call's ``corner`` field,
        an event's place); a sentence's own labels are added to it sentence by sentence.
        """
        issues: list[NameIssue] = []
        pattern, index = self._matcher()
        owners = {a for a in about if a}
        for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text or ""):
            if sentence.strip():
                issues += self._check_sentence(sentence, owners, pattern, index, where)
        return issues

    def _check_sentence(
        self,
        sentence: str,
        owners: set[str],
        pattern: re.Pattern[str] | None,
        index: dict[str, _Known],
        where: str,
    ) -> list[NameIssue]:
        folded = fold(sentence)
        issues: list[NameIssue] = []
        names: list[tuple[int, int, _Known]] = []
        if pattern is not None:
            for m in pattern.finditer(folded):
                known = index[m.lastgroup or ""]
                if _generic(known.name) and not m.group(0)[:1].isupper():
                    continue  # "the chicane" is a description; "Chicane" is the name
                names.append((m.start(), m.end(), known))
        labels = [(m.start(), m.end(), f"T{int(m.group(1))}") for m in _LABEL_RE.finditer(folded)]
        # Only corners of the map count: a wheel ("RF") or a provisional bucket is no corner.
        scope = {
            other
            for lb in owners | {lb for _, _, lb in labels}
            if self._knows(lb)
            for other in self._also(lb)
        }
        adjacent: set[int] = set()

        for start, end, label in labels:
            if not self._knows(label):
                continue  # an unknown label is the label check's business
            sep = _SEP_RE.match(folded, end)
            at = sep.end() if sep and sep.end() > end else None
            if at is None:
                continue
            hit = next((n for n in names if n[0] == at), None)
            allowed = self._also(label)
            truths = [t for t in (self.labels.get(lb) for lb in sorted(allowed)) if t]
            truth = " or ".join(truths) or None
            if hit is not None:
                adjacent.add(hit[0])
                if hit[2].label in allowed or name_key(hit[2].name) in map(name_key, truths):
                    continue
                issues.append(NameIssue(self._wrong(where, label, hit[2].name, truth), label))
                continue
            sep_text = folded[end:at].strip()
            if sep_text == ":" or (sep_text == "" and not folded[:start].strip()):
                # "T7: Move the brake point", "T7 Brake earlier": after a colon, or a bare
                # label opening the sentence, a capitalised word is the sentence going on.
                continue
            cand = _candidate(folded, at)
            if cand is None:
                continue
            if any(name_key(cand).startswith(name_key(t)) for t in truths):
                continue
            issues.append(NameIssue(self._wrong(where, label, cand, truth), label))

        for start, _, known in names:
            if start in adjacent:
                continue
            if not known.here:
                issues.append(
                    NameIssue(
                        f"{where}: names {known.name!r}, which is not a corner of this circuit"
                        + (" (it is not on the session's corner map)." if not self.venue else "."),
                        known.name,
                    )
                )
                continue
            if known.label is None or not scope or known.label in scope:
                continue
            about = "/".join(sorted(scope))
            issues.append(
                NameIssue(
                    f"{where}: names {known.name!r}, which is {known.label}, but this is about "
                    f"{about}.",
                    about,
                )
            )
        return issues

    @staticmethod
    def _wrong(where: str, label: str, said: str, truth: str | None) -> str:
        if truth:
            return f"{where}: calls {label} {said!r}, but {label} is {truth}."
        return (
            f"{where}: calls {label} {said!r}, but {label} has no mapped name (the corner map "
            f"numbers this circuit's corners)."
        )


def _candidate(folded: str, at: int) -> str | None:
    """The capitalised name written at ``at``, unless it is an ordinary word."""
    m = _CANDIDATE_RE.match(folded, at)
    if m is None:
        return None
    words = m.group(0).split(" ")
    first = words[0].rstrip(".").casefold()
    if first in _NOT_A_NAME or first in _GENERIC:
        return None
    if re.fullmatch(r"[A-Z]{1,4}", words[0]) or any(ch.isdigit() for ch in words[0]):
        return None  # "T4 RF", "T7 ABS", "T3 S2"
    # Stop at the first ordinary word: "Pouhon Brake" is the name "Pouhon".
    kept: list[str] = []
    for w in words:
        if kept and w.casefold() in _NOT_A_NAME:
            break
        kept.append(w)
    return " ".join(kept).rstrip(".")
