"""Pre-loaded track reference data and per-session track facts.

See :mod:`rtv.tracks.reference` for the venue data (named corners from
OpenStreetMap) and :mod:`rtv.tracks.session` for what a session's own header and
trace say about the layout.
"""

from __future__ import annotations

from rtv.tracks.reference import MATCH_RADIUS_M, Segment, TrackReference, get, load_all, resolve
from rtv.tracks.session import corner_map, header_facts, name_at_pct, reference_for

__all__ = [
    "MATCH_RADIUS_M",
    "Segment",
    "TrackReference",
    "corner_map",
    "get",
    "header_facts",
    "load_all",
    "name_at_pct",
    "reference_for",
    "resolve",
]
