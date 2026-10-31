"""Onboard video and VLM visual-note endpoints. See ``docs/VISION.md``.

Free: registering, uploading, syncing and streaming a video, single frames, the
moment plan (a dry run of an analysis) and reading stored notes. Paid (Kimi Code
quota): ``POST .../vision/analyses``, which needs ``RTV_VISION_MODE`` set to
``post`` or ``live`` and a key.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from rtv.api.deps import get_vision
from rtv.vision.models import SyncAnchor
from rtv.vision.moments import SOURCES
from rtv.vision.service import VisionDisabled, VisionService, VisionUnavailable
from rtv.vision.store import check_id, safe_filename
from rtv.vision.video import VIDEO_EXTENSIONS, VideoToolError

router = APIRouter(tags=["vision"])


class LapStart(BaseModel):
    lap: int = Field(ge=0, description="RTV lap number, as /sessions/{id}/laps lists it.")
    video_time: float = Field(
        ge=0.0, description="Video time (s) the car crosses the line to start that lap."
    )


class SyncBody(BaseModel):
    """Exactly one of the three."""

    offset_s: float | None = Field(None, description="Session time at the video's first frame (s).")
    anchors: list[SyncAnchor] | None = Field(
        None, description="Known (video_time, session_time) pairs; two or more fit drift."
    )
    lap_starts: list[LapStart] | None = Field(
        None, description="Line crossings seen in the video, by lap."
    )


class RegisterBody(BaseModel):
    path: str = Field(description="Path of the video on the server's disk.")
    label: str | None = None
    sync: SyncBody | None = None


class AnalysisBody(BaseModel):
    video_id: str
    sources: list[str] = Field(default_factory=lambda: list(SOURCES))
    laps: list[int] | None = Field(None, description="Laps for finding moments; default all.")
    ref_lap: int | None = Field(None, description="Reference lap; default fastest valid lap.")
    event_run: str | None = Field(None, description="Race-journal run; default newest.")
    max_moments: int | None = Field(None, ge=1, description="Capped by the frame budget.")


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'\""))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, VisionDisabled):
        return HTTPException(409, str(exc))
    if isinstance(exc, VisionUnavailable | VideoToolError):
        return HTTPException(503, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    raise exc


def _sync_kwargs(body: SyncBody) -> dict:
    return {
        "offset_s": body.offset_s,
        "anchors": body.anchors,
        "lap_starts": [(s.lap, s.video_time) for s in body.lap_starts]
        if body.lap_starts is not None
        else None,
    }


@router.get("/vision/status", summary="Vision mode, key, ffmpeg and the frame budget")
def vision_status(vision: VisionService = Depends(get_vision)) -> dict:
    return vision.status()


# ---- live -----------------------------------------------------------------


class CaptureBody(BaseModel):
    session_id: str | None = Field(
        None, description="Session to stamp and register under; default the live one."
    )


@router.get("/vision/live", summary="Live onboard: capture, frame source and the live VLM")
def live_status(vision: VisionService = Depends(get_vision)) -> dict:
    return vision.live_status()


@router.post("/vision/live/capture/start", summary="Start capturing the sim window (free)")
def capture_start(
    body: CaptureBody | None = None, vision: VisionService = Depends(get_vision)
) -> dict:
    try:
        return vision.start_capture((body or CaptureBody()).session_id)
    except RuntimeError as exc:
        if isinstance(exc, VideoToolError):
            raise _errors(exc) from exc
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise _errors(exc) from exc


@router.post(
    "/vision/live/capture/stop", summary="Stop capturing; register the recording, synced"
)
def capture_stop(vision: VisionService = Depends(get_vision)) -> dict:
    return vision.stop_capture()


@router.get("/vision/live/frame", summary="The newest live frame (image/jpeg; 204 when none)")
def live_frame(vision: VisionService = Depends(get_vision)) -> Response:
    jpeg = vision.latest_frame()
    if jpeg is None:
        return Response(status_code=204)
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


MJPEG_BOUNDARY = "rtvframe"


@router.get(
    "/vision/live/mjpeg",
    summary="The live frames as multipart MJPEG, for an <img> element",
)
async def live_mjpeg(
    request: Request,
    max_frames: int | None = Query(None, ge=1, description="End after this many (tests)."),
    vision: VisionService = Depends(get_vision),
) -> StreamingResponse:
    period = 1.0 / vision.settings.vision_capture_preview_fps

    async def frames():
        # Poll the newest frame at the preview rate: it covers stamped frames and
        # the unstamped ones a capture shows before a live session starts.
        last: bytes | None = None
        sent = 0
        while max_frames is None or sent < max_frames:
            if await request.is_disconnected():
                return
            jpeg = vision.latest_frame()
            if jpeg is None or jpeg is last:
                await asyncio.sleep(period)
                continue
            last, sent = jpeg, sent + 1
            yield (
                f"--{MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n\r\n"
            ).encode() + jpeg + b"\r\n"

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )


# ---- videos ---------------------------------------------------------------


@router.get("/sessions/{session_id}/videos", summary="Onboard videos registered for a session")
def list_videos(session_id: str, vision: VisionService = Depends(get_vision)) -> dict:
    try:
        check_id(session_id)
        videos = vision.store.list_videos(session_id)
    except ValueError as exc:
        raise _errors(exc) from exc
    return {"session_id": session_id, "videos": [v.to_api() for v in videos]}


@router.post("/sessions/{session_id}/videos", status_code=201, summary="Register a video by path")
def register_video(
    session_id: str, body: RegisterBody, vision: VisionService = Depends(get_vision)
) -> dict:
    try:
        video = vision.register_video(session_id, Path(body.path), label=body.label)
        if body.sync is not None:
            video = vision.set_sync(session_id, video.video_id, **_sync_kwargs(body.sync))
    except Exception as exc:
        raise _errors(exc) from exc
    return video.to_api()


@router.post(
    "/sessions/{session_id}/videos/upload",
    status_code=201,
    summary="Upload a video (raw bytes as the body) and register it",
)
async def upload_video(
    session_id: str,
    request: Request,
    filename: str = Query(..., description="Original file name, e.g. 'spa onboard.mp4'."),
    label: str | None = None,
    vision: VisionService = Depends(get_vision),
) -> dict:
    name = safe_filename(filename)
    if not name.lower().endswith(VIDEO_EXTENSIONS):
        raise HTTPException(
            400, f"Only video files can be uploaded ({', '.join(VIDEO_EXTENSIONS)})."
        )
    try:
        check_id(session_id)
        if vision.repo.get_session(session_id) is None:
            raise KeyError(f"Unknown session {session_id}")
    except Exception as exc:
        raise _errors(exc) from exc
    folder = vision.store.upload_dir(session_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid.uuid4().hex[:8]}_{name}"
    size = 0
    with target.open("wb") as fh:
        async for chunk in request.stream():
            fh.write(chunk)
            size += len(chunk)
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "The request body was empty; send the file's bytes.")
    try:
        video = vision.register_video(
            session_id, target, label=label or Path(filename).stem, uploaded=True
        )
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise _errors(exc) from exc
    return video.to_api()


@router.get("/sessions/{session_id}/videos/{video_id}", summary="One registered video")
def get_video(session_id: str, video_id: str, vision: VisionService = Depends(get_vision)) -> dict:
    try:
        return vision.get_video(session_id, video_id).to_api()
    except Exception as exc:
        raise _errors(exc) from exc


@router.delete(
    "/sessions/{session_id}/videos/{video_id}",
    summary="Unregister a video (an uploaded file is deleted; a registered path is not)",
)
def delete_video(
    session_id: str, video_id: str, vision: VisionService = Depends(get_vision)
) -> dict:
    try:
        gone = vision.store.delete_video(session_id, check_id(video_id))
    except Exception as exc:
        raise _errors(exc) from exc
    if gone is None:
        raise HTTPException(404, f"Unknown video {video_id} for session {session_id}")
    return {"deleted": video_id, "file_deleted": gone.uploaded}


@router.put("/sessions/{session_id}/videos/{video_id}/sync", summary="Set how the video lines up")
def set_sync(
    session_id: str,
    video_id: str,
    body: SyncBody,
    vision: VisionService = Depends(get_vision),
) -> dict:
    try:
        return vision.set_sync(session_id, video_id, **_sync_kwargs(body)).to_api()
    except Exception as exc:
        raise _errors(exc) from exc


@router.get(
    "/sessions/{session_id}/videos/{video_id}/time",
    summary="Convert between video time and session time",
)
def convert_time(
    session_id: str,
    video_id: str,
    session_time: float | None = None,
    video_time: float | None = None,
    vision: VisionService = Depends(get_vision),
) -> dict:
    try:
        video = vision.get_video(session_id, video_id)
    except Exception as exc:
        raise _errors(exc) from exc
    if not video.sync.synced:
        raise HTTPException(409, "The video is not synced yet.")
    if (session_time is None) == (video_time is None):
        raise HTTPException(400, "Give exactly one of session_time or video_time.")
    if session_time is not None:
        video_time = video.sync.to_video(session_time)
    else:
        session_time = video.sync.to_session(video_time)
    inside = video.duration_s is None or 0.0 <= video_time <= video.duration_s
    return {"session_time": session_time, "video_time": video_time, "in_video": inside}


@router.get(
    "/sessions/{session_id}/videos/{video_id}/file",
    summary="The video file itself (supports HTTP Range, for a <video> element)",
)
def video_file(session_id: str, video_id: str, vision: VisionService = Depends(get_vision)):
    try:
        video = vision.get_video(session_id, video_id)
    except Exception as exc:
        raise _errors(exc) from exc
    path = Path(video.path)
    if not path.is_file():
        raise HTTPException(404, f"The video file is gone: {path.name}")
    return FileResponse(path, filename=path.name)


@router.get(
    "/sessions/{session_id}/videos/{video_id}/frame",
    summary="One JPEG frame at a session time (or video time)",
    response_class=Response,
)
def video_frame(
    session_id: str,
    video_id: str,
    session_time: float | None = None,
    video_time: float | None = None,
    width: int = Query(640, ge=64, le=3840),
    vision: VisionService = Depends(get_vision),
) -> Response:
    try:
        video = vision.get_video(session_id, video_id)
        if (session_time is None) == (video_time is None):
            raise ValueError("Give exactly one of session_time or video_time.")
        if session_time is not None:
            if not video.sync.synced:
                raise ValueError("The video is not synced yet; ask by video_time.")
            video_time = video.sync.to_video(session_time)
        if video_time < 0 or (video.duration_s is not None and video_time > video.duration_s):
            raise HTTPException(416, "That time is outside the video.")
        jpeg = vision.frames.frame(Path(video.path), video_time, width=width)
    except HTTPException:
        raise
    except Exception as exc:
        raise _errors(exc) from exc
    return Response(jpeg, media_type="image/jpeg")


# ---- analyses ---------------------------------------------------------------


def _plan_kwargs(body: AnalysisBody) -> dict:
    return {
        "sources": tuple(body.sources),
        "laps": body.laps,
        "ref_lap": body.ref_lap,
        "event_run": body.event_run,
        "max_moments": body.max_moments,
    }


@router.post(
    "/sessions/{session_id}/vision/plan",
    summary="Dry run: the moments an analysis would look at, and its frame count (free)",
)
def plan(session_id: str, body: AnalysisBody, vision: VisionService = Depends(get_vision)) -> dict:
    try:
        result = vision.plan(session_id, body.video_id, **_plan_kwargs(body))
    except Exception as exc:
        raise _errors(exc) from exc
    return {
        "session_id": session_id,
        "video_id": body.video_id,
        "model": vision.model,
        **result.to_api(vision.settings.vision_frames_per_moment),
    }


@router.post(
    "/sessions/{session_id}/vision/analyses",
    status_code=202,
    summary="Run the VLM over the planned moments in the background (uses Kimi quota)",
)
def start_analysis(
    session_id: str, body: AnalysisBody, vision: VisionService = Depends(get_vision)
) -> dict:
    try:
        return vision.start_analysis(session_id, body.video_id, **_plan_kwargs(body))
    except Exception as exc:
        raise _errors(exc) from exc


@router.get("/sessions/{session_id}/vision/analyses", summary="Vision analyses of a session")
def list_analyses(session_id: str, vision: VisionService = Depends(get_vision)) -> dict:
    try:
        check_id(session_id)
        runs = vision.list_analyses(session_id)
    except Exception as exc:
        raise _errors(exc) from exc
    return {
        "session_id": session_id,
        "default_analysis": vision.store.default_run(session_id),
        "analyses": runs,
    }


@router.get(
    "/sessions/{session_id}/vision/analyses/{analysis_id}",
    summary="One analysis: its status and every note, kept or not",
)
def get_analysis(
    session_id: str, analysis_id: str, vision: VisionService = Depends(get_vision)
) -> dict:
    try:
        notes = vision.store.notes(session_id, analysis_id)
        path = vision.store.run_path(session_id, analysis_id)
    except Exception as exc:
        raise _errors(exc) from exc
    from rtv.vision.store import summarize_run

    summary = summarize_run(path)
    job = vision.job(analysis_id)
    if job is not None:
        summary["status"] = job["status"]
        summary["progress"] = {"done": job["done"], "total": job["total"]}
    return {"session_id": session_id, **summary, "items": [n.to_api() for n in notes]}


@router.post(
    "/sessions/{session_id}/vision/analyses/{analysis_id}/cancel",
    summary="Stop a running analysis after the moment in flight",
)
def cancel_analysis(
    session_id: str, analysis_id: str, vision: VisionService = Depends(get_vision)
) -> dict:
    if not vision.cancel(analysis_id):
        raise HTTPException(404, f"No running analysis {analysis_id}")
    return {"analysis_id": analysis_id, "cancelling": True}


@router.get(
    "/sessions/{session_id}/vision/notes",
    summary="Visual notes of a session, for the review timeline and corner panels",
)
def get_notes(
    session_id: str,
    analysis: str | None = Query(None, description="Analysis id; default newest complete."),
    lap: int | None = None,
    corner: str | None = Query(None, description="Layer-1 corner label, e.g. T7."),
    t_from: float | None = Query(None, alias="from", description="Session time, s."),
    t_to: float | None = Query(None, alias="to", description="Session time, s."),
    include_dropped: bool = Query(
        False, description="Also return notes with nothing kept (errors, all dropped)."
    ),
    vision: VisionService = Depends(get_vision),
) -> dict:
    try:
        analysis_id, notes = vision.notes(
            session_id, analysis_id=analysis, lap=lap, corner=corner, t_from=t_from, t_to=t_to
        )
    except Exception as exc:
        raise _errors(exc) from exc
    items = [n.to_api() for n in notes if include_dropped or n.kept]
    return {
        "session_id": session_id,
        "analysis_id": analysis_id,
        "count": len(items),
        "items": items,
    }


@router.get(
    "/sessions/{session_id}/vision/coach-notes",
    summary="Kept visual notes in the compact shape the coach and MCP server read",
)
def get_coach_notes(
    session_id: str, lap: int | None = None, vision: VisionService = Depends(get_vision)
) -> dict:
    try:
        check_id(session_id)
        if vision.repo.get_session(session_id) is None:
            raise KeyError(f"Unknown session {session_id}")
    except Exception as exc:
        raise _errors(exc) from exc
    return vision.coach_notes(session_id, lap)


@router.get(
    "/sessions/{session_id}/vision/frames/{analysis_id}/{name}",
    summary="A frame an analysis sent to the VLM",
    response_class=FileResponse,
)
def analysis_frame(
    session_id: str, analysis_id: str, name: str, vision: VisionService = Depends(get_vision)
):
    try:
        check_id(name)
        folder = vision.store.frames_dir(session_id, analysis_id)
    except Exception as exc:
        raise _errors(exc) from exc
    path = folder / name
    if not name.endswith(".jpg") or not path.is_file():
        raise HTTPException(404, f"No frame {name} in analysis {analysis_id}")
    return FileResponse(path, media_type="image/jpeg")
