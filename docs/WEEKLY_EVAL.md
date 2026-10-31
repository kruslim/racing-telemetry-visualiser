# Weekly live eval

Every Monday a Claude Code scheduled task runs every live eval on the latest `main` and
posts what changed since the week before. It is kept out of PR CI on purpose: CI has no key
and stays free; the weekly run is the one place where the real model is measured on a
schedule, so a model update or an endpoint slowdown on Kimi's side shows up within a week.

## What runs

`scripts/weekly_eval.py`, in this order:

| Step | What | Same as |
|---|---|---|
| `replay` | one Spa replay at 2x with the pitwall agents on, in a server of its own on a throwaway data dir | the `spa-kimi-run-*` replays |
| `real` | the real-journal cases (`evals/real/spa_run`) re-executed live, Gemini judge | `evals/run_pitwall.py --real spa_run --judge-provider gemini` |
| `golden` | the generated pitwall golden sets | `evals/run_pitwall.py` |
| `coach` | the coach eval on five Spa laps (2, 3, 8, 14 vs 6; 16 vs 17), Gemini judge | `evals/run.py --judge-provider gemini` |
| `real_offline` | free: the recorded real cases re-scored with today's checks, diffed with `baseline.json` | `scripts/build_real_evals.py --diff spa_run` |

The Spa `.ibt` comes from `tests/fixtures/real/`, then the project files copy
(`fixtures/gt3-audi-r8-spa/`), else `scripts/fetch_fixtures.py` downloads it. It is imported
once into the throwaway store; the coach cases use that import's session id.

Cost per run, roughly: 25-35 Kimi calls for the replay, about 25 for the real cases, 30-60
for the golden sets and a few per coach case, plus 12 + 5 Gemini judge calls.

## Output

Each run writes `/mnt/project-files/weekly-evals/<YYYY-MM-DD>/` (a second run the same day
gets `-2`; a dry run gets `-dry-run`):

- `REPORT.md`: headline changes first (refusals, validator failures, late and expired
  radio calls, latency, tokens, judge scores, and `DRIFT:` lines when an agent's model id,
  prompt, tools or output contract changed), then a step table with Kimi calls and tokens
  per step, the replay side by side with last week (`compare_runs` tables), every metric
  side by side, the real-case fields that differ from `baseline.json`, and each agent's
  model and prompt hash.
- `summary.json`: everything the next week diffs against (flat `metrics`, the agent
  `fingerprint`, per-step results, the replay's `run_stats`).
- `real.json`, `golden.json`: the per-case rows; the replay journal (`*-replay.jsonl`) and
  `server.log`.

"Last week" is the newest earlier folder with a `summary.json`; live runs win over dry runs,
so a dry run only stands in until the first live result exists. Latency moves under 15 %
and token moves under 10 % are treated as noise and left out of the headlines (they are
still in the full table). Kimi latency varies with endpoint load, so one week's latency
change is a prompt to look, not a verdict (see B4 in `docs/PITWALL.md`).

## Quota

The Kimi Code key has a rolling 5-hour usage window. A 429 ("usage limit") anywhere stops
the run cleanly: the step that hit it keeps what it finished, the replay is stopped, later
model steps are marked `skipped`, the free offline step still runs and the report opens with
"Stopped on a Kimi 429". The task does not retry; the next Monday is the retry.

## Running it by hand

```bash
python scripts/weekly_eval.py --dry-run           # free: no model call, no server
python scripts/weekly_eval.py --dry-run --replay-journal path/to/run.jsonl
python scripts/weekly_eval.py                     # live (Kimi + Gemini quota)
python scripts/weekly_eval.py --steps real,golden --out-root /tmp/weekly
```

`--dry-run` records the free parts (offline real-case scores, the agents' model ids and
prompt hashes, the golden-set size and, with `--replay-journal`, a journal's statistics), so
it can serve as a baseline. The first one, `2026-10-31-dry-run`, uses the D3 Spa replay
journal (`spa-kimi-run-D3/`) as the replay baseline. Offline tests:
`tests/test_weekly_eval.py`.

## The scheduled task

Name **Weekly live eval**, Mondays 05:45 Europe/London. It fires into the project thread
"Run live evals weekly and report the diff", which checks out the latest `main`, sets up
the venv, runs the script and asks the project coordinator to post the summary (under 80
words, with the `REPORT.md` link) in the project chat. It needs `KIMI_CODE_API_KEY` and
`GEMINI_API_KEY` in the cloud environment's variables.

To change or stop it:

- In the Claude app: Scheduled tasks (claude.ai/code, the clock icon), open **Weekly live
  eval**, then edit the schedule or prompt, pause it, or delete it.
- Or ask in that project thread ("move the weekly eval to Sunday 20:00", "pause the weekly
  eval", "stop the weekly eval"); it edits or deletes its own task.
- To change what runs, edit `scripts/weekly_eval.py` (`STEPS`, `COACH_LAPS`, `HEADLINES`)
  on a branch like any other change; the task always runs the latest `main`.
