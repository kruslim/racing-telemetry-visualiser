/**
 * Radio audio discipline. Pure logic: no speechSynthesis, no DOM, no timers.
 *
 * One driver, one pair of ears, four agents talking. This module owns every rule
 * about *what gets said and when*; a "speaker" object injected at construction
 * owns the actual noise. That split is the point -- the rules below are the part
 * that can be wrong in a way you only notice mid-race, so they are testable
 * without a browser (see js/audio-manager.test.js, run in-page by audio-test.html
 * or headlessly by `node frontend/js/run-audio-tests.mjs`).
 *
 * The rules, in the order they bite:
 *
 *   1. SINGLE SPEAKER. One utterance at a time. Never two voices at once.
 *   2. CRITICAL INTERRUPTS. A critical call cancels whatever is mid-sentence and
 *      goes now -- unless what is mid-sentence is itself critical, in which case
 *      it queues. Critical never talks over critical.
 *   3. ADVISORY QUEUES, in priority-then-arrival order.
 *   4. INFO IS OPPORTUNISTIC. It speaks only if the channel is idle, and it is
 *      dropped once it has waited longer than `staleInfoMs` (15 s): stale context
 *      read out three corners later is worse than nothing.
 *   5. SUPERSEDE. A newer non-critical message with the same (agent, subject)
 *      replaces the older one *while it is still queued* -- the same rule the
 *      backend RadioFeed applies, restated here because the browser queue is a
 *      second place a stale pit lap could survive.
 *   6. MUTES. A muted agent (or master mute) is dropped at the door, never queued,
 *      so unmuting does not unleash a backlog of history.
 *
 * The speaker interface (duck-typed, all optional except speak/cancel):
 *
 *   speak(message, {volume, cue, voice})  -> begin; the host calls
 *                                            manager.finished(message_id) on end
 *   cancel()                              -> stop immediately
 *
 * Nothing in here touches storage of any kind. Mutes and volume live in memory
 * and in the URL, by design.
 */

export const PRIORITY_RANK = { critical: 0, advisory: 1, info: 2 };
export const DEFAULT_PRIORITY = 'info';

/** Info older than this has stopped being information. */
export const STALE_INFO_MS = 15000;

/** How many recently-seen ids are remembered for de-duplication. */
const SEEN_LIMIT = 256;

export function priorityRank(priority) {
  const rank = PRIORITY_RANK[priority];
  return rank === undefined ? PRIORITY_RANK[DEFAULT_PRIORITY] : rank;
}

/** Content-derived fallback id, for a message that arrived without one. */
function fallbackId(message, counter) {
  const text = String(message.spoken_text || '');
  return `local-${counter}-${String(message.agent || '?')}-${text.length}`;
}

/**
 * The radio channel as the driver hears it.
 */
export class RadioAudioManager {
  constructor(options = {}) {
    const {
      speaker = null,
      now = () => Date.now(),
      staleInfoMs = STALE_INFO_MS,
      volume = 1,
      onChange = null,
      onEvent = null,
    } = options;

    this.speaker = speaker;
    this.now = now;
    this.staleInfoMs = staleInfoMs;
    this.volume = clamp01(volume);
    this.masterMuted = false;

    this._muted = new Set();
    this._queue = [];
    this._current = null;
    this._seq = 0;
    this._seen = [];
    this._seenSet = new Set();
    this._onChange = onChange;
    this._onEvent = onEvent;

    this.counters = {
      received: 0,
      spoken: 0,
      queued: 0,
      superseded: 0,
      interrupted: 0,
      droppedStale: 0,
      droppedMuted: 0,
      droppedDuplicate: 0,
      droppedSilent: 0,
    };
    /** The last message a critical call cut off, for the UI to show. */
    this.lastInterrupted = null;
  }

  // ---- controls --------------------------------------------------------
  setVolume(value) {
    this.volume = clamp01(value);
    this._changed();
    return this.volume;
  }

