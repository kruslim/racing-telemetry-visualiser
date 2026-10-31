"""Design tokens: the single source of the desktop palette, type and spacing.

Mirrors the "Tokens" artboard of the RTV Desktop design canvas. The agent and
severity names match ``frontend/css/pitwall.css`` (``--strategist`` ...) so both
clients speak the same colour language, but the values are the desktop redesign's.
"""

from __future__ import annotations

# Neutrals
BG = "#0a0c0f"
CHROME = "#0e1115"
PANEL = "#11151a"
PANEL_2 = "#171c23"
LINE = "#252c35"
LINE_2 = "#333c47"
ROW_LINE = "#1d232b"
MUTED = "#94a0ad"
SOFT = "#c2cbd4"
INK = "#e8edf2"
ACCENT = "#5cb8ff"
ACCENT_SOFT = "#9bd3ff"
SELECT = "#22303f"
GOOD = "#5fe39a"

# Agents: a stroke colour and a lighter text colour each. Always shown with a name.
AGENT = {
    "strategist": "#b18cff",
    "vehicle_engineer": "#4fa3ff",
    "spotter": "#ff6b9a",
    "coach": "#35c98a",
    "driver": "#e8edf2",
}
AGENT_TEXT = {
    "strategist": "#d4c0ff",
    "vehicle_engineer": "#a9d0ff",
    "spotter": "#ff9fbd",
    "coach": "#8ff0c3",
    "driver": "#e8edf2",
}
AGENT_LABEL = {
    "strategist": "strategist",
    "vehicle_engineer": "vehicle engineer",
    "spotter": "spotter",
    "coach": "coach",
    "driver": "driver",
}

# Severity: never colour alone, always icon + word.
SEVERITY = {"critical": "#ff4f4a", "advisory": "#ffb020", "info": "#8aa0b8"}
SEVERITY_TEXT = {"critical": "#ffb3ae", "advisory": "#ffcf70", "info": "#b9c8d8"}
SEVERITY_BG = {"critical": "#3a1616", "advisory": "#33280f", "info": "#1c2530"}
SEVERITY_ICON = {"critical": "▲", "advisory": "◆", "info": "●"}

# Flags
FLAG = {
    "green": "#2ecc71",
    "yellow": "#ffd400",
    "red": "#e5252a",
    "white": "#f2f4f7",
    "checkered": "#f2f4f7",
    "blue": "#2f6bff",
    "unknown": "#3a4552",
}
FLAG_INK = {"yellow": "#1a1500", "white": "#111111", "checkered": "#111111"}

# Margin bands
BAND = {"green": "#2ecc71", "amber": "#ffb020", "red": "#ff4f4a", "unknown": MUTED}
BAND_BG = {"green": "#10241b", "amber": "#2a2414", "red": "#1d1214", "unknown": PANEL_2}

# Traces
LAP_TRACE = "#5cb8ff"
REF_TRACE = "#ff9a3c"

# Outcome tags: (background, text)
OUTCOME = {
    "radio": ("#1d2a3a", ACCENT_SOFT),
    "spoke": ("#1d2a3a", ACCENT_SOFT),
    "refused": ("#2a2414", "#ffd27a"),
    "error": ("#3a1616", "#ffb3ae"),
    "working": ("#173322", "#8ff0c3"),
    "unfinished": ("#1c222a", SOFT),
    "no output": ("#1c222a", SOFT),
}

# Type
SANS = '"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
MONO = '"Cascadia Mono", "Consolas", monospace'
SANS_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "Arial"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Courier New"]

SIZE_HERO = 34
SIZE_VALUE = 20
SIZE_BODY = 13
SIZE_RADIO = 14
SIZE_LABEL = 11

# Space and shape
SPACE = (4, 8, 12, 16, 24, 32)
RADIUS_PANEL = 8
RADIUS_CONTROL = 6
HIT = 32


def agent_color(agent: str | None) -> str:
    return AGENT.get(agent or "", MUTED)


def agent_text(agent: str | None) -> str:
    return AGENT_TEXT.get(agent or "", SOFT)


def severity_color(severity: str | None) -> str:
    return SEVERITY.get(severity or "info", SEVERITY["info"])
