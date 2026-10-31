---
paths:
  - "src/rtv/coaching/**"
  - "evals/**"
  - "mcp_server/**"
---

# Coaching layers and evals

- Layer 1 (`coaching/features.py`, `LapFindings`) is deterministic and is the ground
  truth. Models only ever see findings, never raw telemetry rows.
- Every figure the coach asserts (time gain, corner, min speed, brake point) is checked
  against the findings by `coaching/agent/validation.py`. The in-loop `validate` node and
  `evals/checks.py` both use it, so change tolerances or rules there, in one place.
- Figures in the orchestrator's prose (metres, km/h, gear, seconds, g, brake direction) are
  checked by `coaching/agent/prose.py`, with its tolerances in `validation.py`.
- The coach refuses with a grounded `CoachRefusal` when the asked-about channel wasn't
  captured; don't make it guess.
- `anthropic`, `langgraph`, `langchain-*` and `mcp` stay optional: import them lazily
  in code, guard tests with `pytest.importorskip`.
- New coach behaviour gets a hermetic test driven by `ScriptedModel` (`tests/conftest.py`)
  and, where it is a regression risk, a seed case in `evals/cases.py` for the runner.
- Running `evals/run.py`, `scripts/deep_coach.py` (without `--findings-only`) or the
  orchestrator for real costs money: ask the user first, or use the `live-eval` skill.
