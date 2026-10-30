/**
 * The pitwall pipeline as a graph, with a packet for every record that crossed it.
 *
 *   telemetry -> engine -> detectors -> bus -> orchestrator -> agents
 *   agents <-> model, agents <-> tools, agents -> validator -> radio -> driver
 *   radio -> agents   (the recent radio each run is shown: how agents hear each other)
 *
 * The layout is fixed; what moves is driven entirely by `pulse(edge)`, called
 * with the edges activity-model.js `edgesFor()` derives from real records. Nothing
 * animates on a timer. SVG through createElementNS + textContent, never HTML strings.
 */

import { AGENTS, NODES } from './activity-model.js';

const SVG = 'http://www.w3.org/2000/svg';
const W = 1100;
const H = 340;
const NODE_W = 134;
const NODE_H = 44;
const PULSE_MS = 650;
const MAX_PULSES = 160;
const SUB_CHARS = 21;

/** Node positions (centres) in the 1100 x 340 viewBox. */
export const LAYOUT = {
  telemetry: [75, 50],
  engine: [225, 50],
  detectors: [375, 50],
  bus: [525, 50],
  orchestrator: [675, 50],
  strategist: [840, 50],
  vehicle_engineer: [840, 135],
  spotter: [840, 220],
  coach: [840, 305],
  llm: [1020, 50],
  tools: [1020, 175],
  validator: [1020, 305],
  radio: [675, 220],
  driver: [500, 220],
};

export const LABELS = {
  telemetry: 'Telemetry',
  engine: 'Race-state engine',
  detectors: 'Detectors',
  bus: 'Event bus',
  orchestrator: 'Orchestrator',
  strategist: 'Strategist',
  vehicle_engineer: 'Vehicle engineer',
  spotter: 'Spotter',
  coach: 'Coach',
  llm: 'Kimi model',
  tools: 'Tools',
  validator: 'Grounding check',
  radio: 'Radio feed',
  driver: 'Driver',
};

/** The static edges drawn even when idle, so the pipeline reads at rest. */
export const STATIC_EDGES = [
  ['telemetry', 'engine'],
  ['engine', 'detectors'],
  ['detectors', 'bus'],
  ['bus', 'orchestrator'],
  ...AGENTS.map((a) => ['orchestrator', a]),
  ...AGENTS.map((a) => [a, 'llm']),
  ...AGENTS.map((a) => [a, 'tools']),
  ...AGENTS.map((a) => [a, 'validator']),
  ['validator', 'radio'],
  ['radio', 'driver'],
];

/** A gently curved path between two node centres, edge to edge. */
export function edgePath(from, to) {
  const [x1, y1] = LAYOUT[from] || [0, 0];
  const [x2, y2] = LAYOUT[to] || [0, 0];
  const dx = x2 - x1;
  const dy = y2 - y1;
  // Leave from the box edge facing the target.
  const sx = x1 + Math.sign(dx) * (Math.abs(dx) > Math.abs(dy) ? NODE_W / 2 : 0);
  const sy = y1 + Math.sign(dy) * (Math.abs(dx) > Math.abs(dy) ? 0 : NODE_H / 2);
  const ex = x2 - Math.sign(dx) * (Math.abs(dx) > Math.abs(dy) ? NODE_W / 2 : 0);
  const ey = y2 - Math.sign(dy) * (Math.abs(dx) > Math.abs(dy) ? 0 : NODE_H / 2);
  const mx = (sx + ex) / 2;
  const my = (sy + ey) / 2;
  const bend = Math.abs(dx) > Math.abs(dy) ? dy * 0.15 : dx * 0.15;
  return `M${sx},${sy} Q${mx - bend},${my + bend} ${ex},${ey}`;
}

function el(tag, attrs, text) {
  const n = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  if (text !== undefined) n.textContent = String(text);
  return n;
}

export class FlowGraph {
  constructor(host) {
    this.host = host;
    this.svg = el('svg', {
      class: 'flow-graph',
      viewBox: `0 0 ${W} ${H}`,
      preserveAspectRatio: 'xMidYMid meet',
      role: 'img',
      'aria-label': 'Pitwall data flow',
    });
    this.edgeLayer = el('g', { class: 'fg-edges' });
    this.pulseLayer = el('g', { class: 'fg-pulses' });
    this.nodeLayer = el('g', { class: 'fg-nodes' });
    this.svg.append(this.edgeLayer, this.nodeLayer, this.pulseLayer);
    host.append(this.svg);
    this.paths = new Map();
    this.nodes = new Map();
    this.pulses = [];
    for (const [from, to] of STATIC_EDGES) this._path(from, to);
    for (const id of NODES) this._node(id);
    this._tick = this._tick.bind(this);
    requestAnimationFrame(this._tick);
  }

