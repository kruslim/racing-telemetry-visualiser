/**
 * The event ticker: the deterministic layer, made visible.
 *
 * This is deliberately *not* the radio feed above it. Everything here happened
 * -- a wheel locked, a rival pitted, a flag changed -- and nothing here was
 * decided by a model. Keeping them visually distinct is the whole point: it is
 * how an operator can tell "the car did this" from "an agent thinks this".
 */

import { clock, humanise } from './format.js';
import { el, clear } from './dom.js';

const LIMIT = 60;

export class TickerPanel {
  constructor(root) {
    this.root = root;
    this.strip = root.querySelector('[data-role="ticker-strip"]');
    this.count = root.querySelector('[data-role="ticker-count"]');
    this._seen = 0;
    this._paused = false;
    // Pause on hover, like the radio feed: a line you are reading must not slide
    // out from under the pointer.
    this.strip.addEventListener('mouseenter', () => (this._paused = true));
    this.strip.addEventListener('mouseleave', () => {
      this._paused = false;
      this._scroll();
    });
  }

  /** Prefill from the ring buffer, oldest first. */
  seed(lines) {
    clear(this.strip);
    this._seen = 0;
    for (const line of lines) this.push(line, true);
    this._scroll(true);
  }

  push(line, quiet = false) {
    if (!line) return;
    this._seen += 1;
    const node = el('span', `tick sev-${line.severity}${line.injected ? ' injected' : ''}`);
    node.append(el('span', 'tick-lap', line.lap === null ? '—' : `L${line.lap}`));
    node.append(el('span', 'tick-time', clock(line.sessionTime)));
    node.append(el('span', 'tick-key', humanise(line.key)));
    if (line.text) node.append(el('span', 'tick-text', line.text));
    if (line.injected) node.append(el('span', 'tick-tag', 'DIRECTOR'));
    this.strip.append(node);
    while (this.strip.children.length > LIMIT) this.strip.removeChild(this.strip.firstChild);
    if (this.count) this.count.textContent = `${this._seen} events`;
    if (!quiet) this._scroll();
  }

  _scroll(force = false) {
    if (this._paused && !force) return;
    this.strip.scrollLeft = this.strip.scrollWidth;
  }
}

export default TickerPanel;