  setMasterMuted(muted) {
    this.masterMuted = !!muted;
    if (this.masterMuted) this.stop();
    this._changed();
    return this.masterMuted;
  }

  setAgentMuted(agent, muted) {
    if (muted) this._muted.add(agent);
    else this._muted.delete(agent);
    if (muted && this._current && this._current.message.agent === agent) this.stop();
    this._changed();
    return this.isAgentMuted(agent);
  }

  isAgentMuted(agent) {
    return this.masterMuted || this._muted.has(agent);
  }

  mutedAgents() {
    return [...this._muted].sort();
  }

  /** Stop what is being said and let the queue carry on. */
  stop() {
    if (this._current) {
      this._cancelSpeaker();
      this._current = null;
    }
    this._advance();
    this._changed();
  }

  /** Silence: kill the current call and throw the queue away. */
  clear() {
    this._cancelSpeaker();
    this._current = null;
    this._queue = [];
    this._changed();
  }

  // ---- the channel -----------------------------------------------------
  /**
   * Offer one RadioMessage to the channel.
   * @returns {{action: string, reason?: string, message: object}}
   */
  push(raw) {
    const message = normalise(raw, () => fallbackId(raw, ++this._seq));
    this.counters.received += 1;

    if (!message.spoken_text) {
      return this._result('dropped', message, 'empty');
    }
    // The driver's own push-to-talk transcript is shown, never read back.
    if (message.speak === false) {
      this.counters.droppedSilent += 1;
      return this._result('dropped', message, 'not_spoken');
    }
    if (this._isDuplicate(message.message_id)) {
      this.counters.droppedDuplicate += 1;
      return this._result('dropped', message, 'duplicate');
    }
    if (this.isAgentMuted(message.agent)) {
      this._remember(message.message_id);
      this.counters.droppedMuted += 1;
      return this._result('dropped', message, 'muted');
    }

    this._remember(message.message_id);
    this._expireStale();

    const critical = message.priority === 'critical';
    const entry = { message, queuedAt: this.now(), seq: ++this._seq };

    // Supersede first: a message that replaces a queued one must not also be
    // counted as a new arrival in the queue.
    const replaced = critical ? null : this._supersede(entry);

    if (critical && this._current && this._current.message.priority !== 'critical') {
      const interrupted = this._current.message;
      this._cancelSpeaker();
      this.counters.interrupted += 1;
      this.lastInterrupted = interrupted;
      this._current = null;
      this._start(entry);
      return this._result('interrupted', message, undefined, interrupted);
    }

    if (!this._current && this._queue.length === 0) {
      this._start(entry);
      return this._result('speaking', message, replaced ? 'superseded' : undefined, replaced);
    }

    // Info is opportunistic: it may wait, but only briefly, and only if it has a
    // realistic chance of still being true when the channel frees up.
    this._queue.push(entry);
    this._sortQueue();
    if (!replaced) this.counters.queued += 1;
    this._changed();
    return this._result('queued', message, replaced ? 'superseded' : undefined, replaced);
  }

  /** The host tells us an utterance ended (naturally or otherwise). */
  finished(messageId) {
    if (!this._current) return false;
    if (messageId && this._current.message.message_id !== messageId) return false;
    this.counters.spoken += 1;
    this._current = null;
    this._advance();
    this._changed();
    return true;
  }

  /**
   * Advance the clock: drop stale info and start anything waiting.
   * Safe to call as often as you like; it does nothing when nothing changed.
   */
  tick() {
    const dropped = this._expireStale();
    const started = this._advance();
    if (dropped || started) this._changed();
    return { dropped, started };
  }

  // ---- introspection ---------------------------------------------------
  get speaking() {
    return this._current ? this._current.message : null;
  }

  get queue() {
    return this._queue.map((entry) => entry.message);
  }

  get idle() {
    return this._current === null;
  }

