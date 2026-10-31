# `spa_run`: eval cases from a real Kimi Spa replay

Every agent run (with an answer) of a pitwall replay of the real Spa race (`.ibt` fixture:
Audi R8 LMS EVO II GT3, Spa, 19 laps; see `docs/` and project files
`fixtures/gt3-audi-r8-spa/`), with agents on `kimi-k3`. One line of `cases.jsonl` per run;
`prompts.json` holds each system prompt once (by `prompt_sha`); `source.json` names the
journal and its sha256; `baseline.json` is the offline result the tests compare against.
The full journal stays in project files (`spa-kimi-run-F3/`), not in the repo.

Current set: 12 runs (coach 5, strategist 3, vehicle_engineer 3, spotter 1) from the first
~1080 s of the race. That replay was cut short by a Kimi quota 429; the set is regenerated
from a full replay when the key has quota again (same commands, below).

## Use

```bash
python -m pytest tests/test_real_evals.py               # offline, free
python scripts/build_real_evals.py --diff spa_run        # what a check change moves
python scripts/build_real_evals.py --baseline spa_run    # accept it (commit the diff)
python evals/run_pitwall.py --real spa_run --dry-run     # recorded calls + scores, free
python evals/run_pitwall.py --real spa_run [--agent coach] [--limit 3] \
    [--judge-provider gemini] [--out live.json]          # re-execute on Kimi (quota)
```

## Regenerate

1. Start the app with `RTV_PITWALL_AGENTS=true`, import the Spa `.ibt`, and replay it:
   `POST /api/v1/replay/start {"session_id": ..., "speed": 2}`.
2. Take the newest journal, `data/sessions/<id>/runs/*.jsonl` (schema 3 or later), and
   copy it to project files.
3. `python scripts/build_real_evals.py --journal <file> --out evals/real/spa_run --label "<where it is kept>"`
   (`--max-cases N` keeps the first N runs per agent if the set gets too large).
4. Run the tests and commit `evals/real/spa_run/` with the new baseline.

## What the offline result means

Per case: `answers` (today's validator and policy on each recorded answer, and whether that
matches the verdict recorded live), `checks` (the role scorer of `evals/pitwall_checks.py`
on the final answer), `corners` (corner labels and names checked against the session's
corner map) and `validity` (the B1 air-time rule re-stamped and checked at the recorded air
time). Findings in the first baseline: the coach said "T8" and "T4" while the corner map
was still provisional; `engineer_check` flags "T4 Speaker's Corner" against the event's
bucket `C09` (the check compares the raw strings, a false positive for a later fix).
