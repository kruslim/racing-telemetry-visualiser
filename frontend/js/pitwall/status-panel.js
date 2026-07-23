/**
 * The status strip: the four or five things you are allowed to read at 250 km/h.
 *
 * The flag band is the point of this panel. Under green it is a hairline; under
 * anything else it takes the full width in that flag's colour, because "is there
 * a caution" must never be a thing you have to look for.
 */

import { NA, clock, isNum, num, trendArrow } from './format.js';
import { setClass, setText } from './dom.js';

export class StatusPanel {
  constructor(root) {
    this.root = root;
    this.band = root.querySelector('[data-role="flag-band"]');
    this.fields = {};
    root.querySelectorAll('[data-field]').forEach((node) => {
      this.fields[node.dataset.field] = node;
    });
  }

  _set(field, value, className) {
    const node = this.fields[field];
    if (!node) return;
    setText(node, value);
    if (className !== undefined) setClass(node, className);
  }

  render(view) {
    const flag = view.flag;
    setText(this.band, flag.alert ? flag.label : '');
    setClass(this.band, `flag-band flag-${flag.phase}${flag.alert ? ' alert' : ''}`);

    this._set('session-state', view.session.state);
    this._set('session-track', view.session.track);

    const remaining = view.remaining;
    this._set(
      'remaining',
      remaining.kind === 'time' ? clock(remaining.seconds) : remaining.text
    );
    this._set('remaining-unit', remaining.unit);

    const conditions = view.conditions;
    this._set(
      'track-temp',
      conditions.available && isNum(conditions.track)
        ? `${num(conditions.track, 1)}°${trendArrow(conditions.trackTrend, 0.05)}`
        : NA
    );
    this._set(
      'air-temp',
      conditions.available && isNum(conditions.air)
        ? `${num(conditions.air, 1)}°${trendArrow(conditions.airTrend, 0.05)}`
        : NA
    );

    const connection = view.connection;
    this._set('connection', connection.label, `pill conn-${connection.state}`);
    this._set('connection-detail', connection.detail);

    const llm = view.llm;
    this._set('llm', llm.label, `pill llm-${llm.tone}`);
    this._set('llm-detail', llm.detail);
  }
}

export default StatusPanel;
