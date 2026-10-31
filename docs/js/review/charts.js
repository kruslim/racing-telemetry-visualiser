/**
 * Canvas charts for the review: stacked lap traces on one distance axis, and the
 * track map. Drawing only; every number arrives from the API already computed.
 *
 *   const traces = new TraceStack(canvas, { onHover: (m) => ... });
 *   traces.setData({ x, series: [{ id, label, unit, main, ref }], delta, lapLength });
 *   traces.highlight([entry_m, exit_m]);   // a corner from the findings
 *   traces.setHover(metres);               // crosshair, e.g. from the map
 */

import { cssVar } from '../pitwall/dom.js';

const PAD_L = 64;
const PAD_R = 12;
const ROW_GAP = 8;
const AXIS_H = 18;

function setupCanvas(canvas, height) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || canvas.parentElement.clientWidth || 600;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width, height };
}

function extent(arrays) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const arr of arrays) {
    for (const v of arr || []) {
      if (v == null || !Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!Number.isFinite(lo)) return [0, 1];
  if (hi - lo < 1e-9) return [lo - 1, hi + 1];
  const pad = (hi - lo) * 0.06;
  return [lo - pad, hi + pad];
}

function nearestIndex(xs, x) {
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < x) lo = mid;
    else hi = mid;
  }
  return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
}

