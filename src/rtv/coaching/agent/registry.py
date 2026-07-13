"""The canonical tool registry for the graph coach — one authority on what tools exist.

The tool set and the descriptions are kept deliberately **parallel** to the Layer-2 MCP
server (``mcp_server/telemetry_coach.py``): the graph drives the same coaching semantics
in-process that the MCP server exposes over HTTP. The two lists are hand-kept in sync for now
(a later refactor could have the MCP server import these descriptions to prevent drift — the
reference project's single-authority ``tools/registry.py`` pattern, once a second consumer
justifies it).

Order is the cost order the model should read them in: cheap orienting calls first
(list_sessions, list_laps, list_available_channels), the primary interpretation call
(get_lap_findings) next, the expensive raw-channel pull (compare_channel) last.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field

from rtv.coaching.agent.provider import FindingsProvider

# A handler takes the injected provider plus a validated input model and returns a plain dict
# (the tool result the model reads). Below-provider raises become error payloads in the
# executor, so handlers stay a straight pass-through.
ToolFn = Callable[[FindingsProvider, BaseModel], dict]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    invoke: ToolFn


# ----------------------------------------------------------------- input models
class ListSessionsInput(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


class ListLapsInput(BaseModel):
    session_id: str


class ListAvailableChannelsInput(BaseModel):
    session_id: str


class GetLapFindingsInput(BaseModel):
    session_id: str
    main_lap: int
    ref_lap: int


class CompareChannelInput(BaseModel):
    session_id: str
    name: str
    laps: list[int]
    grid: int = Field(default=200, ge=10, le=1000)


# ----------------------------------------------------------------- descriptions
LIST_SESSIONS_DESCRIPTION = (
    "List recorded telemetry sessions (most recent first). Call this first to discover which "
    "sessions exist and get their session_id, car, track and label before any other tool."
)
LIST_LAPS_DESCRIPTION = (
    "List the laps in a session with their lap times and validity flags. Use it to choose a "
    "'main' lap to coach and a faster 'reference' lap to compare against (typically the "
    "session's fastest valid lap, or an imported alien lap)."
)
LIST_AVAILABLE_CHANNELS_DESCRIPTION = (
    "List the telemetry channels this session actually captured. Call this before claiming a "
    "channel exists or refusing for missing data — it is the ground truth for what the "
    "telemetry can and cannot answer (e.g. whether tyre temperatures were logged)."
)
GET_LAP_FINDINGS_DESCRIPTION = (
    "THE PRIMARY TOOL — the deterministic corner-by-corner coaching findings for one lap vs a "
    "reference. Call it whenever the driver asks to review a lap, compare against a "
    "reference/fastest lap, find where they are losing time, or what to work on. It returns "
    "~20 compact findings (no raw telemetry): per-corner min-speed deltas, brake points, "
    "throttle, gear, grip and lock-ups, each with apportioned time loss; plus sector deltas "
    "and a 'chief' summary listing the top-3 priority corners and total time available. Base "
    "your coaching on these numbers — do not invent figures."
)
COMPARE_CHANNEL_DESCRIPTION = (
    "Compare one raw channel (e.g. Speed, Throttle, Brake, Gear) across laps, aligned on lap "
    "distance (0..1). Use this only when get_lap_findings is not enough and you need the "
    "actual trace shape. Keep grid small (100-300) to stay token-light."
)


# ----------------------------------------------------------------- handlers
def _list_sessions(provider: FindingsProvider, inp: ListSessionsInput) -> dict:
    return {"sessions": provider.list_sessions(inp.limit)}


def _list_laps(provider: FindingsProvider, inp: ListLapsInput) -> dict:
    return {"session_id": inp.session_id, "laps": provider.list_laps(inp.session_id)}


def _list_available_channels(provider: FindingsProvider, inp: ListAvailableChannelsInput) -> dict:
    return {
        "session_id": inp.session_id,
        "channels": provider.available_channels(inp.session_id),
    }


def _get_lap_findings(provider: FindingsProvider, inp: GetLapFindingsInput) -> dict:
    # The result IS the LapFindings dict; the tools node harvests it as ground truth.
    return provider.lap_findings(inp.session_id, inp.main_lap, inp.ref_lap)


def _compare_channel(provider: FindingsProvider, inp: CompareChannelInput) -> dict:
    return provider.compare_channel(inp.session_id, inp.name, inp.laps, inp.grid)


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("list_sessions", LIST_SESSIONS_DESCRIPTION, ListSessionsInput, _list_sessions),
    ToolSpec("list_laps", LIST_LAPS_DESCRIPTION, ListLapsInput, _list_laps),
    ToolSpec(
        "list_available_channels",
        LIST_AVAILABLE_CHANNELS_DESCRIPTION,
        ListAvailableChannelsInput,
        _list_available_channels,
    ),
    ToolSpec(
        "get_lap_findings", GET_LAP_FINDINGS_DESCRIPTION, GetLapFindingsInput, _get_lap_findings
    ),
    ToolSpec(
        "compare_channel", COMPARE_CHANNEL_DESCRIPTION, CompareChannelInput, _compare_channel
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}

GET_LAP_FINDINGS_TOOL = "get_lap_findings"
LIST_CHANNELS_TOOL = "list_available_channels"
