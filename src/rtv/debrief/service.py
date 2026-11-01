"""Write, store and serve the post-race debrief of one journal run (DB2, DB3).

:class:`DebriefService` is one object on :class:`~rtv.services.AppServices`. It builds the
inputs (:mod:`rtv.debrief.inputs`), always computes the deterministic half, and, when a
provider is available, asks Kimi for the analysis half in one structured call
(:class:`~rtv.pitwall.provider.KimiProvider` with ``thinking=True``: there is no latency
budget after the race). Each section of the answer is checked (:mod:`rtv.debrief.checks`);
a section that fails is repaired once and refused with its reason if it still fails, as the
coaches do. With no key, or when the call fails, the debrief is the deterministic half plus
a plain note that the analysis is unavailable.

The result is stored as ``<run_id>.debrief.json`` beside the run and recorded as a
``debrief`` record at the end of the run's journal. Regenerating reads the recorded
journal again; it never re-runs the live agents.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rtv.debrief.checks import (
    SectionReport,
    check_priorities,
    check_section,
    fact_set,
    known_corners,
)
from rtv.debrief.contract import SECTION_TITLES, SECTIONS, DebriefAnswer
from rtv.debrief.inputs import build_inputs
from rtv.logging import get_logger
from rtv.store import journal

log = get_logger("debrief")

#: Bumped when the stored document changes shape.
DEBRIEF_SCHEMA = 1
#: Thinking needs room; the answer itself is a few hundred words.
MAX_TOKENS = 16_000
#: The layer name for usage accounting (``GET /api/v1/llm/usage``).
LAYER = "debrief"

SYSTEM_PROMPT = """\
You are the race engineer writing the debrief after a race, for the driver to read at a
desk. You are not on the radio: write plain, complete sentences.

You are given one JSON document, INPUTS, built deterministically from the race journal,
the stored race summary and the Layer-1 lap findings (every clean lap compared with the
best one). It is everything you know. Rules that are checked after you answer:

1. Every number you write must be a number in INPUTS, in the same unit as its field
   (fields ending _s are seconds, _l litres, _kmh km/h, _c degrees C, _kpa kPa; "lap N"
   is a lap number; "P4" a position). You may round. Do not compute new figures
   (no sums, averages or projections of your own), except the difference of two figures
   you write in the same sentence.
2. Only refer to laps listed in INPUTS.recorded_laps (or a lap the plan names).
3. Refer to corners by the labels in INPUTS (findings.corners, findings.recurring,
   events). If you add a name, it must be the name INPUTS gives that label
   (corner_names); otherwise write the label alone. Never invent a corner.
4. A brake-point instruction must agree with the corner's diagnostics ("Brake point
   9 m late" means brake earlier there).
5. Where time was lost comes from findings.laps, findings.recurring and the lap times,
   not impressions. Recurring issues are corners that lost time on several laps.
