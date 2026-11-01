"""Hermetic debrief cases (stage 09 part B, DB5): scripted answers over a recorded journal.

Each case is built from a real debrief input (the journal of a replayed synthetic race, see
:mod:`evals.synthetic_debrief`): a *faithful* answer whose every figure, lap and corner is
copied from the inputs, and broken variants of it, each carrying one defect a model makes:

* ``invented_figure`` -- a lap time that is in no input;
* ``unrecorded_lap`` -- a point about a lap the run never recorded;
* ``unsupported_priority`` -- a priority about a corner its basis section never discusses;
* ``swapped_fact`` -- the right number for the wrong fact: a fuel figure said as seconds;
* ``hallucinated_corner`` -- a corner label the findings do not have;
* ``wrong_corner_name`` -- a real label with a name the run does not give it.

A case says which sections must be kept or refused when the scripted model repeats the
broken answer on its repair turn, and :func:`scripted_handler` turns a case into a
:class:`~rtv.pitwall.provider.ScriptedProvider` handler. No network, no key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from rtv.debrief.checks import fact_set
from rtv.debrief.contract import DebriefAnswer, DebriefItem, DebriefPriority
from rtv.grounding.units import Figure


@dataclass
class DebriefCase:
    name: str
    answer: DebriefAnswer
    #: What the scripted model answers on the repair turn (None = the same answer again).
    repair: DebriefAnswer | None = None
    #: section -> the status it must end with (ok, repaired, refused).
    expect: dict[str, str] = field(default_factory=dict)


def _fmt(value: float, nd: int = 3) -> str:
    return f"{round(float(value), nd):g}"


def faithful_answer(inputs: dict[str, Any]) -> DebriefAnswer:
    """An answer made only of what the inputs say, in the units they say it."""
    summary = inputs.get("summary") or {}
    findings = inputs.get("findings") or {}
    parts = []
    if summary.get("finish_position"):
        parts.append(f"Finished P{summary['finish_position']}")
    if summary.get("laps"):
        parts.append(f"over {summary['laps']} laps")
    head = " ".join(parts) or "The race is recorded"
    best = ""
    if summary.get("best_time") is not None and summary.get("best_lap") is not None:
        best = f" The best lap was lap {summary['best_lap']} in {_fmt(summary['best_time'])} s."
    plan = inputs.get("plan") or {}
    final = plan.get("final") or {}
    if final.get("fuel_per_lap_target_l") is not None:
        plan_text = (
            f"The plan was revised {len(plan.get('revisions') or [])} times and finished on a "
            f"fuel target of {_fmt(final['fuel_per_lap_target_l'])} L a lap."
        )
    elif final:
        plan_text = "The race had a plan, but it set no fuel target."
    else:
        plan_text = "There was no race plan in this run."
    time_lost = [
        DebriefItem(
            text=f"Lap {lap['lap']}: {top['corner']} cost {_fmt(top['loss_s'])} s.",
            corner=top["corner"],
            laps=[lap["lap"]],
        )
        for lap in findings.get("laps") or []
        for top in (lap.get("top") or [])[:1]
    ]
    recurring = [
        DebriefItem(
            text=(
                f"{row['corner']} lost time on {row['laps_losing']} laps, "
                f"{_fmt(row['total_loss_s'])} s in all; worst on lap {row['worst_lap']}."
            ),
            corner=row["corner"],
            laps=list(row["laps"]),
        )
        for row in findings.get("recurring") or []
        if row.get("laps_losing", 0) >= 2
    ]
    if summary.get("fuel_per_lap_l") is not None:
        tyres_fuel = f"Fuel use averaged {_fmt(summary['fuel_per_lap_l'])} L a lap."
    else:
        tyres_fuel = "The journal has no fuel figures, so fuel cannot be compared."
    priorities = []
    if recurring:
        priorities.append(
            DebriefPriority(
                text=f"Work on {recurring[0].corner}: it lost time lap after lap.",
                basis="recurring",
                corner=recurring[0].corner,
            )
        )
    if time_lost:
        priorities.append(
            DebriefPriority(text="Then the single worst corners lap by lap.", basis="time_lost")
        )
    return DebriefAnswer(
        summary=f"{head}.{best}",
        plan=plan_text,
        time_lost=time_lost,
        recurring=recurring,
        tyres_fuel=tyres_fuel,
        priorities=priorities,
    )


def _swap_value(inputs: dict[str, Any]) -> float:
    """A litres figure of the inputs that no seconds fact can stand for."""
    facts = fact_set(inputs)
    for value, _unit, _path in facts.facts("litres"):
        if abs(value) <= 3 and float(value).is_integer():
            continue  # a bare small integer is waved through anyway
        if not facts.supports_figure(Figure(round(value, 2), _fmt(value, 2), "s", "s")):
            return round(value, 2)
    raise LookupError("no litres fact is distinct from every seconds fact")


def debrief_cases(inputs: dict[str, Any]) -> list[DebriefCase]:
    good = faithful_answer(inputs)
    if not good.recurring or not good.time_lost:
        raise LookupError("the inputs need recurring and per-lap losses for the cases")
    known = {c["label"] for c in (inputs.get("findings") or {}).get("corners") or []}
    discussed = {i.corner for i in good.recurring}
    other = next((c for c in sorted(known) if c not in discussed), None)
    unknown = next(f"T{n}" for n in range(1, 99) if f"T{n}" not in known)
    missing_lap = max(inputs.get("recorded_laps") or [0]) + 30
    named = {k for k, v in (inputs.get("corner_names") or {}).items() if v}
    plain = next(c for c in sorted(known) if c not in named)

    def edit(**changes: Any) -> DebriefAnswer:
        return good.model_copy(update=changes)

    first_loss = good.time_lost[0]
    cases = [
        DebriefCase(
            "faithful",
            good,
            expect={s: "ok" for s in DebriefAnswer.model_fields},
        ),
        DebriefCase(
            "invented_figure",
            edit(summary=good.summary + " The fastest lap was 7.123 s."),
            expect={"summary": "refused", "recurring": "ok", "priorities": "ok"},
        ),
        DebriefCase(
            "invented_figure_repaired",
            edit(summary=good.summary + " The fastest lap was 7.123 s."),
            repair=good,
            expect={"summary": "repaired", "time_lost": "ok", "priorities": "ok"},
        ),
        DebriefCase(
            "unrecorded_lap",
            edit(
                time_lost=[
                    first_loss.model_copy(
                        update={"laps": [missing_lap], "text": f"Lap {missing_lap} was slow."}
                    ),
                    *good.time_lost[1:],
                ]
            ),
            expect={"time_lost": "refused", "summary": "ok"},
        ),
        DebriefCase(
            "swapped_fact",
            edit(
                time_lost=[
                    first_loss.model_copy(
                        update={
                            "text": f"Lap {first_loss.laps[0]}: {first_loss.corner} cost "
                            f"{_fmt(_swap_value(inputs), 2)} s."
                        }
                    ),
                    *good.time_lost[1:],
                ]
            ),
            expect={"time_lost": "refused", "recurring": "ok"},
        ),
        DebriefCase(
            "hallucinated_corner",
            edit(
                recurring=[
                    good.recurring[0].model_copy(
                        update={"corner": unknown, "text": f"{unknown} kept costing time."}
                    ),
                    *good.recurring[1:],
                ]
            ),
            expect={"recurring": "refused", "time_lost": "ok"},
        ),
        DebriefCase(
            "wrong_corner_name",
            edit(
                recurring=[
                    good.recurring[0].model_copy(update={"corner": f"{plain} La Source"}),
                    *good.recurring[1:],
                ]
            )
            if good.recurring[0].corner == plain
            else edit(
                time_lost=[
                    *good.time_lost,
                    DebriefItem(text=f"{plain} needs a look.", corner=f"{plain} La Source"),
                ]
            ),
            expect={"summary": "ok"},
        ),
    ]
    # The wrong name lands in whichever section was edited.
    last = cases[-1]
    edited = "recurring" if last.answer.recurring != good.recurring else "time_lost"
    last.expect[edited] = "refused"
    # A corner the basis section never discusses: the plan section names no corner.
    corner = other or good.recurring[0].corner
    basis = "recurring" if other is not None else "plan"
    cases.append(
        DebriefCase(
            "unsupported_priority",
            edit(
                priorities=[
                    DebriefPriority(text=f"Work on {corner} above all.", basis=basis, corner=corner)
                ]
            ),
            expect={"priorities": "refused", "recurring": "ok"},
        )
    )
    cases.append(
        DebriefCase(
            "priority_on_a_refused_section",
            edit(
                recurring=[
                    good.recurring[0].model_copy(update={"text": "It cost 7.123 s there."})
                ]
            ),
            expect={"recurring": "refused", "priorities": "refused"},
        )
    )
    return cases


def inputs_of(prompt: str) -> dict[str, Any]:
    """The INPUTS document of a debrief prompt (the scripted model reads it, like Kimi)."""
    body = prompt.split("INPUTS:\n", 1)[1]
    return json.JSONDecoder().raw_decode(body)[0]


def scripted_handler(case: DebriefCase):
    """A ScriptedProvider handler: the case's answer, then its repair (or the same)."""

    def handler(*, messages, **_kw):
        prompt = messages[0]["content"]
        if "YOUR PREVIOUS ANSWER" in prompt:
            return case.repair or case.answer
        return case.answer

    return handler
