# AI Coaching

A deliberately **tiered** coaching system. The core insight: the existing `coach.js`
"agents" are *heuristics*, and they should stay that way — deterministic code finds brake
points and speed deltas faster, cheaper and more reliably than any LLM. Claude is used only
where code is weak: synthesis, prioritisation, explanation and open-ended Q&A.

It never feeds raw 60 Hz telemetry (~1M points/lap) to an LLM. A deterministic
feature-extraction layer distills each lap to ~20 corner findings (a few KB) first — that is
both the token solution and the eval ground truth.

## Layers

| Layer | Code | What it is | Cost |
|---|---|---|---|
| 1. Features | `src/rtv/coaching/`, `GET /coaching/lap-findings` | `coach.js` heuristics ported to Python → `LapFindings` | free |
| 2. MCP server | `mcp_server/telemetry_coach.py` | Tools over the API; chat-driven coaching in Claude Desktop/Code | **$0** (subscription) |
| 3. Orchestration + evals | `src/rtv/coaching/orchestrator.py`, `evals/` | Multi-agent Claude pipeline + eval harness | ~cents (API) |

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

## Layer 3 — orchestration + evals (the showcase)

```
pip install -e ".[ai]"
$env:ANTHROPIC_API_KEY = "sk-ant-..."

python scripts/deep_coach.py <session_id> <main_lap> <ref_lap>              # full pipeline
python scripts/deep_coach.py <session_id> <main_lap> <ref_lap> --findings-only   # no key needed
python evals/run.py                                                        # eval harness
```

`CoachOrchestrator` fans out one specialist per priority corner (in parallel), synthesises a
single plan, then adversarially verifies it — all with structured outputs and a cached findings
block (prompt caching). The eval harness (`evals/checks.py`) cross-checks the coach's figures
against the findings, so hallucinations are caught **deterministically** — factuality is a numeric
comparison, not an LLM-judge vibe. An optional LLM judge scores clarity/actionability/grounding.

## Tests

All offline (no key, no iRacing): `python -m pytest` covers feature extraction, the MCP transform
logic, the orchestration flow (stubbed client), and the eval ground-truth checks (including catching
an injected hallucination).
