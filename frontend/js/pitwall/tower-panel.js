/**
 * The timing tower: the cars around us, and what they are doing to our gap.
 *
 * Collapsed it is P±3, which is the only part of the field a driver can act on;
 * expanded it is everyone. Gaps are the engine's, never re-derived here -- when
 * it has no basis to turn lap distance into seconds it returns null for every
 * gap, and the tower says so at the top rather than printing a column of `n/a`
 * that looks like a bug.
 */

import { NA, gap, humanise, isNum, lapTime } from './format.js';
import { clear, el, setText } from './dom.js';

const TREND_ARROW = { closing: '▲', opening: '▼', steady: '–' };

export class TowerPanel {
  constructor(root, options = {}) {
    this.root = root;
    this.body = root.querySelector('[data-role="tower-body"]');
    this.note = root.querySelector('[data-role="tower-note"]');
    this.toggle = root.querySelector('[data-role="tower-toggle"]');
    this.expanded = false;
    this.onToggle = options.onToggle || (() => {});
    if (this.toggle) {
      this.toggle.onclick = () => {
        this.expanded = !this.expanded;
        this.onToggle(this.expanded);
      };
    }
  }

  render(view) {
    if (this.toggle) {
      setText(this.toggle, view.expanded ? '▲ around me' : `▼ full field (${view.total})`);
      this.toggle.disabled = view.total === 0;
    }

    if (!view.available) {
      setText(this.note, `${NA} — this session has no per-car standings channels`);
    } else if (!view.gapsKnown) {
      setText(this.note, 'gaps unavailable: no lap time or track length to convert from');
    } else {
      setText(this.note, `gap basis: ${humanise(view.gapBasis)}`);
    }

    clear(this.body);
    for (const row of view.rows) {
      this.body.append(this._row(row));
    }
    if (!view.rows.length) {
      this.body.append(el('div', 'tower-empty muted', 'no cars in the running order'));
    }
  }

  _row(row) {
    const classes = ['tower-row'];
    if (row.isPlayer) classes.push('player');
    if (row.onPitRoad) classes.push('in-pit');
    if (row.warning) classes.push(`warn-${row.warning.kind}`);
    const node = el('div', classes.join(' '));

    node.append(el('span', 'pos', row.position === null ? '–' : `P${row.position}`));
    node.append(el('span', 'car', row.label));

    const gapText = row.isPlayer ? '—' : gap(row.gapToPlayer);
    const gapCell = el('span', 'gap', gapText);
    if (!row.isPlayer && row.trend) {
      gapCell.append(el('span', `trend ${row.trend}`, TREND_ARROW[row.trend] || ''));
      gapCell.title = `${row.trend} on us`;
    }
    node.append(gapCell);

    node.append(el('span', 'last', row.lastLap === null ? NA : lapTime(row.lastLap)));

    const status = el('span', 'status');
    if (row.onPitRoad) status.append(el('span', 'tag pit', 'PIT'));
    if (row.warning) status.append(el('span', `tag ${row.warning.kind}`, warningLabel(row.warning)));
    node.append(status);
    return node;
  }
}

function warningLabel(warning) {
  if (warning.kind === 'blue_flag') return 'BLUE';
  if (warning.kind === 'traffic_close') {
    const side = warning.payload && warning.payload.side;
    return side ? `TRAFFIC ${String(side).toUpperCase()}` : 'TRAFFIC';
  }
  if (warning.kind === 'rival_pitted') return 'PITTED';
  return String(warning.kind).toUpperCase();
}

export default TowerPanel;
