/**
 * The radio feed -- the centrepiece, and the only panel a driver reads by ear.
 *
 * Newest at the bottom, like every radio transcript ever written, and it follows
 * the bottom automatically *unless* the pointer is inside it or you have scrolled
 * up: an auto-scroll that fights you while you read the last pit call is worse
 * than one that never scrolls.
 *
 * The one judgement worth recording is the driver thread. A message you send
 * appears as a DRIVER entry and the calls that follow it are nested underneath,
 * because that is how a transcript reads. They are labelled "after your message"
 * rather than "reply", because the backend deliberately does not route driver
 * messages to the agents (stage-4 deviation 1) -- calling an unrelated pit call a
 * reply would be exactly the unbacked claim the rest of this system refuses.
 */

import { NA, agentBadge, humanise, isNum, stamp } from './format.js';
import { clear, el, setText } from './dom.js';

const LIMIT = 200;

/** Agent calls are nested under a driver message for this long (session s). */
export const THREAD_WINDOW_S = 45;

export class RadioPanel {
  constructor(root, options = {}) {
    this.root = root;
    this.list = root.querySelector('[data-role="radio-list"]');
    this.onAir = root.querySelector('[data-role="on-air"]');
    this.queueNode = root.querySelector('[data-role="radio-queue"]');
    this.emptyNode = root.querySelector('[data-role="radio-empty"]');
    this.labels = options.labels || ((agent) => humanise(agent));

    this._hover = false;
    this._nodes = new Map();
    this._thread = null;
    this._speaking = null;

    this.list.addEventListener('mouseenter', () => (this._hover = true));
    this.list.addEventListener('mouseleave', () => {
      this._hover = false;
      this._follow();
    });
  }

  /** True when the view is pinned to the bottom (or nothing is in the way). */
  get following() {
    if (this._hover) return false;
    const slack = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight;
    return slack < 48;
  }

  seed(messages) {
    clear(this.list);
    this._nodes.clear();
    this._thread = null;
    for (const message of messages) this.push(message, null, true);
    this._follow(true);
  }

  /**
   * Add one message. `result` is the audio manager's verdict for it
   * (`speaking` / `queued` / `dropped: muted` ...), shown so the operator can
   * see why something was not heard.
   */
  push(message, result, quiet = false) {
    if (!message) return null;
    if (this.emptyNode) this.emptyNode.hidden = true;
    const follow = this.following;
    const node = this._render(message, result);

    const isDriver = message.agent === 'driver';
    if (isDriver) {
      this.list.append(node);
      const thread = el('div', 'radio-thread');
      thread.append(el('div', 'thread-note', 'after your message'));
      node.append(thread);
      this._thread = {
        node: thread,
        at: isNum(message.session_time) ? message.session_time : null,
        count: 0,
      };
    } else if (this._inThreadWindow(message)) {
      this._thread.node.append(node);
      this._thread.count += 1;
    } else {
      this._thread = null;
      this.list.append(node);
    }

    if (message.message_id) this._nodes.set(message.message_id, node);
    while (this.list.children.length > LIMIT) {
      this.list.removeChild(this.list.firstChild);
    }
    // `follow` was sampled *before* the append: once the node is in, the view is
    // by definition no longer at the bottom, so re-asking would never scroll.
    if (!quiet && follow) this._follow(true);
    return node;
  }

  _inThreadWindow(message) {
    const thread = this._thread;
    if (!thread) return false;
    if (thread.count >= 4) return false;
    if (thread.at === null || !isNum(message.session_time)) return thread.count === 0;
    return message.session_time - thread.at <= THREAD_WINDOW_S;
  }

  /** Mark whichever message is on air right now, from the audio manager. */
  setSpeaking(snapshot) {
    const speaking = snapshot ? snapshot.speaking : null;
    const id = speaking ? speaking.message_id : null;
    if (this._speaking && this._speaking !== id) {
      const previous = this._nodes.get(this._speaking);
      if (previous) previous.classList.remove('on-air');
    }
    this._speaking = id;
    if (id) {
      const node = this._nodes.get(id);
      if (node) node.classList.add('on-air');
    }

    if (this.onAir) {
      clear(this.onAir);
      if (speaking) {
        this.onAir.className = `on-air-strip ${speaking.priority}`;
        this.onAir.append(el('span', 'live-dot', '●'));
        this.onAir.append(el('span', 'who', this.labels(speaking.agent)));
        this.onAir.append(el('span', 'what', speaking.spoken_text));
      } else {
        this.onAir.className = 'on-air-strip idle';
        this.onAir.append(el('span', 'who muted', 'channel clear'));
      }
    }
    if (this.queueNode) {
      const queue = snapshot ? snapshot.queue : [];
      setText(
        this.queueNode,
        queue.length
          ? `queued: ${queue.map((m) => `${this.labels(m.agent)} (${m.priority})`).join(' · ')}`
          : 'queued: —'
      );
    }
  }

  _render(message, result) {
    const priority = message.priority || 'info';
    const isDriver = message.agent === 'driver';
    const classes = ['radio-msg', priority];
    if (isDriver) classes.push('driver');
    if (message.refused) classes.push('refused');
    if (message.grounded === false) classes.push('ungrounded');
    if (priority === 'critical') classes.push('flash');
    const node = el('div', classes.join(' '));

    const head = el('div', 'radio-head');
    head.append(el('span', `badge agent-${message.agent}`, agentBadge(message.agent)));
    head.append(el('span', 'pri', priority));
    const ref = message.event_ref || {};
    head.append(el('span', 'when', `${isNum(ref.lap) ? `L${ref.lap}` : '—'} ${stamp(message.timestamp, message.session_time)}`));
    if (result && result.action) {
      head.append(
        el('span', `verdict ${result.action}`, result.reason ? `${result.action}: ${result.reason}` : result.action)
      );
    }
    node.append(head);

    node.append(el('div', 'spoken', message.spoken_text || ''));

    const detail = String(message.detail_text || '');
    const tools = Array.isArray(message.tools_used) ? message.tools_used : [];
    if (detail || tools.length || ref.key) {
      const more = el('details', 'radio-detail');
      const summary = el('summary', '', detail ? firstLine(detail) : 'why');
      more.append(summary);
      if (detail) more.append(el('div', 'detail-body', detail));
      const trace = [];
      if (ref.key) trace.push(`triggered by ${humanise(ref.key)} @ tick ${ref.tick ?? NA}`);
      if (tools.length) trace.push(`tools: ${tools.join(', ')}`);
      if (message.model) trace.push(`model: ${message.model}`);
      if (Array.isArray(message.ungrounded) && message.ungrounded.length) {
        trace.push(`rejected figures: ${message.ungrounded.join(', ')}`);
      }
      if (trace.length) more.append(el('div', 'detail-trace', trace.join(' · ')));
      node.append(more);
    }
    return node;
  }

  _follow(force = false) {
    if (!force && !this.following) return;
    this.list.scrollTop = this.list.scrollHeight;
  }
}

function firstLine(text) {
  const line = String(text).split('\n')[0].trim();
  return line.length > 90 ? `${line.slice(0, 87)}…` : line;
}

export default RadioPanel;
