/**
 * The live view's onboard panel: the car's camera, live or replayed, and the
 * visual notes the live VLM wrote for the moments the telemetry flagged.
 *
 * Two sources, whichever `GET /api/v1/vision/live` names:
 *   - `capture`: ffmpeg is grabbing the sim window; an <img> shows the server's
 *     MJPEG stream;
 *   - `video`: a replay of a session with a synced recording; a <video> plays
 *     the file and is kept in step with the replay's session time.
 *
 * Visual notes are words only (the server drops any figure), so they are shown
 * as they come, through textContent. `onboardView` is pure and tested in
 * shared.test.js; the class owns the DOM.
 */

import { clear, el, setText } from '../pitwall/dom.js';

const API = '/api/v1';
/** Seek the replay video when it drifts further than this from the session time. */
export const DRIFT_S = 0.4;

export function videoTimeFor(sync, sessionTime) {
  if (!sync || !sync.synced || !Number.isFinite(sessionTime)) return null;
  const rate = sync.rate || 1;
  return (sessionTime - (sync.offset || 0)) / rate;
}

const SKIP_WORDS = {
  merged: 'merged',
  over_cap: 'over the cap',
  no_frames: 'no frames',
};

/** What the panel shows, from the /vision/live body. Pure. */
export function onboardView(status) {
  const s = status || {};
  const capture = s.capture || {};
  const source = s.source || null;
  const vlm = s.vlm || {};
  let mode = 'empty';
  if (capture.running) mode = 'capture';
  else if (source && source.kind === 'video' && source.video) mode = 'video';

  let sourceLine;
  if (mode === 'capture') {
    sourceLine = capture.session_id
      ? `capturing the sim window · ${capture.frames || 0} frames stamped`
      : 'capturing, waiting for a live session to stamp frames';
  } else if (mode === 'video') {
    sourceLine = `recording: ${source.video.label || source.video.video_id}`;
  } else {
    sourceLine = 'No onboard video. Start a capture while iRacing runs, or replay a session that has a synced recording.';
  }

  let vlmLine;
  if (!vlm.enabled) {
    vlmLine = s.mode === 'live'
      ? 'Live VLM off: no Kimi key, or RTV_VISION_LIVE_MAX_CALLS is 0.'
      : 'Live VLM off. Set RTV_VISION_MODE=live to describe flagged moments (uses Kimi quota).';
  } else {
    const parts = [`Live VLM on · ${vlm.calls || 0} of ${vlm.max_calls} calls`];
    if (vlm.pending) parts.push(`${vlm.pending} looking`);
    for (const [why, n] of Object.entries(vlm.skipped || {})) {
      parts.push(`${n} ${SKIP_WORDS[why] || why}`);
    }
    vlmLine = parts.join(' · ');
  }

  const notes = (vlm.recent || [])
    .slice()
    .reverse()
    .map((n) => ({
      id: n.note_id,
      head: `lap ${n.lap ?? '—'} · ${String(n.trigger || '').replace('_', ' ')}` +
        (n.status === 'trigger_not_seen' ? ' · not in frame (check the sync)' : ''),
      lines: (n.kept || []).map((o) => ({
        kind: String(o.kind || '').replace('_', ' '),
        text: o.text,
        cls: `conf-${o.confidence || 'low'}`,
      })),
      empty: (n.kept || []).length
        ? null
        : n.status === 'trigger_not_seen'
          ? 'The frames did not show the flagged moment.'
          : n.status === 'ok'
            ? 'Nothing it could say without a number.'
            : `No note (${n.status}).`,
    }));

  return {
    mode,
    sourceLine,
    vlmLine,
    notes,
    captureRunning: !!capture.running,
    captureError: capture.error || null,
    video: mode === 'video' ? source.video : null,
  };
}

