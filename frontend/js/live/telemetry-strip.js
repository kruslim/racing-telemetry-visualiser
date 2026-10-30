/**
 * Rolling traces of the raw telemetry going into the engine, from /ws/live.
 *
 * Only channels the session's catalog actually carries are drawn: the strip asks
 * for the standard iRacing names, and whatever the catalog frame does not list is
 * shown as "not in this session" rather than as a flat line pretending to be data.
 * It also reports the real frame rate it sees, which feeds the graph's
 * telemetry -> engine edge.
 */

import { clear, el, setText } from '../pitwall/dom.js';

export const CHANNELS = [
  { name: 'Speed', label: 'speed', unit: 'km/h', scale: 3.6, min: 0, max: null },
  { name: 'Throttle', label: 'throttle', unit: '%', scale: 100, min: 0, max: 100 },
  { name: 'Brake', label: 'brake', unit: '%', scale: 100, min: 0, max: 100 },
  { name: 'RPM', label: 'rpm', unit: '', scale: 1, min: 0, max: null },
  { name: 'Gear', label: 'gear', unit: '', scale: 1, min: -1, max: null },
  { name: 'SteeringWheelAngle', label: 'steer', unit: 'deg', scale: 57.2958, min: null, max: null },
];
const RATE_HZ = 20;
const WINDOW_S = 20;

export class TelemetryStrip {
  constructor(host, { onFrames } = {}) {
    this.host = host;
    this.onFrames = onFrames;
    this.series = new Map();
    this.available = null; // Set of names in the catalog, once known
    this.connection = 'connecting';
    this.frames = 0;
    this.rate = 0;
    this._rateWindow = [];
    this._build();
    this._connect();
    setInterval(() => this._reportRate(), 1000);
    const loop = () => {
      this._draw();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  _build() {
    clear(this.host);
    this.head = el('div', 'tstrip-head');
    this.status = el('span', 'sub', 'connecting to /ws/live');
    this.head.append(el('span', 'k', 'raw telemetry'), this.status);
    this.host.append(this.head);
    this.rows = new Map();
    for (const ch of CHANNELS) {
      const row = el('div', 'tstrip-row');
      const label = el('span', 'tstrip-label', ch.label);
      const value = el('span', 'tstrip-value', '—');
      const canvas = el('canvas', 'tstrip-canvas');
      canvas.height = 34;
      row.append(label, canvas, value);
      this.host.append(row);
      this.rows.set(ch.name, { row, value, canvas });
      this.series.set(ch.name, []);
    }
  }

  _connect() {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${scheme}://${window.location.host}/ws/live`);
    ws.onopen = () => {
      ws.send(
        JSON.stringify({ op: 'subscribe', channels: CHANNELS.map((c) => c.name), rate_hz: RATE_HZ })
      );
    };
    ws.onclose = () => {
      this.connection = 'reconnecting';
      setTimeout(() => this._connect(), 2000);
    };
    ws.onmessage = (raw) => {
      let msg;
      try {
        msg = JSON.parse(raw.data);
      } catch (_) {
        return;
      }
      if (msg.type === 'state') this.connection = msg.connection || 'unknown';
      else if (msg.type === 'catalog') this._catalog(msg);
      else if (msg.type === 'data') this._data(msg);
    };
  }

  _catalog(msg) {
    this.available = new Set((msg.variables || []).map((v) => v.name));
    for (const ch of CHANNELS) {
      const { row, value } = this.rows.get(ch.name);
      const present = this.available.has(ch.name);
      row.classList.toggle('missing', !present);
      if (!present) setText(value, 'not in this session');
    }
  }

  _data(msg) {
    this.frames += 1;
    this._rateWindow.push(performance.now());
    const t = msg.t;
    for (const ch of CHANNELS) {
      const raw = msg.v ? msg.v[ch.name] : undefined;
      if (typeof raw !== 'number' || !Number.isFinite(raw)) continue;
      const v = raw * ch.scale;
      const list = this.series.get(ch.name);
      list.push([t, v]);
      while (list.length && list[0][0] < t - WINDOW_S) list.shift();
      const shown = ch.name === 'Gear' ? String(Math.round(v)) : Math.round(v).toString();
      setText(this.rows.get(ch.name).value, `${shown}${ch.unit ? ` ${ch.unit}` : ''}`);
    }
  }

  _reportRate() {
    const now = performance.now();
    this._rateWindow = this._rateWindow.filter((ts) => ts > now - 2000);
    this.rate = this._rateWindow.length / 2;
    setText(
      this.status,
      `${this.connection} · ${this.rate.toFixed(0)} frames/s shown (subscribed at ${RATE_HZ} Hz)`
    );
    if (this.onFrames && this.rate > 0) this.onFrames(this.rate, this.connection);
  }

  _draw() {
    for (const ch of CHANNELS) {
      const { canvas } = this.rows.get(ch.name);
      const list = this.series.get(ch.name);
      const width = canvas.clientWidth || 300;
      if (canvas.width !== width) canvas.width = width;
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (list.length < 2) continue;
      const tEnd = list[list.length - 1][0];
      const t0 = tEnd - WINDOW_S;
      let lo = ch.min;
      let hi = ch.max;
      if (lo === null || hi === null) {
        const values = list.map((p) => p[1]);
        if (lo === null) lo = Math.min(...values);
        if (hi === null) hi = Math.max(...values);
      }
      if (!(hi > lo)) hi = lo + 1;
      ctx.strokeStyle = getComputedStyle(canvas).color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      list.forEach(([t, v], i) => {
        const x = ((t - t0) / WINDOW_S) * canvas.width;
        const y = canvas.height - 2 - ((v - lo) / (hi - lo)) * (canvas.height - 4);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
  }
}
