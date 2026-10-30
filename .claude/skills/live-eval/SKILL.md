---
name: live-eval
description: Run the paid coaching eval (evals/run.py) against the golden laps with the live Claude coach and LLM judge, then summarise the scores.
argument-hint: "[--no-judge] [--cases <file>]"
disable-model-invocation: true
---

# Live coaching eval

This calls the Anthropic API and costs money. The user invoked it, so the run is approved;
run it once, with the flags given, and no more.

Arguments: $ARGUMENTS (default: all cases in `evals/golden_laps.json`, with the judge).

1. Check the setup without reading `.env`: `ANTHROPIC_API_KEY` must be set in the
   environment (print only whether it is set, never its value), and the store must hold
   the sessions the cases name. Cases whose `session_id` is still
   `REPLACE_WITH_REAL_SESSION_ID` can't run; list real ones with
   `GET /api/v1/sessions` and `/sessions/{id}/laps` and ask the user which to use.
2. Run `.venv/Scripts/python.exe evals/run.py $ARGUMENTS` in the background.
3. Report per case: outcome accuracy, claim factuality (supported/claims), the issues it
   printed and, with the judge, clarity, actionability and grounding. Then the summary
   block. Note the model used (`RTV_COACHING_MODEL`, default in `src/rtv/config.py`).
4. If a factuality issue points at a real bug in the coach or the checks, say so and
   propose a fix; don't change code in this skill.

The free, hermetic version is the pytest suite (`tests/test_coach_eval_runner.py`).