export class OnboardPanel {
  constructor(host, { onNotice } = {}) {
    this.host = host;
    this.onNotice = onNotice || (() => {});
    this.img = host.querySelector('[data-role="onboard-img"]');
    this.video = host.querySelector('[data-role="onboard-video"]');
    this.empty = host.querySelector('[data-role="onboard-empty"]');
    this.sourceLine = host.querySelector('[data-role="onboard-source"]');
    this.vlmLine = host.querySelector('[data-role="onboard-vlm"]');
    this.notes = host.querySelector('[data-role="onboard-notes"]');
    this.toggle = host.querySelector('[data-role="onboard-capture"]');
    this.view = onboardView(null);
    this.videoId = null;
    this.streaming = false;
    this.noteIds = '';
    this.toggle.addEventListener('click', () => this._toggleCapture());
  }

  async poll() {
    try {
      const response = await fetch(`${API}/vision/live`);
      if (!response.ok) return;
      this.render(onboardView(await response.json()));
    } catch (_) {
      // The status strip already says when the server is gone.
    }
  }

  render(view) {
    this.view = view;
    setText(this.sourceLine, view.captureError ? `capture failed: ${view.captureError}` : view.sourceLine);
    setText(this.vlmLine, view.vlmLine);
    setText(this.toggle, view.captureRunning ? '■ Stop capture' : '● Start capture');

    const streaming = view.mode === 'capture';
    if (streaming !== this.streaming) {
      // A fresh query string per start, so the browser opens a new stream.
      if (streaming) this.img.src = `${API}/vision/live/mjpeg?t=${Date.now()}`;
      else this.img.removeAttribute('src');
      this.streaming = streaming;
    }
    this.img.hidden = !streaming;

    const vid = view.video ? view.video.video_id : null;
    if (vid !== this.videoId) {
      if (vid) {
        this.video.src = `${API}/sessions/${encodeURIComponent(view.video.session_id)}/videos/${encodeURIComponent(vid)}/file`;
      } else {
        this.video.pause();
        this.video.removeAttribute('src');
      }
      this.videoId = vid;
    }
    this.video.hidden = !vid;
    this.empty.hidden = streaming || !!vid;

    const ids = view.notes.map((n) => n.id).join('|');
    if (ids !== this.noteIds) {
      this.noteIds = ids;
      clear(this.notes);
      for (const note of view.notes) {
        const item = el('div', 'onboard-note');
        item.append(el('div', 'onboard-note-head', note.head));
        if (note.empty) item.append(el('div', 'muted', note.empty));
        for (const line of note.lines) {
          const row = el('div', `onboard-obs ${line.cls}`);
          row.append(el('span', 'k', line.kind), el('span', '', ` ${line.text}`));
          item.append(row);
        }
        this.notes.append(item);
      }
    }
  }

  /**
   * Keep a replayed recording at the replay's session time (called at 10 Hz).
   * `speed` is the replay's rate (0 = as fast as possible: seek only).
   */
  follow(sessionTime, running, speed) {
    if (!this.view.video || this.video.hidden) return;
    const target = videoTimeFor(this.view.video.sync, sessionTime);
    if (target === null) return;
    const duration = this.view.video.duration_s;
    if (target < 0 || (Number.isFinite(duration) && target > duration)) {
      this.video.pause();
      return;
    }
    if (Math.abs(this.video.currentTime - target) > DRIFT_S) this.video.currentTime = target;
    const rate = Number(speed) > 0 ? Math.min(16, Math.max(0.25, Number(speed))) : null;
    if (rate !== null && this.video.playbackRate !== rate) this.video.playbackRate = rate;
    if (running && rate !== null && this.video.paused) this.video.play().catch(() => {});
    if (!running && !this.video.paused) this.video.pause();
  }

  async _toggleCapture() {
    const action = this.view.captureRunning ? 'stop' : 'start';
    try {
      const response = await fetch(`${API}/vision/live/capture/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        this.onNotice(`Capture ${action} failed: ${body.detail || response.status}`);
      } else if (action === 'stop') {
        const body = await response.json();
        if (body.video) this.onNotice('Capture saved and synced to the session; the review can show it.');
      }
    } catch (err) {
      this.onNotice(`Capture ${action} failed: ${err}`);
    }
    this.poll();
  }
}
