/**
 * Strategy: the pit window on a lap axis, the fuel block, the tyre corners.
 *
 * Two canvases and some DOM. The canvases draw only marks the state actually
 * carries -- an unknown window edge is simply not drawn, rather than drawn at
 * the end of the axis where it would read as a decision nobody made.
 */

import { NA, isNum, num, signed, trendArrow } from './format.js';
import { canvas2d, clear, cssVar, el, setClass, setText } from './dom.js';
import { TYRE_PRESSURE_DEADBAND, TYRE_TEMP_DEADBAND } from './view-model.js';

const CORNER_LABELS = { LF: 'LF', RF: 'RF', LR: 'LR', RR: 'RR' };

export class StrategyPanel {
  constructor(root) {
    this.root = root;
    this.windowCanvas = root.querySelector('[data-role="pit-window"]');
    this.windowLegend = root.querySelector('[data-role="pit-window-legend"]');
    this.sparkCanvas = root.querySelector('[data-role="fuel-spark"]');
    this.tyreGrid = root.querySelector('[data-role="tyre-grid"]');
    this.recommendation = root.querySelector('[data-role="recommendation"]');
    this.fields = {};
    root.querySelectorAll('[data-field]').forEach((node) => {
      this.fields[node.dataset.field] = node;
    });
    this._view = null;
    this._points = [];
  }

  _set(field, value, className) {
    const node = this.fields[field];
    if (!node) return;
    setText(node, value);
    if (className !== undefined) setClass(node, className);
  }

  render(view, points) {
    this._view = view;
    this._points = points || [];

    const fuel = view.fuel;
    this._set('fuel-level', view.available ? num(fuel.level, 2, ' L') : NA);
    this._set('fuel-per-lap', view.available ? num(fuel.perLap, 3, ' L') : NA);
    this._set(
      'fuel-per-lap-std',
      isNum(fuel.perLapStd) ? `± ${num(fuel.perLapStd, 3)} over ${fuel.samples} laps` : `${fuel.samples || 0} laps sampled`
    );
    this._set('fuel-laps', view.available ? num(fuel.lapsRemaining, 2) : NA);
    this._set('fuel-laps-needed', view.available ? num(fuel.lapsToFinish, 2) : NA);
    this._set(
      'fuel-margin',
      view.available ? signed(fuel.marginLaps, 2) : NA,
      `stat-value big band-${fuel.band}`
    );
    this._set('fuel-margin-l', isNum(fuel.marginL) ? `${signed(fuel.marginL, 2)} L` : NA);

    this._set('stint', isNum(view.stint.number) ? `#${view.stint.number}` : NA);
    this._set(
      'tyre-laps',
      view.tyres.available ? `${view.tyres.stintLaps} laps on set` : `${NA} — no tyre channels`
    );

    this._renderTyres(view.tyres);
    this.drawWindow();
    this.drawSpark();
  }

  renderRecommendation(recommendation) {
    const node = this.recommendation;
    if (!node) return;
    clear(node);
    if (!recommendation) {
      node.append(el('span', 'muted', 'No strategist call yet.'));
      setClass(node, 'recommendation empty');
      return;
    }
    setClass(
      node,
      `recommendation${recommendation.refused ? ' refused' : ''}${
        recommendation.grounded ? '' : ' ungrounded'
      }`
    );
    node.append(el('span', 'call', recommendation.call));
    if (recommendation.parts.length) {
      node.append(el('span', 'call-parts', recommendation.parts.join(' · ')));
    }
    if (recommendation.spoken) node.append(el('span', 'call-spoken', recommendation.spoken));
    if (recommendation.refused) {
      node.append(el('span', 'call-flag', 'refused — not backed by data'));
    }
  }

  _renderTyres(tyres) {
    const grid = this.tyreGrid;
    if (!grid) return;
    clear(grid);
    for (const corner of tyres.corners) {
      const cell = el('div', `tyre tyre-${corner.key}`);
      cell.append(el('span', 'tyre-corner', CORNER_LABELS[corner.key] || corner.key));

      const temp = el('span', 'tyre-temp');
      temp.append(el('span', 'v', corner.temp === null ? NA : num(corner.temp, 0)));
      temp.append(el('span', 'u', '°C'));
      temp.append(
        el('span', `t ${trendClass(corner.tempTrend, TYRE_TEMP_DEADBAND)}`,
          trendArrow(corner.tempTrend, TYRE_TEMP_DEADBAND))
      );
      cell.append(temp);

      const pressure = el('span', 'tyre-pressure');
      pressure.append(el('span', 'v', corner.pressure === null ? NA : num(corner.pressure, 1)));
      pressure.append(el('span', 'u', 'kPa'));
      pressure.append(
        el('span', `t ${trendClass(corner.pressureTrend, TYRE_PRESSURE_DEADBAND)}`,
          trendArrow(corner.pressureTrend, TYRE_PRESSURE_DEADBAND))
      );
      cell.append(pressure);

      if (isNum(corner.tempTrend)) {
        cell.title = `${corner.key}: ${signed(corner.tempTrend, 2)} °C/lap`;
      }
      grid.append(cell);
    }
  }