  _key(from, to) {
    return `${from}>${to}`;
  }

  _path(from, to) {
    const key = this._key(from, to);
    let path = this.paths.get(key);
    if (!path) {
      path = el('path', { class: `fg-edge edge-${from} edge-to-${to}`, d: edgePath(from, to) });
      this.edgeLayer.append(path);
      this.paths.set(key, path);
    }
    return path;
  }

  _node(id) {
    const [x, y] = LAYOUT[id];
    const g = el('g', { class: `fg-node node-${id}`, transform: `translate(${x - NODE_W / 2},${y - NODE_H / 2})` });
    const rect = el('rect', { width: NODE_W, height: NODE_H, rx: 7 });
    const label = el('text', { class: 'fg-label', x: 10, y: 18 }, LABELS[id] || id);
    const sub = el('text', { class: 'fg-sub', x: 10, y: 34 }, '');
    const dot = el('circle', { class: 'fg-dot', cx: NODE_W - 11, cy: 12, r: 4 });
    const title = el('title', {}, '');
    g.append(title, rect, label, sub, dot);
    this.nodeLayer.append(g);
    this.nodes.set(id, { g, sub, title, flashUntil: 0 });
  }

  /** Update a node's sub-line and state class (idle, queued, thinking, tool, ...). */
  setNode(id, { sub, status } = {}) {
    const n = this.nodes.get(id);
    if (!n) return;
    if (sub !== undefined) {
      // A node is narrow; the full line stays available as the tooltip.
      const text = sub.length > SUB_CHARS ? `${sub.slice(0, SUB_CHARS - 1)}…` : sub;
      if (n.sub.textContent !== text) n.sub.textContent = text;
      if (n.title.textContent !== sub) n.title.textContent = sub;
    }
    if (status !== undefined) {
      const cls = `fg-node node-${id} status-${status}`;
      if (n.g.getAttribute('class') !== cls) n.g.setAttribute('class', cls);
    }
  }

  /** Animate one packet along an edge. `edge` comes from edgesFor(). */
  pulse(edge) {
    if (!edge || !LAYOUT[edge.from] || !LAYOUT[edge.to]) return;
    const path = this._path(edge.from, edge.to);
    if (edge.context) path.classList.add('fg-context');
    const colour = edge.source || edge.agent || '';
    const cls = [
      'fg-pulse',
      colour ? `agent-${colour}` : '',
      edge.ok === false || edge.blocked ? 'fg-bad' : '',
      edge.context ? 'fg-ctx' : '',
    ]
      .filter(Boolean)
      .join(' ');
    const dot = el('circle', { class: cls, r: edge.context ? 3.5 : 4.5 });
    this.pulseLayer.append(dot);
    const start = performance.now() + (edge.delay ? PULSE_MS * 0.6 : 0);
    this.pulses.push({ dot, path, start, length: path.getTotalLength() });
    path.classList.add('fg-hot');
    path._hotUntil = start + PULSE_MS;
    const target = this.nodes.get(edge.to);
    if (target) target.flashUntil = start + PULSE_MS + 220;
    while (this.pulses.length > MAX_PULSES) this.pulses.shift().dot.remove();
  }

  _tick(now) {
    const keep = [];
    for (const p of this.pulses) {
      const f = (now - p.start) / PULSE_MS;
      if (f < 0) {
        p.dot.setAttribute('opacity', '0');
        keep.push(p);
        continue;
      }
      if (f >= 1) {
        p.dot.remove();
        continue;
      }
      const pt = p.path.getPointAtLength(p.length * f);
      p.dot.setAttribute('cx', pt.x.toFixed(1));
      p.dot.setAttribute('cy', pt.y.toFixed(1));
      p.dot.setAttribute('opacity', '1');
      keep.push(p);
    }
    this.pulses = keep;
    for (const path of this.paths.values()) {
      if (path._hotUntil && now > path._hotUntil) {
        path.classList.remove('fg-hot');
        path._hotUntil = 0;
      }
    }
    for (const n of this.nodes.values()) {
      n.g.classList.toggle('fg-flash', now < n.flashUntil);
    }
    requestAnimationFrame(this._tick);
  }
}
