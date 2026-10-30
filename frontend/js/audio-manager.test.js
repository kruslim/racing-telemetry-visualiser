/**
 * Deterministic tests for the radio audio discipline.
 *
 * No DOM, no speechSynthesis, no wall clock -- a fake speaker records what it was
 * asked to do and time is a variable we increment. That makes "a critical spotter
 * call cuts off the strategist mid-sentence" a two-line assertion instead of
 * something you find out about at Eau Rouge.
 *
 * Run in a browser:  open frontend/audio-test.html
 * Run headlessly:    node frontend/js/run-audio-tests.mjs
 */

import { RadioAudioManager, priorityRank } from './audio-manager.js';

// --------------------------------------------------------------------------
// harness
// --------------------------------------------------------------------------
class FakeSpeaker {
  constructor() {
    this.spoken = [];
    this.cancelled = 0;
    this.cues = 0;
  }

  speak(message, opts = {}) {
    this.spoken.push({ message, opts });
    if (opts.cue) this.cues += 1;
  }

  cancel() {
    this.cancelled += 1;
  }

  get last() {
    return this.spoken[this.spoken.length - 1] || null;
  }

  get texts() {
    return this.spoken.map((s) => s.message.spoken_text);
  }
}

class Clock {
  constructor(t = 0) {
    this.t = t;
  }

  now = () => this.t;

  advance(ms) {
    this.t += ms;
    return this.t;
  }
}

function makeManager(options = {}) {
  const speaker = new FakeSpeaker();
  const clock = new Clock();
  const manager = new RadioAudioManager({ speaker, now: clock.now, ...options });
  return { manager, speaker, clock };
}

let idCounter = 0;
function msg(agent, priority, spoken_text, extra = {}) {
  idCounter += 1;
  return {
    message_id: `m${idCounter}`,
    agent,
    priority,
    spoken_text,
    subject: '',
    ...extra,
  };
}

function assert(condition, what) {
  if (!condition) throw new Error(what || 'assertion failed');
}

