"""The shared view golden file, run against the Python desktop models.

``tests/fixtures/view_golden/`` holds ``payload -> expected view fields`` cases built
from the Spa example payloads by ``scripts/build_view_golden.py``. The JS view models
run the same files under node (``frontend/js/run-golden-tests.mjs``), so a rule that
drifts between the web pages and ``rtv.desktop.model`` fails on one side.

This module mirrors the node runner: the same argument markers, the same neutral
output shape (snake_case keys, maps as lists of their values) and the same rule that
an expected object lists the fields both sides share. Test-only.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import math
from collections import OrderedDict
from pathlib import Path
from typing import Any

from rtv.desktop.model import activity, review, view_model
from rtv.desktop.model import format as fmt

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "view_golden"
MODULES = {"format": fmt, "view_model": view_model, "activity": activity, "review": review}


def snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (name[i - 1].islower() or name[i - 1].isdigit()):
            out.append("_" + ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def resolve_arg(arg: Any, inputs: dict) -> Any:
    if isinstance(arg, list):
        return [resolve_arg(a, inputs) for a in arg]
    if not isinstance(arg, dict):
        return arg
    if "$input" in arg:
        value = inputs[arg["$input"]]
        return copy.deepcopy(value[arg["index"]] if "index" in arg else value)
    if "$auditRows" in arg:
        return review.audit_rows(resolve_arg(arg["$auditRows"], inputs))
    if "$opts" in arg:
        return {snake(k): resolve_arg(v, inputs) for k, v in arg["$opts"].items()}
    if "$model" in arg:
        return activity.reduce(resolve_arg(arg["$model"], inputs), arg.get("up_to"))
    if "$trends" in arg:
        tracker = view_model.GapTrendTracker()
        for state in resolve_arg(arg["$trends"], inputs):
            tracker.update(state)
        return tracker
    if "$warnings" in arg:
        warnings = view_model.TowerWarnings()
        for event in resolve_arg(arg["$warnings"]["events"], inputs):
            warnings.observe(event)
        return warnings.clock(arg["$warnings"].get("clock"))
    return {k: resolve_arg(v, inputs) for k, v in arg.items()}


def call(case: dict, inputs: dict) -> Any:
    args = resolve_arg(case.get("args") or [], inputs)
    kwargs = resolve_arg({"$opts": case["kwargs"]}, inputs) if case.get("kwargs") else {}
    if case["fn"] == "FuelHistory.points":
        history = view_model.FuelHistory()
        for state in args[0]:
            history.update(state)
        return history.points()
    fn = getattr(MODULES[case["module"]], snake(case["fn"]))
    return fn(*args, **kwargs)


def neutral(value: Any) -> Any:
    """The shape both runners compare in."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: neutral(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, OrderedDict):
        return [neutral(v) for v in value.values()]
    if isinstance(value, dict):
        return {snake(str(k)): neutral(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [neutral(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def mismatch(expected: Any, actual: Any, path: str = "$") -> str | None:
    """Where ``actual`` departs from ``expected``, or None. Extra actual keys are allowed."""
    numeric = (int, float)
    if (
        isinstance(expected, numeric)
        and isinstance(actual, numeric)
        and not isinstance(expected, bool)
        and not isinstance(actual, bool)
    ):
        if abs(expected - actual) <= 1e-9 * max(1, abs(expected)):
            return None
        return f"{path}: expected {expected}, got {actual}"
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return f"{path}: expected a list, got {json.dumps(actual)}"
        if len(expected) != len(actual):
            return f"{path}: expected {len(expected)} items, got {len(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            if m := mismatch(e, a, f"{path}[{i}]"):
                return m
        return None
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return f"{path}: expected an object, got {json.dumps(actual)}"
        for key, value in expected.items():
            if key not in actual:
                return f"{path}.{key}: missing"
            if m := mismatch(value, actual[key], f"{path}.{key}"):
                return m
        return None
    if expected == actual and type(expected) is type(actual):
        return None
    return f"{path}: expected {json.dumps(expected)}, got {json.dumps(actual)}"


def load_golden(directory: Path = GOLDEN_DIR) -> tuple[dict, list[tuple[str, dict]]]:
    inputs = json.loads((directory / "inputs.json").read_text("utf-8"))
    cases = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "inputs.json":
            continue
        for case in json.loads(path.read_text("utf-8"))["cases"]:
            cases.append((path.stem, case))
    return inputs, cases
