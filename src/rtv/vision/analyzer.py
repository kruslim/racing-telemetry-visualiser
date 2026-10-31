"""One VLM call per moment: frames in, a grounded :class:`VisualNote` out.

The call goes through the same :class:`~rtv.pitwall.provider.LLMProvider`
interface as every other layer (``KimiProvider`` in the app, ``ScriptedProvider``
in tests), with the frames as Anthropic base64 image blocks built by
:func:`rtv.llm.image_block` and the answer forced through the respond tool as a
:class:`~rtv.vision.models.VisionAnswer`. Thinking stays off, as for the pitwall
agents: kimi-k3 rejects a named ``tool_choice`` while it thinks.

What the model is shown about the moment is number-free: why the telemetry
flagged it ("the car went off track", "time was lost through T7") and nothing
else. No raw telemetry, no lap times, no gains.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rtv import llm
from rtv.logging import get_logger
from rtv.pitwall.provider import LLMProvider
from rtv.vision.grounding import ground
from rtv.vision.models import FrameRef, Moment, VideoInfo, VisionAnswer, VisualNote
from rtv.vision.store import RunWriter, VisionStore
from rtv.vision.video import FrameSource

log = get_logger("vision.analyzer")

AGENT_NAME = "vision"

VISION_PROMPT = """\
You look at still frames from a sim-racing onboard camera (iRacing) and describe
what they show, for a driver reviewing the race afterwards.

The telemetry already knows every number: speed, gear, inputs, lap distance, time
lost. Never give a number, a distance, a speed, a time, a gear or a count, not even
in words. Describe only what the camera sees and the channels cannot: where the car
is against the kerbs, track edges, apex and exit; whether it used or ran over a
kerb; what other cars nearby are doing; visible damage; dust, debris, marbles or
wet patches; visible car behaviour such as a slide, a twitch or tyre smoke.

The frames are in time order and span a few seconds around the moment the
telemetry flagged. Do not refer to frames by number. If the frames plainly show
something other than that moment (the pit lane, a replay menu, a different part of
the track), say so with trigger_visible 'no' and add no observations. If you cannot
tell, use 'unclear'. Say less rather than guess: an empty list is a good answer.
"""


def frame_times(moment: Moment, n: int) -> list[float]:
    """``n`` session times spread evenly across the moment's window, ends included."""
    if n <= 1:
        return [moment.session_time]
    step = (moment.t_end - moment.t_start) / (n - 1)
    return [moment.t_start + i * step for i in range(n)]


def _brief(moment: Moment) -> str:
    seen: list[str] = []
    for trig in sorted(moment.triggers, key=lambda t: t.session_time):
        if trig.label and trig.label not in seen:
            seen.append(trig.label)
    where = f" The corner is {moment.corner}." if moment.corner else ""
    return "The telemetry flagged this moment: " + "; ".join(seen) + "." + where


class VisionAnalyzer:
    def __init__(
        self,
        provider: LLMProvider,
        frames: FrameSource,
        store: VisionStore,
        *,
        model: str | None = None,
        frames_per_moment: int = 4,
        frame_width: int = 768,
        max_tokens: int = 900,
    ) -> None:
        self._provider = provider
        self._frames = frames
        self._store = store
        self._model = model or llm.model("reasoning")
        self.frames_per_moment = frames_per_moment
        self.frame_width = frame_width
        self.max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    async def analyze_moment(
        self, video: VideoInfo, moment: Moment, analysis_id: str
    ) -> VisualNote:
        note = VisualNote(
            note_id=f"{analysis_id}:{moment.moment_id}",
            analysis_id=analysis_id,
            video_id=video.video_id,
            moment=moment,
            model=self._model,
        )
        folder = self._store.frames_dir(video.session_id, analysis_id)
        folder.mkdir(parents=True, exist_ok=True)
        content: list[dict[str, Any]] = [{"type": "text", "text": _brief(moment)}]
        try:
            for i, st in enumerate(frame_times(moment, self.frames_per_moment)):
                vt = video.sync.to_video(st)
                jpeg = await asyncio.to_thread(
                    self._frames.frame, Path(video.path), vt, width=self.frame_width
                )
                name = f"{moment.moment_id}-{i}.jpg"
                (folder / name).write_bytes(jpeg)
                note.frames.append(
                    FrameRef(name=name, video_time=round(vt, 3), session_time=round(st, 3))
                )
                content.append(llm.image_block(jpeg))
        except Exception as exc:  # a bad frame costs one moment, not the analysis
            log.warning("Vision: no frames for %s: %s", moment.moment_id, exc)
            note.status, note.error = "error", f"frames: {exc}"
            return note
        content.append(
            {"type": "text", "text": "Describe what these frames show, in the required structure."}
        )

        started = time.monotonic()
        answer: VisionAnswer | None = None
        try:
            for _attempt in range(2):  # one retry on an invalid respond payload
                response = await self._provider.complete(
                    agent=AGENT_NAME,
                    model=self._model,
                    system=[{"type": "text", "text": VISION_PROMPT}],
                    messages=[{"role": "user", "content": content}],
                    tools=(),
                    output_model=VisionAnswer,
                    max_tokens=self.max_tokens,
                )
                if isinstance(response.output, VisionAnswer):
                    answer = response.output
                    break
        except Exception as exc:
            log.warning("Vision call failed for %s: %s", moment.moment_id, exc)
            note.status, note.error = "error", f"model: {type(exc).__name__}: {exc}"[:300]
            return note
        finally:
            note.latency_ms = int((time.monotonic() - started) * 1000)

        if answer is None:
            note.status = "no_answer"
            return note
        note.trigger_visible = answer.trigger_visible
        note.status, note.observations = ground(answer, moment)
        return note

    async def run(
        self,
        video: VideoInfo,
        moments: list[Moment],
        writer: RunWriter,
        *,
        on_note: Callable[[VisualNote], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[VisualNote]:
        notes: list[VisualNote] = []
        for moment in moments:
            if cancelled is not None and cancelled():
                break
            note = await self.analyze_moment(video, moment, writer.analysis_id)
            writer.note(note)
            notes.append(note)
            if on_note is not None:
                on_note(note)
        return notes