  /**
   * The pit window as a lap axis. Current lap is a bright caret; the window is a
   * filled span between earliest and latest; the fuel-limit lap (where the tank
   * runs dry, fractional) is a hard edge past which there is no choice left.
   */
  drawWindow() {
    const view = this._view;
    if (!this.windowCanvas) return;
    const surface = canvas2d(this.windowCanvas);
    if (!surface) return;
    const { ctx, width, height } = surface;
    const legend = [];

    const line = cssVar('--line', '#262d38');
    const muted = cssVar('--muted', '#8b949e');
    const ink = cssVar('--ink', '#e6edf3');
    const accent = cssVar('--accent', '#2f81f7');
    const critical = cssVar('--critical', '#f85149');
    const advisory = cssVar('--advisory', '#d29922');
    const info = cssVar('--info', '#3fb950');

    const pad = 10;
    const track = { y: Math.round(height * 0.52), h: 14 };
    const win = view && view.window;

    ctx.fillStyle = line;
    ctx.fillRect(pad, track.y, width - pad * 2, track.h);

    if (!win || !isNum(win.first) || win.last <= win.first) {
      ctx.fillStyle = muted;
      ctx.font = '12px ui-monospace, monospace';
      ctx.fillText('no fuel model — pit window unavailable', pad, track.y - 8);
      setText(this.windowLegend, `${NA} — needs fuel channels and a learned per-lap`);
      return;
    }

    const span = win.last - win.first;
    const x = (lap) => pad + ((lap - win.first) / span) * (width - pad * 2);

    // lap gridlines, every lap while they are legible, else every fifth
    const step = span > 24 ? 5 : 1;
    ctx.font = '10px ui-monospace, monospace';
    ctx.fillStyle = muted;
    for (let lap = Math.ceil(win.first); lap <= win.last; lap += step) {
      const px = x(lap);
      ctx.fillRect(px, track.y + track.h, 1, 4);
      ctx.fillText(String(lap), px - 5, track.y + track.h + 16);
    }

    // the window itself
    if (isNum(win.earliest) && isNum(win.latest) && win.latest >= win.earliest) {
      ctx.fillStyle = win.open ? withAlpha(info, 0.35) : withAlpha(accent, 0.28);
      ctx.fillRect(x(win.earliest), track.y, Math.max(2, x(win.latest) - x(win.earliest)), track.h);
      ctx.fillStyle = win.open ? info : accent;
      ctx.fillRect(x(win.earliest), track.y - 4, 2, track.h + 8);
      ctx.fillRect(x(win.latest), track.y - 4, 2, track.h + 8);
      legend.push(`window ${win.earliest}–${win.latest}${win.open ? ' (open)' : ''}`);
    } else {
      legend.push(`window ${NA}`);
    }

    // where the tank actually runs out -- fractional, and the hard bound
    if (isNum(win.fuelLimitLap)) {
      const px = x(win.fuelLimitLap);
      ctx.fillStyle = critical;
      ctx.fillRect(px, track.y - 8, 2, track.h + 16);
      ctx.font = '10px ui-monospace, monospace';
      ctx.fillText('dry', Math.min(px + 4, width - 24), track.y - 10);
      legend.push(`dry at ${num(win.fuelLimitLap, 2)}`);
    } else {
      legend.push(`dry ${NA}`);
    }

    // the chequered flag, when the session says how long it is
    if (isNum(win.finishLap)) {
      const px = x(win.finishLap);
      ctx.fillStyle = advisory;
      ctx.fillRect(px, track.y - 8, 2, track.h + 16);
      legend.push(`finish ${num(win.finishLap, 2)}`);
    }

    // and us
    if (isNum(win.lap)) {
      const px = x(win.lap);
      ctx.fillStyle = ink;
      ctx.beginPath();
      ctx.moveTo(px, track.y - 12);
      ctx.lineTo(px - 6, track.y - 22);
      ctx.lineTo(px + 6, track.y - 22);
      ctx.closePath();
      ctx.fill();
      ctx.fillRect(px - 1, track.y - 12, 2, track.h + 12);
      legend.unshift(`lap ${win.lap}`);
    }

    setText(this.windowLegend, legend.join('  ·  '));
  }

  /** Per-lap consumption, one point per completed lap. */
  drawSpark() {
    if (!this.sparkCanvas) return;
    const surface = canvas2d(this.sparkCanvas);
    if (!surface) return;
    const { ctx, width, height } = surface;
    const points = this._points;
    const muted = cssVar('--muted', '#8b949e');

    if (points.length < 2) {
      ctx.fillStyle = muted;
      ctx.font = '11px ui-monospace, monospace';
      ctx.fillText(points.length ? 'one lap sampled' : 'no consumption samples yet', 4, height / 2 + 4);
      return;
    }

    const values = points.map((p) => p.perLap);
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    if (hi - lo < 1e-6) {
      lo -= 0.05;
      hi += 0.05;
    }
    const pad = 3;
    const x = (i) => pad + (i / (points.length - 1)) * (width - pad * 2);
    const y = (v) => height - pad - ((v - lo) / (hi - lo)) * (height - pad * 2);

    ctx.strokeStyle = cssVar('--accent', '#2f81f7');
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    points.forEach((point, index) => {
      const px = x(index);
      const py = y(point.perLap);
      if (index === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();

    const last = points[points.length - 1];
    ctx.fillStyle = cssVar('--ink', '#e6edf3');
    ctx.beginPath();
    ctx.arc(x(points.length - 1), y(last.perLap), 2.5, 0, Math.PI * 2);
    ctx.fill();
  }
}

function trendClass(value, deadband) {
  if (!isNum(value)) return 'flat';
  if (value > deadband) return 'up';
  if (value < -deadband) return 'down';
  return 'flat';
}

/** `#rrggbb` -> `rgba(...)`. Named colours fall through unchanged. */
function withAlpha(color, alpha) {
  const match = /^#?([0-9a-f]{6})$/i.exec(String(color).trim());
  if (!match) return color;
  const value = parseInt(match[1], 16);
  const r = (value >> 16) & 255;
  const g = (value >> 8) & 255;
  const b = value & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export default StrategyPanel;
