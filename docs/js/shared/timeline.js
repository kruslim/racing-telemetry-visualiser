/**
 * A session-time axis with lanes, shared by the live view and the post-race review.
 *
 *   const tl = new Timeline(host, { lanes: [{ id: 'events', label: 'events' }, ...] });
 *   tl.setItems('events', [{ t0: 812.4, label: 'yellow', cls: 'critical', record }]);
 *   tl.follow(120);            // live: the last 120 s, sliding with setNow(t)
 *   tl.setRange(0, 3600);      // review: a fixed window ...
 *   tl.setCursor(1834.2);      // ... with a cursor the user can drag
 *   tl.onSeek = (t) => ...;    // fired while dragging the cursor (scrub mode)
 *   tl.onSelect = (item) => ...;  // click on an item; item.record is what you passed
 *
 * Items are { t0, t1?, label?, cls?, record? }: a point when t1 is absent, a span
 * otherwise. Lap ticks and flag bands are just lanes of spans the caller passes.
 *
 * The geometry (`scaleX`, `layoutLane`, `ticks`) is pure and exported so it can be
 * tested under node; the class is the only part that touches the DOM, and it does
 * so through createElementNS + textContent, never HTML strings.
 */

const SVG = 'http://www.w3.org/2000/svg';
const LANE_H = 22;
const ROW_H = 9;
const AXIS_H = 18;
const LABEL_W = 92;
const POINT_W = 3;

/** Session time to x within [0, width]. */
export function scaleX(t, range, width) {
  const [t0, t1] = range;
  if (!(t1 > t0)) return 0;
  return ((t - t0) / (t1 - t0)) * width;
}

/**
 * Place a lane's items: visible ones only, each with x, w and a row so that
 * overlapping spans stack instead of hiding each other.
 */
export function layoutLane(items, range, width, maxRows = 2) {
  const [r0, r1] = range;
  const out = [];
  const rowEnds = [];
  const sorted = [...(items || [])]
    .filter((it) => it && Number.isFinite(it.t0))
    .sort((a, b) => a.t0 - b.t0);
  for (const item of sorted) {
    const end = Number.isFinite(item.t1) ? item.t1 : item.t0;
    if (end < r0 || item.t0 > r1) continue;
    const x = Math.max(0, scaleX(item.t0, range, width));
    const x2 = Math.min(width, scaleX(end, range, width));
    const w = Math.max(POINT_W, x2 - x);
    let row = rowEnds.findIndex((rowEnd) => rowEnd <= x);
    if (row === -1) row = rowEnds.length < maxRows ? rowEnds.length : maxRows - 1;
    rowEnds[row] = x + w + 1;
    out.push({ item, x, w, row, point: !Number.isFinite(item.t1) });
  }
  return out;
}

/** "Nice" tick positions across a range: 1/2/5 x 10^n seconds. */
export function ticks(range, approxCount = 6) {
  const [t0, t1] = range;
  const span = t1 - t0;
  if (!(span > 0)) return [];
  const raw = span / Math.max(1, approxCount);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || 10 * mag;
  const first = Math.ceil(t0 / step) * step;
  const out = [];
  for (let t = first; t <= t1 + 1e-9; t += step) out.push(Math.round(t * 1000) / 1000);
  return out;
}

/** 3725.4 -> "1:02:05", 125 -> "2:05". */
export function clock(t) {
  if (!Number.isFinite(t)) return '';
  const s = Math.max(0, Math.floor(t));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
}

function node(tag, attrs, text) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  if (text !== undefined) n.textContent = String(text);
  return n;
}