  snapshot() {
    return {
      speaking: this.speaking,
      queue: this.queue,
      volume: this.volume,
      masterMuted: this.masterMuted,
      muted: this.mutedAgents(),
      counters: { ...this.counters },
    };
  }

  // ---- internals -------------------------------------------------------
  _supersede(entry) {
    const { agent, subject } = entry.message;
    if (!subject) return null;
    const index = this._queue.findIndex(
      (queued) =>
        queued.message.agent === agent &&
        queued.message.subject === subject &&
        queued.message.priority !== 'critical'
    );
    if (index < 0) return null;
    const [replaced] = this._queue.splice(index, 1);
    this.counters.superseded += 1;
    this._emit('superseded', { replaced: replaced.message, by: entry.message });
    return replaced.message;
  }

  _expireStale() {
    if (!this._queue.length) return 0;
    const now = this.now();
    const kept = [];
    let dropped = 0;
    for (const entry of this._queue) {
      const stale =
        entry.message.priority === 'info' && now - entry.queuedAt > this.staleInfoMs;
      if (stale) {
        dropped += 1;
        this.counters.droppedStale += 1;
        this._emit('stale', { message: entry.message, waitedMs: now - entry.queuedAt });
      } else {
        kept.push(entry);
      }
    }
    this._queue = kept;
    return dropped;
  }

  _advance() {
    if (this._current) return false;
    const entry = this._queue.shift();
    if (!entry) return false;
    this._start(entry);
    return true;
  }

  _start(entry) {
    this._current = entry;
    const message = entry.message;
    if (this.speaker && typeof this.speaker.speak === 'function') {
      this.speaker.speak(message, {
        volume: this.volume,
        // A short radio click ahead of a critical call: the driver's ear is
        // primed a beat before the words, the way a real transmit key sounds.
        cue: message.priority === 'critical',
      });
    }
    this._emit('speaking', { message });
    this._changed();
  }

  _cancelSpeaker() {
    if (this.speaker && typeof this.speaker.cancel === 'function') this.speaker.cancel();
  }

  _sortQueue() {
    this._queue.sort(
      (a, b) =>
        priorityRank(a.message.priority) - priorityRank(b.message.priority) ||
        a.seq - b.seq
    );
  }

  _isDuplicate(id) {
    if (!id) return false;
    if (this._seenSet.has(id)) return true;
    if (this._current && this._current.message.message_id === id) return true;
    return this._queue.some((entry) => entry.message.message_id === id);
  }

  _remember(id) {
    if (!id || this._seenSet.has(id)) return;
    this._seenSet.add(id);
    this._seen.push(id);
    while (this._seen.length > SEEN_LIMIT) this._seenSet.delete(this._seen.shift());
  }

  _result(action, message, reason, related) {
    const result = { action, message };
    if (reason) result.reason = reason;
    if (related) result.related = related;
    this._emit(action, result);
    return result;
  }

  _emit(type, detail) {
    if (typeof this._onEvent === 'function') this._onEvent(type, detail);
  }

  _changed() {
    if (typeof this._onChange === 'function') this._onChange(this.snapshot());
  }
}

/** Coerce a wire message into exactly the fields the channel cares about. */
export function normalise(raw, makeId) {
  const message = raw || {};
  const priority = PRIORITY_RANK[message.priority] === undefined
    ? DEFAULT_PRIORITY
    : message.priority;
  return {
    message_id: message.message_id || (makeId ? makeId() : ''),
    agent: message.agent || 'pitwall',
    priority,
    spoken_text: String(message.spoken_text || '').trim(),
    detail_text: message.detail_text || '',
    subject: message.subject || '',
    audio_url: message.audio_url || null,
    speak: message.speak !== false,
    session_time: message.session_time ?? null,
    data: message.data || {},
    refused: !!message.refused,
    grounded: message.grounded !== false,
  };
}

function clamp01(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 1;
  return Math.min(1, Math.max(0, number));
}

export default RadioAudioManager;