6. Tyres and fuel: compare fuel_by_lap / tyres_by_lap and the summary with the
   projection as it stood (per_lap_l, laps_remaining, margin_laps; the plan's targets).
7. Each priority rests on one section above it (its basis); a priority about a corner
   must be about a corner that section discusses. At most five, most important first.
8. When the data does not say something, say that it does not; never fill a gap.

Answer by calling the answer tool once with every field filled."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _names(value: str) -> set[str]:
    return {n.strip().lower() for n in (value or "").split(",") if n.strip()}


def render_prompt(inputs: dict[str, Any]) -> str:
    return "INPUTS:\n" + json.dumps(inputs, separators=(",", ":"), default=str)


def repair_prompt(inputs: dict[str, Any], answer: DebriefAnswer, issues: list[str]) -> str:
    return (
        render_prompt(inputs)
        + "\n\nYOUR PREVIOUS ANSWER:\n"
        + answer.model_dump_json()
        + "\n\nIt failed these checks:\n- "
        + "\n- ".join(issues)
        + "\n\nAnswer again with every section. Fix each issue: use only figures, laps and "
        "corners that are in INPUTS, or leave the claim out. Keep the sections that passed."
    )


def deterministic_half(inputs: dict[str, Any]) -> dict[str, Any]:
    """Everything the debrief can say with no model: the summary, laps, findings, plan."""
    summary = inputs.get("summary") or {}
    findings = inputs.get("findings") or {}
    plan = inputs.get("plan") or {}
    radio = inputs.get("radio") or []
    return {
        "result": {
            k: summary.get(k)
            for k in (
                "start_position",
                "finish_position",
                "laps",
                "clean_laps",
                "best_lap",
                "best_time",
                "median_time",
                "stdev_time",
                "pit_stops",
                "incidents",
            )
        },
        "laps": inputs.get("laps") or [],
        "stints": summary.get("stints") or [],
        "fuel": {
            "used_l": summary.get("fuel_used_l"),
            "added_l": summary.get("fuel_added_l"),
            "per_lap_l": summary.get("fuel_per_lap_l"),
            "final": (inputs.get("final_state") or {}).get("fuel"),
            "by_lap": inputs.get("fuel_by_lap") or [],
        },
        "tyres_by_lap": inputs.get("tyres_by_lap") or [],
        "plan": {
            "final": plan.get("final"),
            "revisions": plan.get("revisions") or [],
            "rejected": plan.get("rejected") or [],
        },
        "reference_lap": findings.get("reference_lap"),
        "time_lost": findings.get("laps") or [],
        "recurring": [r for r in findings.get("recurring") or [] if r.get("laps_losing", 0) >= 2],
        "events": summary.get("events") or {},
        "radio": {
            "calls": len(radio),
            "by_status": dict(Counter(r.get("status") for r in radio).most_common()),
            "by_agent": dict(Counter(r.get("agent") for r in radio).most_common()),
        },
        "notes": [*(summary.get("notes") or []), *(findings.get("notes") or [])],
    }


def _content(answer: DebriefAnswer, name: str) -> Any:
    value = getattr(answer, name)
    if isinstance(value, list):
        return [item.model_dump() for item in value]
    return value


def _section(name: str, status: str, content: Any = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "title": SECTION_TITLES[name], "status": status}
    if isinstance(content, list):
        out["items"] = content
    elif content is not None:
        out["text"] = content
    out.update({k: v for k, v in extra.items() if v not in (None, [], "")})
    return out


class AnalysisUnavailable(RuntimeError):
    """The model half could not be produced (no answer, a failed call)."""


class DebriefService:
    """The debrief of journal runs: generate, store, load, and the session-end hook."""

    def __init__(
        self,
        settings: Any,
        repo: Any,
        coaching: Any,
        *,
        provider: Any | None = None,
        notifier: Any | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.coaching = coaching
        #: An injected provider (tests, evals); otherwise Kimi when a key is configured.
        self.provider = provider
        self.notifier = notifier
        self._kimi: Any | None = None
        self._lock = threading.Lock()
        #: Completed debriefs, for status and tests.
        self.generated = 0

    # ---- configuration ---------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.settings.journal_dir)

    def model(self) -> str:
        from rtv import llm

        return llm.model("reasoning", override=self.settings.debrief_model, settings=self.settings)

    def auto(self) -> bool:
        value = str(self.settings.debrief_auto or "auto").strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        return bool(self.settings.pitwall_agents)

    def sessions(self) -> set[str]:
        return _names(self.settings.debrief_sessions)

    def resolve_provider(self) -> tuple[Any | None, str | None]:
        """The provider to call, or ``None`` and why the analysis is unavailable."""
        if self.provider is not None:
            return self.provider, None
        from rtv import llm

        if not llm.is_configured(self.settings):
            return None, f"no Kimi key is configured ({llm.key_env_var(self.settings)})"
        if self._kimi is None:
            try:
                from rtv.pitwall.provider import KimiProvider

                self._kimi = KimiProvider(thinking=True)
            except Exception as exc:  # the ai extra is optional
                log.warning("Debrief analysis unavailable: %s", exc)
                return None, "the 'ai' extra is not installed"
        return self._kimi, None

    def can_analyse(self) -> bool:
        return self.resolve_provider()[0] is not None

    # ---- storage ---------------------------------------------------------
    def run_file(self, session_id: str, run_id: str) -> Path:
        return journal.run_path(self.root, session_id, run_id)

    def load(self, session_id: str, run_id: str) -> dict[str, Any] | None:
        path = journal.debrief_path(self.run_file(session_id, run_id))
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Unreadable debrief %s", path)
            return None

    def _store(self, path: Path, doc: dict[str, Any]) -> None:
        out = journal.debrief_path(path)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
        tmp.replace(out)
        journal.append_record(
            path,
            "debrief",
            {
                "path": out.name,
                "trigger": doc["trigger"],
                "generated_at": doc["generated_at"],
                "analysis": doc["analysis"],
                "model": doc.get("model"),
                "sections": {s["name"]: s["status"] for s in doc["sections"]},
            },
        )

    # ---- generation ------------------------------------------------------
    def generate(
        self, session_id: str, run_id: str, *, trigger: str = "manual"
    ) -> dict[str, Any]:
        """Build, check, store and return the debrief of one recorded run."""
        path = self.run_file(session_id, run_id)
        if not path.is_file():
            raise FileNotFoundError(f"Unknown journal run {run_id} for session {session_id}")
        with self._lock:  # one debrief at a time: they share the store and the provider
            inputs = build_inputs(
                session_id=session_id, path=path, repo=self.repo, coaching=self.coaching
            )
            provider, reason = self.resolve_provider()
            analysis: dict[str, Any]
            sections: list[dict[str, Any]]
            model = None
            calls = 0
            if provider is None:
                analysis = {"available": False, "reason": reason}
                sections = []
            else:
                model = self.model()
                try:
                    sections, calls = asyncio.run(self._analyse(provider, model, inputs))
                    analysis = {"available": True, "reason": None}
                except Exception as exc:
                    log.warning("Debrief analysis of %s/%s failed: %s", session_id, run_id, exc)
                    analysis = {
                        "available": False,
                        "reason": f"the analysis call failed ({type(exc).__name__}: {exc})",
                    }
                    sections = []
            if not analysis["available"]:
                sections = [_section(name, "unavailable") for name in SECTIONS]
            kept = sum(s["status"] in {"ok", "repaired"} for s in sections)
            doc = {
                "schema": DEBRIEF_SCHEMA,
                "session_id": session_id,
                "run_id": run_id,
                "session_type": inputs["run"].get("session_type"),
                "generated_at": _now(),
                "trigger": trigger,
                "model": model,
                "model_calls": calls,
                "analysis": analysis,
                "note": (
                    None
                    if analysis["available"]
                    else f"The analysis half is unavailable: {analysis['reason']}. "
                    "This debrief is the deterministic summary only."
                ),
                "kept_sections": kept,
                "sections": sections,
                "deterministic": deterministic_half(inputs),
                "references": {
                    "laps": inputs["recorded_laps"],
                    "corners": inputs["corner_names"],
                    "reference_lap": inputs["findings"].get("reference_lap"),
                },
                "inputs": inputs,
            }
            self._store(path, doc)
            self.generated += 1
        log.info(
            "Debrief of %s/%s stored (%s, %d/%d sections kept)",
            session_id, run_id,
            "analysis" if analysis["available"] else "deterministic only",
            kept, len(SECTIONS),
        )
        return doc

    async def _analyse(
        self, provider: Any, model: str, inputs: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int]:
        from rtv.pitwall.provider import structured_call

        system = [{"type": "text", "text": SYSTEM_PROMPT}]
        facts = fact_set(inputs)
        corners = known_corners(inputs)
        calls = 1
        first = await structured_call(
            provider,
            agent=LAYER,
            model=model,
            system=system,
            prompt=render_prompt(inputs),
            output_model=DebriefAnswer,
            max_tokens=MAX_TOKENS,
        )
        if first is None:
            raise AnalysisUnavailable("the model returned no valid answer")
        reports = self._check(first, facts, inputs, corners)
        second = None
        second_reports: dict[str, SectionReport] = {}
        if not all(r.ok for r in reports.values()):
            issues = [i for r in reports.values() for i in r.issues]
            calls += 1
            second = await structured_call(
                provider,
                agent=LAYER,
                model=model,
                system=system,
                prompt=repair_prompt(inputs, first, issues),
                output_model=DebriefAnswer,
                max_tokens=MAX_TOKENS,
            )
            if second is not None:
                second_reports = self._check(second, facts, inputs, corners)
        return self._assemble(first, reports, second, second_reports, facts, inputs, corners), calls

    def _check(
        self,
        answer: DebriefAnswer,
        facts: Any,
        inputs: dict[str, Any],
        corners: dict[str, Any],
    ) -> dict[str, SectionReport]:
        reports = {
            name: check_section(name, answer, facts=facts, inputs=inputs, corners=corners)
            for name in SECTIONS
            if name != "priorities"
        }
        kept = {n: _content_obj(answer, n) for n, r in reports.items() if r.ok}
        reports["priorities"] = check_priorities(
            answer.priorities, kept, facts=facts, inputs=inputs, corners=corners
        )
        return reports

    def _assemble(
        self,
        first: DebriefAnswer,
        reports: dict[str, SectionReport],
        second: DebriefAnswer | None,
        second_reports: dict[str, SectionReport],
        facts: Any,
        inputs: dict[str, Any],
        corners: dict[str, Any],
    ) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        kept_obj: dict[str, Any] = {}
        for name in SECTIONS:
            if name == "priorities":
                continue
            if reports[name].ok:
                out[name] = _section(name, "ok", _content(first, name))
                kept_obj[name] = _content_obj(first, name)
            elif second is not None and second_reports[name].ok:
                out[name] = _section(
                    name, "repaired", _content(second, name), first_issues=reports[name].issues
                )
                kept_obj[name] = _content_obj(second, name)
            else:
                issues = (second_reports.get(name) or reports[name]).issues
                out[name] = _section(
                    name,
                    "refused",
                    reason="Refused: the section did not hold up against the inputs after "
                    "one repair. " + " ".join(issues[:4]),
                    issues=issues,
                )
        # The priorities are held to the sections as kept, whichever pass they came from.
        verdicts = []
        for answer in (first, second):
            if answer is None:
                continue
            rep = check_priorities(
                answer.priorities, kept_obj, facts=facts, inputs=inputs, corners=corners
            )
            verdicts.append((answer, rep))
            if rep.ok:
                break
        answer, rep = verdicts[-1]
        if rep.ok:
            status = "ok" if answer is first else "repaired"
            first_issues = None if answer is first else verdicts[0][1].issues
            out["priorities"] = _section(
                "priorities", status, _content(answer, "priorities"), first_issues=first_issues
            )
        else:
            out["priorities"] = _section(
                "priorities",
                "refused",
                reason="Refused: the priorities are not supported by the sections above "
                "them. " + " ".join(rep.issues[:4]),
                issues=rep.issues,
            )
        return [out[name] for name in SECTIONS]

    # ---- the session-end hook --------------------------------------------
    def on_session_end(self, ended: Any) -> None:
        """``AppServices.on_session_end`` callback: the automatic debrief (DB2)."""
        if not self.auto():
            return
        kind = (ended.session_type or "").lower()
        if kind not in self.sessions():
            log.info(
                "No debrief for %s/%s: session type %r is not in RTV_DEBRIEF_SESSIONS",
                ended.session_id, ended.run_id, ended.session_type,
            )
            return
        doc = self.generate(ended.session_id, ended.run_id, trigger="session_end")
        notify = getattr(self.notifier, "notify_debrief", None)
        if notify is not None:
            notify(ended, doc)


def _content_obj(answer: DebriefAnswer, name: str) -> Any:
    return getattr(answer, name)


def debrief_line(doc: dict[str, Any]) -> str:
    """One line about a stored debrief, for the notifier."""
    head = f"Debrief of run {doc.get('run_id')} ready"
    if not (doc.get("analysis") or {}).get("available"):
        return f"{head}: deterministic summary only ({(doc.get('analysis') or {}).get('reason')})."
    return f"{head}: {doc.get('kept_sections')}/{len(SECTIONS)} analysis sections kept."