export class TraceStack {
  constructor(canvas, { rowHeight = 92, onHover } = {}) {
    this.canvas = canvas;
    this.rowHeight = rowHeight;
    this.onHover = onHover || null;
    this.data = null;
    this.span = null;
    this.hover = null;
    canvas.addEventListener('mousemove', (event) => {
      if (!this.data) return;
      const box = canvas.getBoundingClientRect();
      const m = this._metresAt(event.clientX - box.left, box.width);
      this.setHover(m);
      if (this.onHover) this.onHover(m);
    });
    canvas.addEventListener('mouseleave', () => {
      this.setHover(null);
      if (this.onHover) this.onHover(null);
    });
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => this.draw()).observe(canvas.parentElement);
    }
  }

  /** data: { x: metres[], series: [{id,label,unit,main[],ref[]}], delta: {x[], y[]}, lapLength } */
  setData(data) {
    this.data = data;
    this.draw();
  }

  highlight(span) {
    this.span = span;
    this.draw();
  }

  setHover(metres) {
    this.hover = Number.isFinite(metres) ? metres : null;
    this.draw();
  }

  _rows() {
    const rows = [...(this.data.series || [])];
    if (this.data.delta && this.data.delta.x.length) {
      rows.unshift({ id: 'delta', label: 'delta', unit: 's', delta: true });
    }
    return rows;
  }

  _metresAt(px, width) {
    const plotW = width - PAD_L - PAD_R;
    const t = Math.min(1, Math.max(0, (px - PAD_L) / plotW));
    return t * (this.data.lapLength || 1);
  }

  draw() {
    if (!this.data) return;
    const rows = this._rows();
    const height = AXIS_H + rows.length * (this.rowHeight + ROW_GAP);
    const { ctx, width } = setupCanvas(this.canvas, height);
    const plotW = width - PAD_L - PAD_R;
    const len = this.data.lapLength || 1;
    const sx = (m) => PAD_L + (m / len) * plotW;
    const colours = {
      main: cssVar('--accent', '#2f81f7'),
      ref: cssVar('--advisory', '#d29922'),
      label: cssVar('--muted', '#8b949e'),
      line: cssVar('--line', '#262d38'),
      ink: cssVar('--ink', '#e6edf3'),
      gain: cssVar('--info', '#3fb950'),
      loss: cssVar('--critical', '#f85149'),
      span: 'rgba(210, 153, 34, 0.14)',
    };
    ctx.font = '11px "Segoe UI", system-ui, sans-serif';

    if (this.span) {
      ctx.fillStyle = colours.span;
      ctx.fillRect(sx(this.span[0]), 0, Math.max(2, sx(this.span[1]) - sx(this.span[0])), height);
    }

    rows.forEach((row, i) => {
      const top = i * (this.rowHeight + ROW_GAP);
      const h = this.rowHeight;
      ctx.strokeStyle = colours.line;
      ctx.lineWidth = 1;
      ctx.strokeRect(PAD_L + 0.5, top + 0.5, plotW, h);
      ctx.fillStyle = colours.label;
      ctx.fillText(row.label, 6, top + 14);
      ctx.fillText(row.unit || '', 6, top + 28);

      if (row.delta) {
        const { x, y } = this.data.delta;
        const [lo, hi] = extent([y, [0]]);
        const sy = (v) => top + h - ((v - lo) / (hi - lo)) * h;
        ctx.strokeStyle = colours.line;
        ctx.beginPath();
        ctx.moveTo(PAD_L, sy(0));
        ctx.lineTo(PAD_L + plotW, sy(0));
        ctx.stroke();
        ctx.lineWidth = 1.6;
        for (let k = 1; k < x.length; k += 1) {
          // Rising delta = the lap under review is losing time there.
          ctx.strokeStyle = y[k] >= y[k - 1] ? colours.loss : colours.gain;
          ctx.beginPath();
          ctx.moveTo(sx(x[k - 1]), sy(y[k - 1]));
          ctx.lineTo(sx(x[k]), sy(y[k]));
          ctx.stroke();
        }
        ctx.fillStyle = colours.label;
        ctx.fillText(`${hi.toFixed(2)}`, PAD_L + 4, top + 12);
        ctx.fillText(`${lo.toFixed(2)}`, PAD_L + 4, top + h - 4);
        return;
      }

      const [lo, hi] = extent([row.main, row.ref]);
      const sy = (v) => top + h - ((v - lo) / (hi - lo)) * h;
      for (const [key, colour] of [['ref', colours.ref], ['main', colours.main]]) {
        const ys = row[key];
        if (!ys) continue;
        ctx.strokeStyle = colour;
        ctx.lineWidth = key === 'main' ? 1.5 : 1.1;
        ctx.beginPath();
        let pen = false;
        for (let k = 0; k < this.data.x.length; k += 1) {
          const v = ys[k];
          if (v == null || !Number.isFinite(v)) {
            pen = false;
            continue;
          }
          const px = sx(this.data.x[k]);
          if (pen) ctx.lineTo(px, sy(v));
          else ctx.moveTo(px, sy(v));
          pen = true;
        }
        ctx.stroke();
      }
      ctx.fillStyle = colours.label;
      ctx.fillText(`${hi.toFixed(0)}`, PAD_L + 4, top + 12);
      ctx.fillText(`${lo.toFixed(0)}`, PAD_L + 4, top + h - 4);
    });

    // distance axis
    const axisTop = rows.length * (this.rowHeight + ROW_GAP);
    ctx.fillStyle = colours.label;
    const step = len > 4000 ? 1000 : 500;
    for (let m = 0; m <= len; m += step) {
      ctx.fillText(`${(m / 1000).toFixed(1)} km`, sx(m) - 14, axisTop + 13);
    }

    if (this.hover != null) {
      const px = sx(this.hover);
      ctx.strokeStyle = colours.ink;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px, 0);
      ctx.lineTo(px, axisTop);
      ctx.stroke();
      this._readout(ctx, rows, px, width);
    }
  }

  _readout(ctx, rows, px, width) {
    const lines = [];
    if (this.data.delta && this.data.delta.x.length) {
      const k = nearestIndex(this.data.delta.x, this.hover);
      lines.push(`delta ${this.data.delta.y[k] >= 0 ? '+' : ''}${this.data.delta.y[k].toFixed(3)} s`);
    }
    const k = nearestIndex(this.data.x, this.hover);
    for (const row of rows) {
      if (row.delta) continue;
      const a = row.main && row.main[k];
      const b = row.ref && row.ref[k];
      const f = (v) => (v == null || !Number.isFinite(v) ? 'n/a' : v.toFixed(row.digits ?? 0));
      lines.push(`${row.label} ${f(a)} / ${f(b)} ${row.unit || ''}`);
    }
    lines.unshift(`${Math.round(this.hover)} m`);
    const boxW = 190;
    const x = px + boxW + 12 > width ? px - boxW - 8 : px + 8;
    ctx.fillStyle = 'rgba(14, 17, 22, 0.92)';
    ctx.fillRect(x, 4, boxW, lines.length * 15 + 8);
    ctx.fillStyle = cssVar('--ink', '#e6edf3');
    lines.forEach((line, i) => ctx.fillText(line, x + 6, 18 + i * 15));
  }
}

