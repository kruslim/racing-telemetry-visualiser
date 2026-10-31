"""Ground a VLM answer before anything downstream may read it.

Two rules from the design, applied in this order:

1. **Number-free.** Every number in this app comes from telemetry. An observation
   with a digit or a spelled-out measurement in it ("half a second", "two metres")
   is dropped, not edited: a model that measured something from pixels has
   guessed, and a guessed figure that reaches the coach or the radio is exactly
   the failure the citation validators exist to stop. The moment's own corner
   label (``T7``) is the one token with a digit that is allowed through.
2. **Telemetry wins.** Where a deterministic fact on the moment can check an
   observation, it does: a kerb claim against a flat vertical-acceleration trace
   is contradicted and dropped, and one against a spike is corroborated. When the
   model says the frames do not show the flagged moment at all, the telemetry
   still says it happened, so the note is marked ``trigger_not_seen`` (most often
   a sync error) and its observations are kept only at low confidence.
"""

from __future__ import annotations

import re

from rtv.vision.models import Moment, Observation, VisionAnswer, VisionObservationOut

_DIGIT = re.compile(r"\d")
_SPELLED = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"twenty|thirty|forty|fifty|hundred|thousand|half|quarter|dozen|"
    r"first|second|third|fourth|fifth)\s+"
    r"(?:of\s+an?\s+|an?\s+)?"
    r"(?:metres?|meters?|m|km|kph|mph|km/h|seconds?|secs?|tenths?|hundredths?|"
    r"car[- ]lengths?|lengths?|gears?|laps?|percent|g)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:no|not|never|without|avoid(?:s|ed|ing)?|stay(?:s|ed)? off|kept off|"
    r"clear of|didn'?t|doesn'?t|did not|does not)\b",
    re.IGNORECASE,
)
_KERB = re.compile(r"\b(?:kerbs?|curbs?|rumble strips?|sausage)\b", re.IGNORECASE)


def numeric_reason(text: str, allowed: tuple[str, ...] = ()) -> str | None:
    """Why ``text`` breaks the number-free rule, or ``None`` if it does not."""
    scrubbed = text
    for token in allowed:
        if token:
            scrubbed = re.sub(rf"\b{re.escape(token)}\b", "", scrubbed, flags=re.IGNORECASE)
    if _DIGIT.search(scrubbed):
        return "contains a number"
    if _SPELLED.search(scrubbed):
        return "contains a measurement"
    return None


def _kerb_claim(obs: VisionObservationOut) -> bool | None:
    """True: says the car used a kerb. False: says it stayed off. None: neither."""
    if obs.kind != "kerb_use" and not _KERB.search(obs.text):
        return None
    return not _NEGATION.search(obs.text)


def ground(answer: VisionAnswer, moment: Moment) -> tuple[str, list[Observation]]:
    """``(status, observations)`` for one moment. Every input observation is returned,
    with a status; only ``corroborated`` and ``uncheckable`` ones are kept."""
    allowed = tuple(t for t in {moment.corner, *(t.corner for t in moment.triggers)} if t)
    kerb_fact = moment.facts.get("kerb_signal")
    not_seen = answer.trigger_visible == "no"
    out: list[Observation] = []
    for obs in answer.observations:
        text = " ".join(obs.text.split())
        if not text:
            continue
        why = numeric_reason(text, allowed)
        if why is not None:
            out.append(
                Observation(
                    kind=obs.kind,
                    text=text,
                    confidence=obs.confidence,
                    status="dropped",
                    reason=f"Number-free rule: {why}.",
                )
            )
            continue
        status, reason = "uncheckable", None
        claim = _kerb_claim(obs)
        if claim is not None and kerb_fact is not None:
            if claim and kerb_fact is False:
                status, reason = "contradicted", "Vertical acceleration is flat in this window."
            elif not claim and kerb_fact is True:
                status, reason = "contradicted", "Vertical acceleration spikes in this window."
            else:
                status, reason = "corroborated", "Matches the vertical-acceleration trace."
        confidence = obs.confidence
        if not_seen and status != "contradicted":
            confidence = "low"
            reason = reason or "The model did not see the flagged moment; check the sync."
        out.append(
            Observation(
                kind=obs.kind, text=text, confidence=confidence, status=status, reason=reason
            )
        )
    return ("trigger_not_seen" if not_seen else "ok"), out