export class Timeline {
  constructor(host, { lanes = [], mode = 'follow', windowS = 120 } = {}) {
    this.host = host;
    this.lanes = lanes;
    this.items = Object.fromEntries(lanes.map((l) => [l.id, []]));
    this.mode = mode;
    this.windowS = windowS;
    this.range = [0, windowS];
    this.now = null;
    this.cursor = null;
    this.onSeek = null;
    this.onSelect = null;
    this.selected = null;
    this._dirty = true;
    this.svg = node('svg', { class: 'timeline', role: 'img' });
    host.append(this.svg);
    this._wireDrag();
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => this.invalidate()).observe(host);
    }
    const loop = () => {
      if (this._dirty) this.render();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  invalidate() {
    this._dirty = true;
  }

  setLanes(lanes) {
    this.lanes = lanes;
    for (const l of lanes) if (!this.items[l.id]) this.items[l.id] = [];
    this.invalidate();
  }

  setItems(laneId, items) {
    this.items[laneId] = items || [];
    this.invalidate();
  }

  addItem(laneId, item, limit = 400) {
    const list = this.items[laneId] || (this.items[laneId] = []);
    list.push(item);
    if (list.length > limit) list.splice(0, list.length - limit);
    this.invalidate();
  }

  /** Live mode: show the last `windowS` seconds and slide with setNow(). */
  follow(windowS = this.windowS) {
    this.mode = 'follow';
    this.windowS = windowS;
    this.invalidate();
  }

  setNow(t) {
    if (!Number.isFinite(t)) return;
    this.now = t;
    if (this.mode === 'follow') this.range = [Math.max(0, t - this.windowS), Math.max(t, this.windowS)];
    this.invalidate();
  }

  /** Review mode: a fixed window and a draggable cursor. */
  setRange(t0, t1) {
    this.mode = 'scrub';
    this.range = [t0, t1];
    this.invalidate();
  }

  setCursor(t) {
    this.cursor = Number.isFinite(t) ? t : null;
    this.invalidate();
  }

  clear() {
    for (const k of Object.keys(this.items)) this.items[k] = [];
    this.invalidate();
  }

  _plotWidth() {
    return Math.max(50, (this.host.clientWidth || 600) - LABEL_W - 8);
  }

  _timeAt(clientX) {
    const box = this.svg.getBoundingClientRect();
    const x = clientX - box.left - LABEL_W;
    const [t0, t1] = this.range;
    return t0 + Math.min(1, Math.max(0, x / this._plotWidth())) * (t1 - t0);
  }

  _wireDrag() {
    let dragging = false;
    const seek = (event) => {
      const t = this._timeAt(event.clientX);
      this.setCursor(t);
      if (this.onSeek) this.onSeek(t);
    };
    this.svg.addEventListener('pointerdown', (event) => {
      if (this.mode !== 'scrub' || event.target.dataset.item) return;
      dragging = true;
      this.svg.setPointerCapture(event.pointerId);
      seek(event);
    });
    this.svg.addEventListener('pointermove', (event) => {
      if (dragging) seek(event);
    });
    this.svg.addEventListener('pointerup', () => {
      dragging = false;
    });
  }

  render() {
    this._dirty = false;
    const svg = this.svg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const width = this._plotWidth();
    const height = AXIS_H + this.lanes.length * LANE_H;
    svg.setAttribute('viewBox', `0 0 ${width + LABEL_W + 8} ${height}`);
    svg.setAttribute('height', String(height));

    const plot = node('g', { transform: `translate(${LABEL_W},0)` });
    for (const t of ticks(this.range, Math.max(3, Math.round(width / 110)))) {
      const x = scaleX(t, this.range, width);
      plot.append(node('line', { class: 'tl-grid', x1: x, x2: x, y1: AXIS_H - 4, y2: height }));
      plot.append(node('text', { class: 'tl-tick', x, y: 11, 'text-anchor': 'middle' }, clock(t)));
    }

    this.lanes.forEach((lane, i) => {
      const y = AXIS_H + i * LANE_H;
      svg.append(node('text', { class: 'tl-lane-label', x: 6, y: y + 15 }, lane.label || lane.id));
      plot.append(node('line', { class: 'tl-lane-line', x1: 0, x2: width, y1: y + LANE_H, y2: y + LANE_H }));
      for (const placed of layoutLane(this.items[lane.id], this.range, width)) {
        const { item, x, w, row, point } = placed;
        const rect = node('rect', {
          class: `tl-item ${point ? 'tl-point' : 'tl-span'} ${item.cls || ''}`.trim(),
          x,
          y: y + 3 + row * ROW_H,
          width: w,
          height: point ? LANE_H - 6 : ROW_H - 1,
          rx: point ? 1 : 2,
        });
        rect.dataset.item = '1';
        if (item === this.selected) rect.classList.add('selected');
        const title = node('title', {}, item.label || '');
        rect.append(title);
        rect.addEventListener('click', () => {
          this.selected = item;
          this.invalidate();
          if (this.onSelect) this.onSelect(item);
        });
        plot.append(rect);
      }
    });

    const marker = this.mode === 'scrub' ? this.cursor : this.now;
    if (Number.isFinite(marker)) {
      const x = scaleX(marker, this.range, width);
      if (x >= 0 && x <= width) {
        plot.append(node('line', { class: 'tl-cursor', x1: x, x2: x, y1: 0, y2: height }));
      }
    }
    svg.append(plot);
  }
}
