"""Onboard video and the VLM "visual notes" built on it.

Telemetry picks the moment, the VLM describes it. Nothing in this package feeds
the race-state engine or produces a number: a visual note is words about a moment
that a deterministic source (a detector event or a Layer-1 finding) already
flagged, checked against telemetry before it is kept.

* :mod:`rtv.vision.models` -- videos, the time-sync map, moments, notes.
* :mod:`rtv.vision.video` -- ffprobe/ffmpeg: probe a file, grab JPEG frames.
* :mod:`rtv.vision.store` -- the video registry and the vision journal on disk.
* :mod:`rtv.vision.moments` -- which moments to look at, from the race journal
  and the Layer-1 findings, inside the frame budget.
* :mod:`rtv.vision.grounding` -- the number-free rule and telemetry corroboration.
* :mod:`rtv.vision.analyzer` -- one VLM call per moment through :mod:`rtv.llm`.
* :mod:`rtv.vision.live` -- the live seam: a kept note as a ``visual_observation``
  event on the bus.

``docs/VISION.md`` documents the API built on it.
"""

from __future__ import annotations
