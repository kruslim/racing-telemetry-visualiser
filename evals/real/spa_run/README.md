# `spa_run`: eval cases from a real Kimi Spa replay

Every agent run (with an answer) of a pitwall replay of the real Spa race (`.ibt` fixture:
Audi R8 LMS EVO II GT3, Spa, 19 laps; see `docs/` and project files
`fixtures/gt3-audi-r8-spa/`), with agents on `kimi-k3`. One line of `cases.jsonl` per run;
`prompts.json` holds each system prompt once (by `prompt_sha`); `source.json` names the
journal and its sha256; `baseline.json` is the offline result the tests compare against.
The full journal stays in project files (`spa-kimi-run-F3/`), not in the repo.

Current set: 23 runs (coach 10, strategist 6, vehicle_engineer 6, spotter 1) from the
combined 2x Spa replay of 2026-11-01 on main 41ceed6 (A1-A3, B2, B3, C1, D1, D4), with
`scripts/ask_during_replay.py` asking the driver questions alongside; the two runs that
errored before answering are left out. The full journal is project files
`spa-kimi-run-combined/`. About 0.9 MB, most of it the full race state per case.

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
time). In this baseline every answer reproduces its live verdict. Findings: the coach named
"T8" and "T4" before the corner map was built; `engineer_check` flags "T4 Speaker's Corner"
against the event's bucket `C09` (it compares raw strings: a false positive in the check);
one spotter answer to a driver question is 19 words against the spotter's 14.

After R1 (fixes from the combined replay) the baseline was rebuilt again, and four answers
now differ from their live verdict, all on purpose: the engineer's two "one lap to green"
answers and the coach's "second gear ... slip 0.23 lap 13" now pass (a spelled-out "one" is
speech; the gear diagnostic's "reference used 2" is a gear; "0.23 lap 13" is a slip figure
then a lap number), and the spotter's 19-word answer now fails the spotter's own 14-word
limit, which the live validator holds it to. The engineer's "17.4 litres" (the strategist's
fuel, heard on the radio) still fails; its repair hint now names the strategist's call.