function assertEqual(actual, expected, what) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${what || 'assertEqual'}: got ${a}, expected ${e}`);
}

// --------------------------------------------------------------------------
// the cases
// --------------------------------------------------------------------------
export const CASES = [
  {
    name: 'one utterance at a time: the second advisory waits',
    run() {
      const { manager, speaker } = makeManager();
      manager.push(msg('strategist', 'advisory', 'Window opens lap five.'));
      manager.push(msg('vehicle_engineer', 'advisory', 'Fronts are dropping off.'));
      assertEqual(speaker.spoken.length, 1, 'only one utterance started');
      assertEqual(manager.queue.length, 1, 'the other one is queued');
      assertEqual(manager.speaking.agent, 'strategist', 'the first one holds the channel');
    },
  },
  {
    name: 'a critical spotter call interrupts an advisory mid-sentence',
    run() {
      const { manager, speaker } = makeManager();
      manager.push(msg('strategist', 'advisory', 'Window opens lap five, fuel only.'));
      const result = manager.push(msg('spotter', 'critical', 'Car left, car left!'));
      assertEqual(result.action, 'interrupted', 'the push reports the interrupt');
      assertEqual(speaker.cancelled, 1, 'the speaker was cancelled');
      assertEqual(speaker.texts, ['Window opens lap five, fuel only.', 'Car left, car left!']);
      assertEqual(manager.speaking.agent, 'spotter', 'the spotter has the channel');
      assertEqual(manager.counters.interrupted, 1, 'counted');
      assertEqual(manager.queue.length, 0, 'the cut-off advisory is discarded, not requeued');
      assertEqual(manager.lastInterrupted.agent, 'strategist', 'and is remembered for the UI');
    },
  },
  {
    name: 'critical never talks over critical: it queues instead',
    run() {
      const { manager, speaker } = makeManager();
      manager.push(msg('spotter', 'critical', 'Yellow, yellow, slow down.'));
      const result = manager.push(msg('strategist', 'critical', 'Box now, box now.'));
      assertEqual(result.action, 'queued', 'the second critical waits its turn');
      assertEqual(speaker.cancelled, 0, 'nothing was cut off');
      assertEqual(manager.counters.interrupted, 0, 'and nothing was counted as interrupted');
      manager.finished(manager.speaking.message_id);
      assertEqual(manager.speaking.spoken_text, 'Box now, box now.', 'then it airs');
    },
  },
  {
    name: 'the queue drains by priority, then by arrival',
    run() {
      const { manager, speaker } = makeManager();
      manager.push(msg('spotter', 'critical', 'Hold this line.'));
      manager.push(msg('coach', 'info', 'Sector two is your best.'));
      manager.push(msg('vehicle_engineer', 'advisory', 'Watch the rear temps.'));
      manager.push(msg('strategist', 'critical', 'Box now.'));
      assertEqual(
        manager.queue.map((m) => m.priority),
        ['critical', 'advisory', 'info'],
        'ordered by priority'
      );
      let guard = 0;
      while (manager.speaking && guard++ < 10) manager.finished(manager.speaking.message_id);
      assertEqual(speaker.texts, [
        'Hold this line.',
        'Box now.',
        'Watch the rear temps.',
        'Sector two is your best.',
      ]);
    },
  },
  {
    name: 'a newer message supersedes a queued one on the same subject',
    run() {
      const { manager } = makeManager();
      manager.push(msg('strategist', 'advisory', 'Box lap five.', { subject: 'pit_stop' }));
      manager.push(msg('strategist', 'advisory', 'Box lap six.', { subject: 'pit_stop' }));
      manager.push(msg('strategist', 'advisory', 'Box lap seven.', { subject: 'pit_stop' }));
      assertEqual(manager.queue.length, 1, 'one current answer, not a log of them');
      assertEqual(manager.queue[0].spoken_text, 'Box lap seven.', 'the newest one survives');
      assertEqual(manager.counters.superseded, 1, 'counted');
    },
  },
  {
    name: 'a different subject is not superseded',
    run() {
      const { manager } = makeManager();
      manager.push(msg('spotter', 'critical', 'Stay left.'));
      manager.push(msg('vehicle_engineer', 'advisory', 'Fronts are hot.', { subject: 'tyres' }));
      manager.push(msg('vehicle_engineer', 'advisory', 'Oil is climbing.', { subject: 'car_health' }));
      assertEqual(manager.queue.length, 2, 'a tyre note and an oil note are two facts');
      assertEqual(manager.counters.superseded, 0);
    },
  },
  {
    name: 'a queued critical is never superseded',
    run() {
      const { manager } = makeManager();
      manager.push(msg('spotter', 'critical', 'Hold on.'));
      manager.push(msg('strategist', 'critical', 'Box now, fumes.', { subject: 'pit_stop' }));
      manager.push(msg('strategist', 'advisory', 'Maybe lap eight.', { subject: 'pit_stop' }));
      assertEqual(manager.counters.superseded, 0, 'the advisory did not replace it');
      assertEqual(manager.queue.length, 2);
      assertEqual(manager.queue[0].spoken_text, 'Box now, fumes.', 'and it is still first');
    },
  },
  {
    name: 'info speaks when the channel is idle',
    run() {
      const { manager, speaker } = makeManager();
      const result = manager.push(msg('coach', 'info', 'Good exit that lap.'));
      assertEqual(result.action, 'speaking');
      assertEqual(speaker.spoken.length, 1);
    },
  },
  {
    name: 'info waiting longer than 15 s is dropped as stale',
    run() {
      const { manager, clock } = makeManager();
      manager.push(msg('strategist', 'advisory', 'Fuel is marginal.'));
      manager.push(msg('coach', 'info', 'Sector one improved.'));
      assertEqual(manager.queue.length, 1, 'queued behind the advisory');
      clock.advance(14000);
      manager.tick();
      assertEqual(manager.queue.length, 1, 'still fresh at 14 s');
      clock.advance(2000);
      manager.tick();
      assertEqual(manager.queue.length, 0, 'gone at 16 s');
      assertEqual(manager.counters.droppedStale, 1);
    },
  },
  {
    name: 'an advisory is never dropped for age',
    run() {
      const { manager, clock } = makeManager();
      manager.push(msg('spotter', 'critical', 'Yellow flag.'));
      manager.push(msg('strategist', 'advisory', 'Pit window is open.'));
      clock.advance(600000);
      manager.tick();
      assertEqual(manager.queue.length, 1, 'a decision is still a decision ten minutes later');
      assertEqual(manager.counters.droppedStale, 0);
    },
  },
  {
    name: 'a muted agent is dropped at the door, and unmuting replays nothing',
    run() {
      const { manager, speaker } = makeManager();
      manager.setAgentMuted('coach', true);
      const result = manager.push(msg('coach', 'advisory', 'Brake ten metres later.'));
      assertEqual(result.action, 'dropped');
      assertEqual(result.reason, 'muted');
      assertEqual(speaker.spoken.length, 0);
      assertEqual(manager.counters.droppedMuted, 1);

      manager.setAgentMuted('coach', false);
      manager.tick();
      assertEqual(speaker.spoken.length, 0, 'no backlog is unleashed');
      manager.push(msg('coach', 'advisory', 'Brake ten metres later.'));
      assertEqual(speaker.spoken.length, 1, 'but the next one is heard');
    },
  },
  {
    name: 'master mute silences the channel and stops what is being said',
    run() {
      const { manager, speaker } = makeManager();
      manager.push(msg('strategist', 'advisory', 'Window opens lap five.'));
      manager.setMasterMuted(true);
      assertEqual(speaker.cancelled, 1, 'the current call is stopped');
      assert(manager.idle, 'nothing is speaking');
      manager.push(msg('spotter', 'critical', 'Car right.'));
      assertEqual(speaker.spoken.length, 1, 'not even a critical speaks while muted');
      manager.setMasterMuted(false);
      manager.push(msg('spotter', 'critical', 'Car right, still there.'));
      assertEqual(speaker.spoken.length, 2, 'and the radio comes back');
    },
  },
  {
    name: 'a replayed message id is spoken once (a reconnect must not repeat itself)',
    run() {
      const { manager, speaker } = makeManager();
      const message = msg('strategist', 'advisory', 'Box next lap.');
      manager.push(message);
      const again = manager.push({ ...message });
      assertEqual(again.action, 'dropped');
      assertEqual(again.reason, 'duplicate');
      assertEqual(speaker.spoken.length, 1);
      assertEqual(manager.counters.droppedDuplicate, 1);
    },
  },
  {
    name: "the driver's own transcript is shown but never read back",
    run() {
      const { manager, speaker } = makeManager();
      const result = manager.push(
        msg('driver', 'info', 'Fronts are gone, I need tyres.', { speak: false })
      );
      assertEqual(result.action, 'dropped');
      assertEqual(result.reason, 'not_spoken');
      assertEqual(speaker.spoken.length, 0);
      assertEqual(manager.counters.droppedSilent, 1);
    },
  },
  {
    name: 'volume reaches the speaker, and a critical carries the radio-click cue',
    run() {
      const { manager, speaker } = makeManager();
      manager.setVolume(0.4);
      manager.push(msg('strategist', 'advisory', 'Fuel is fine.'));
      assertEqual(speaker.last.opts.volume, 0.4, 'volume passed through');
      assertEqual(speaker.last.opts.cue, false, 'no click on an advisory');
      manager.push(msg('spotter', 'critical', 'Car inside!'));
      assertEqual(speaker.last.opts.cue, true, 'a click ahead of a critical');
      assertEqual(speaker.cues, 1);
      assertEqual(manager.setVolume(5), 1, 'volume is clamped');
      assertEqual(manager.setVolume(-2), 0);
    },
  },
  {
    name: 'a late end-callback from a cancelled utterance is ignored',
    run() {
      const { manager } = makeManager();
      manager.push(msg('strategist', 'advisory', 'Window opens lap five.'));
      const stale = manager.speaking.message_id;
      manager.push(msg('spotter', 'critical', 'Car left!'));
      assertEqual(manager.finished(stale), false, 'the old utterance no longer owns the channel');
      assertEqual(manager.speaking.agent, 'spotter', 'the critical is still speaking');
      assertEqual(manager.counters.spoken, 0, 'and nothing was counted as completed');
    },
  },
  {
    name: 'backend audio urls are carried through untouched',
    run() {
      const { manager, speaker } = makeManager();
      manager.push(
        msg('spotter', 'critical', 'Car left!', { audio_url: '/api/v1/pitwall/audio/abc123' })
      );
      assertEqual(speaker.last.message.audio_url, '/api/v1/pitwall/audio/abc123');
    },
  },
  {
    name: 'an unknown priority is treated as info, not as an error',
    run() {
      const { manager } = makeManager();
      assertEqual(priorityRank('nonsense'), priorityRank('info'));
      const result = manager.push(msg('spotter', 'URGENT!!', 'Something.'));
      assertEqual(result.message.priority, 'info');
      assertEqual(result.action, 'speaking');
    },
  },
  {
    name: 'an empty call is dropped rather than spoken as silence',
    run() {
      const { manager, speaker } = makeManager();
      const result = manager.push(msg('coach', 'info', '   '));
      assertEqual(result.action, 'dropped');
      assertEqual(result.reason, 'empty');
      assertEqual(speaker.spoken.length, 0);
    },
  },
  {
    name: 'the synthetic scenario: four agents, one channel, nothing overlaps',
    run() {
      const { manager, speaker, clock } = makeManager();
      const heard = [];
      const say = (m) => {
        manager.push(m);
        // Every call runs to completion unless something cuts it off.
        while (manager.speaking) {
          const current = manager.speaking;
          heard.push(`${current.agent}:${current.priority}`);
          clock.advance(2000);
          manager.finished(current.message_id);
        }
      };
      say(msg('strategist', 'advisory', 'Window opens lap five, fuel only.', { subject: 'pit_stop' }));
      say(msg('vehicle_engineer', 'advisory', 'Left front is over temperature.', { subject: 'tyres' }));
      say(msg('spotter', 'critical', 'Yellow in sector two.'));
      say(msg('coach', 'info', 'Good rotation there.'));
      assertEqual(heard, [
        'strategist:advisory',
        'vehicle_engineer:advisory',
        'spotter:critical',
        'coach:info',
      ]);
      assertEqual(speaker.cancelled, 0, 'nobody talked over anybody');
      assertEqual(manager.counters.spoken, 4);
      assert(manager.idle, 'the channel is clear at the end');
    },
  },
];

/**
 * Run every case. Returns a plain result object so a browser page and a node
 * script can render it however they like.
 */
export function runAudioTests() {
  const results = CASES.map((testCase) => {
    try {
      testCase.run();
      return { name: testCase.name, ok: true, error: '' };
    } catch (error) {
      return { name: testCase.name, ok: false, error: String((error && error.message) || error) };
    }
  });
  const failed = results.filter((r) => !r.ok);
  return {
    total: results.length,
    passed: results.length - failed.length,
    failed: failed.length,
    ok: failed.length === 0,
    results,
  };
}

export default runAudioTests;