/**
 * The track map: the lap's GPS path, event markers where the detectors fired,
 * and a dot at the hovered distance.
 */
export class TrackMap {
  constructor(canvas, { height = 300 } = {}) {
    this.canvas = canvas;
    this.height = height;
    this.points = [];
    this.markers = [];
    this.dot = null;
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => this.draw()).observe(canvas.parentElement);
    }
  }

  setPath(points) {
    this.points = (points || []).filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon));
    this.draw();
  }

  /** markers: [{ point: {lat, lon}, severity, label }] */
  setMarkers(markers) {
    this.markers = markers || [];
    this.draw();
  }

  setDot(point) {
    this.dot = point;
    this.draw();
  }

  draw() {
    const { ctx, width, height } = setupCanvas(this.canvas, this.height);
    ctx.clearRect(0, 0, width, height);
    if (this.points.length < 2) {
      ctx.fillStyle = cssVar('--muted', '#8b949e');
      ctx.font = '12px "Segoe UI", system-ui, sans-serif';
      ctx.fillText('No GPS position channels (Lat/Lon) in this session.', 12, 24);
      return;
    }
    const lat0 = this.points.reduce((s, p) => s + p.lat, 0) / this.points.length;
    const kx = Math.cos((lat0 * Math.PI) / 180);
    const xs = this.points.map((p) => p.lon * kx);
    const ys = this.points.map((p) => p.lat);
    const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
    const [y0, y1] = [Math.min(...ys), Math.max(...ys)];
    const scale = Math.min((width - 24) / (x1 - x0 || 1), (height - 24) / (y1 - y0 || 1));
    const ox = (width - (x1 - x0) * scale) / 2;
    const oy = (height - (y1 - y0) * scale) / 2;
    const project = (p) => [ox + (p.lon * kx - x0) * scale, height - (oy + (p.lat - y0) * scale)];

    ctx.strokeStyle = cssVar('--line', '#262d38');
    ctx.lineWidth = 7;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    this.points.forEach((p, i) => {
      const [x, y] = project(p);
      if (i) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
    });
    ctx.stroke();
    ctx.strokeStyle = cssVar('--muted', '#8b949e');
    ctx.lineWidth = 1.5;
    ctx.stroke();

    const [sx, sy] = project(this.points[0]);
    ctx.fillStyle = cssVar('--ink', '#e6edf3');
    ctx.fillRect(sx - 1, sy - 6, 3, 12); // start/finish

    const sev = {
      critical: cssVar('--critical', '#f85149'),
      advisory: cssVar('--advisory', '#d29922'),
      info: cssVar('--info', '#3fb950'),
    };
    for (const m of this.markers) {
      if (!m.point) continue;
      const [x, y] = project(m.point);
      ctx.fillStyle = sev[m.severity] || sev.info;
      ctx.globalAlpha = 0.75;
      ctx.beginPath();
      ctx.arc(x, y, m.severity === 'critical' ? 5 : 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }
    if (this.dot) {
      const [x, y] = project(this.dot);
      ctx.fillStyle = cssVar('--accent', '#2f81f7');
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fill();
    }
  }
}
