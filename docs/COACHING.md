# AI Coaching

A deliberately **tiered** coaching system. The core insight: the existing `coach.js`
"agents" are *heuristics*, and they should stay that way — deterministic code finds brake
points and speed deltas faster, cheaper and more reliably than any LLM. Claude is used only
where code is weak: synthesis, prioritisation, explanation and open-ended Q&A.

It never feeds raw 60 Hz telemetry (~1M points/lap) to an LLM. A deterministic
feature-extraction layer distills each lap to ~20 corner findings (a few KB) first — that is
both the token solution and the eval ground truth.

> **Looking for the flow diagram?** [`AI_FLOW.md`](./AI_FLOW.md) has the
> end-to-end diagrams — the LangGraph state machine, the orchestrator pipeline,
> every tool call and its parameters, and a fully worked example.

## Layers

| Layer | Code | What it is | Cost |
|---|---|---|---|
| 1. Features | `src/rtv/coaching/`, `GET /coaching/lap-findings` | `coach.js` heuristics ported to Python → `LapFindings` | free |
| 2. MCP server | `mcp_server/telemetry_coach.py` | Tools over the API; chat-driven coaching in Claude Desktop/Code | **$0** (subscription) |
| 3a. Orchestration | `src/rtv/coaching/orchestrator.py` | Code-controlled multi-agent Claude pipeline (fan-out → synthesise → verify) | ~cents (API) |
| 3b. Graph agent + evals | `src/rtv/coaching/agent/`, `evals/` | LangGraph tool-calling coach: grounded refusal + in-loop citation validator, replayed by a hermetic regression harness | ~cents (API) |

## Layer 1 — deterministic features

`CoachingService.lap_findings(session_id, main_lap, ref_lap)` reuses `repo.compare()` to align
the main and reference laps on a uniform lap-distance grid, detects corners as prominent speed
minima, builds an elapsed-time delta curve scaled to the API lap time, and runs the eight
diagnostics (speed, brake, lock-up, throttle, gear, grip, consistency, sector). Output is the
compact `LapFindings` model. Requires `Speed` + `LapDist`; missing channels are skipped and noted.

```
GET /api/v1/coaching/lap-findings?session_id=...&main_lap=5&ref_lap=3
```

## Layer 2 — MCP server (start here)

```
pip install -e ".[mcp]"
.\run.ps1                         # backend must be running
```

Register in **Claude Code** (project `.mcp.json` is already included), or **Claude Desktop**
(`claude_desktop_config.json`). Then chat: *"Review my lap 5 vs my fastest"*,
*"Why am I slow in turn 4?"*. Claude orchestrates the tool calls; the deterministic
`get_lap_findings` keeps it grounded. No per-token bill — it runs on your subscription.

## Layer 3a — orchestration (the multi-agent showcase)

```
pip install -e ".[ai]"
$env:ANTHROPIC_API_KEY = "sk-ant-..."

python scripts/deep_coach.py <session_id> <main_lap> <ref_lap>              # full pipeline
python scripts/deep_coach.py <session_id> <main_lap> <ref_lap> --findings-only   # no key needed
python evals/run.py                                                        # orchestrator eval harness
```

`CoachOrchestrator` fans out one specialist per priority corner (in parallel), synthesises a
single plan, then adversarially verifies it — all with structured outputs and a cached findings
block (prompt caching). The eval harness (`evals/checks.py`) cross-checks the coach's figures
against the findings, so hallucinations are caught **deterministically** — factuality is a numeric
comparison, not an LLM-judge vibe. An optional LLM judge scores clarity/actionability/grounding.

## Layer 3b — the graph agent (`src/rtv/coaching/agent/`)

Where 3a is a fixed code-controlled pipeline, 3b is a **LangGraph** state graph the *model*
drives — a tool-calling loop over the same `LapFindings` ground truth, with two behaviours that
make it more than a chat wrapper:

- **Grounded refusal.** Ask for a channel the session never captured (tyre temperatures, say) and
  the coach returns a structured `CoachRefusal` listing the channels that *are* available — filled
  by code from a tool result, never from the model — instead of inventing a number. Refusal is a
  first-class, correct outcome (`refuse` is its own tool).
- **In-loop citation validation.** Every figure a claim asserts (a corner's `net_dt`, a min speed)
  must trace to a finding that was actually retrieved. A claimed time gain the findings don't
  support is bounced back as a validation turn (`validation.check_figures`, shared with the 3a
  eval checker); after bounded retries it degrades to an honest "couldn't determine" — never a
  crash, never a guess.

```
pip install -e ".[agent]"          # or just ".[dev]" — the tests need no key

# the graph: agent -> tools -> validate -> refuse, with forced-answer termination
python -c "from rtv.coaching.agent import run_coach"
```

The graph's tools call **in-process** into `CoachingService`/`Repository` (not over HTTP like the
MCP server), which is what lets the eval harness run offline. That harness (`evals/`) is a
canopy-style flywheel: named hermetic fixtures (`evals/fixtures.py`), a racing failure taxonomy
(`evals/schemas.py::ErrorType`), a replayable `CoachTrace`, hard assertions, a seed case set
(`evals/cases.py`), and a **baseline-diffing regression runner** (`evals/runner.py`) where a case
that regresses is a merge-blocker. The HITL review gate and a calibrated judge are the next step.

## Tests

All offline (no key, no iRacing): `python -m pytest` covers feature extraction, the MCP transform
logic, the 3a orchestration flow (stubbed client), the 3a eval ground-truth checks (including
catching an injected hallucination), and the 3b graph coach driven by a **scripted model** —
grounded refusal, the in-loop validator bouncing a bad figure, iteration-cap termination, the
trace, the fixtures, and the regression runner's baseline diff.
