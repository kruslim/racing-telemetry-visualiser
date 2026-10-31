/**
 * Tests for the shared live/review components' pure parts: the activity model,
 * the timeline geometry and the onboard panel's view. Run by `node frontend/js/run-shared-tests.mjs`, and
 * by pytest when node is installed. No DOM, no dependencies.
 */

import { apply, createModel, edgesFor, recentRuns, reduce } from './activity-model.js';
import { clock, layoutLane, scaleX, ticks } from './timeline.js';
import { onboardView, videoTimeFor } from '../live/onboard-panel.js';

/** One real run as the backend emits it (see tests/test_pitwall_activity.py). */
function runRecords() {
  let seq = 0;
  const r = (kind, extra = {}) => ({
    seq: ++seq,
    wall_ms: 1000 + seq * 100,
    session_time: 42 + seq * 0.01,
    tick: 2520,
    kind,
    agent: 'strategist',
    run_id: 'strategist-1',
    event_key: 'pit_window_open',
    data: {},
    ...extra,
  });
  return [
    r('dispatch', { agent: null, run_id: null, data: { accepted: ['strategist'], skipped: { coach: 'cooldown' }, blocked: null } }),
    r('run_start', { data: { trigger: 'pit_window_open', model: 'kimi-k3', context_radio: [{ message_id: 'm0', agent: 'spotter' }] } }),
    r('llm_call_start', { data: { call: 1, model: 'kimi-k3' } }),
    r('llm_call_end', { data: { call: 1, ok: true, latency_ms: 900, tool_calls: ['get_fuel_projection'] } }),
    r('tool_call', { data: { tool: 'get_fuel_projection', ok: true, latency_ms: 3, args: {}, result: { type: 'object', keys: ['laps'], size: 1 } } }),
    r('llm_call_start', { data: { call: 2, model: 'kimi-k3' } }),
    r('llm_call_end', { data: { call: 2, ok: true, latency_ms: 700, tool_calls: [] } }),
    r('validate', { data: { ok: true, unsupported: [], repaired: false } }),
    r('run_end', { data: { outcome: 'radio', message_id: 'm1', turns: 2, latency_ms: 1650 } }),
    r('radio_queued', { run_id: null, data: { message_id: 'm1' } }),
    r('radio_aired', { run_id: null, data: { message_id: 'm1' } }),
  ];
}

const TESTS = [
  ['a full run folds into one run with two model calls and a tool call', () => {
    const model = reduce(runRecords());
    const [run] = recentRuns(model);
    eq(run.runId, 'strategist-1');
    eq(run.spans.map((s) => s.kind).join(','), 'llm,tool,llm');
    eq(run.outcome, 'radio');
    eq(run.turns, 2);
    eq(model.llm.calls, 2);
    eq(model.tools.calls, 1);
    eq(model.radio.aired, 1);
    eq(model.agents.strategist.status, 'idle');
    eq(model.agents.coach.skipped, 'cooldown');
    eq(model.dispatch.cooldown, 1);
  }],
  ['an agent is thinking between run_start and run_end', () => {
    const model = reduce(runRecords().slice(0, 3));
    eq(model.agents.strategist.status, 'thinking');
    eq(model.llm.inflight, 1);
  }],
  ['unknown kinds are ignored, by contract with the recorder', () => {
    const model = createModel();
    apply(model, { seq: 1, kind: 'state', data: { anything: true } });
    apply(model, { seq: 2, kind: 'race_event', data: {} });
    eq(model.runs.size, 0);
    eq(model.lastSeq, 2);
  }],
  ['a replayed seq is not applied twice', () => {
    const model = createModel();
    const records = runRecords();
    records.forEach((r) => apply(model, r));
    records.forEach((r) => apply(model, r));
    eq(model.llm.calls, 2);
  }],
  ['reduce up to a session time rebuilds the state at that cursor', () => {
    const records = runRecords();
    const atCall = reduce(records, records[2].session_time);
    eq(atCall.agents.strategist.status, 'thinking');
    const atEnd = reduce(records, 1e9);
    eq(atEnd.agents.strategist.status, 'idle');
  }],
  ['context radio becomes radio -> agent edges coloured by who spoke', () => {
    const edges = edgesFor(runRecords()[1]);
    eq(edges.length, 1);
    eq(edges[0].from, 'radio');
    eq(edges[0].to, 'strategist');
    eq(edges[0].source, 'spotter');
  }],
  ['dispatch fans out from the orchestrator to each accepted agent', () => {
    const edges = edgesFor(runRecords()[0]);
    eq(edges.map((e) => `${e.from}>${e.to}`).join(','), 'bus>orchestrator,orchestrator>strategist');
  }],
  ['a driver message goes onto the radio, not from it', () => {
    const [edge] = edgesFor({ kind: 'radio_aired', agent: 'driver', data: {} });
    eq(`${edge.from}>${edge.to}`, 'driver>radio');
  }],
  ['timeline scale maps the range onto the width', () => {
    eq(scaleX(50, [0, 100], 200), 100);
    eq(scaleX(0, [0, 0], 200), 0);
  }],
  ['timeline lanes drop items outside the range and stack overlapping spans', () => {
    const placed = layoutLane(
      [
        { t0: 10, t1: 40 },
        { t0: 20, t1: 30 },
        { t0: 500 },
      ],
      [0, 100],
      100
    );
    eq(placed.length, 2);
    eq(placed[0].row, 0);
    eq(placed[1].row, 1);
  }],
  ['timeline ticks land on round numbers', () => {
    eq(ticks([0, 120], 6).join(','), '0,20,40,60,80,100,120');
    eq(ticks([5, 5]).length, 0);
  }],
  ['clock formats session time', () => {
    eq(clock(125), '2:05');
    eq(clock(3725.4), '1:02:05');
  }],
  ['the onboard panel is empty with no source and says how to turn the VLM on', () => {
    const view = onboardView({ mode: 'off', capture: { running: false }, source: null, vlm: { enabled: false } });
    eq(view.mode, 'empty');
    eq(view.vlmLine.includes('RTV_VISION_MODE=live'), true);
    eq(view.notes.length, 0);
  }],
  ['a running capture streams and lists live notes newest first', () => {
    const view = onboardView({
      mode: 'live',
      capture: { running: true, session_id: 's1', frames: 42 },
      source: { kind: 'capture', video_id: 'v1' },
      vlm: {
        enabled: true, calls: 2, max_calls: 10, pending: 1, skipped: { merged: 1 },
        recent: [
          { note_id: 'a', lap: 3, trigger: 'offtrack', status: 'ok',
            kept: [{ kind: 'kerb_use', text: 'Rode the exit kerb.', confidence: 'high' }] },
          { note_id: 'b', lap: 4, trigger: 'lockup', status: 'trigger_not_seen', kept: [] },
        ],
      },
    });
    eq(view.mode, 'capture');
    eq(view.sourceLine, 'capturing the sim window · 42 frames stamped');
    eq(view.vlmLine, 'Live VLM on · 2 of 10 calls · 1 looking · 1 merged');
    eq(view.notes[0].id, 'b');
    eq(view.notes[0].head.includes('check the sync'), true);
    eq(view.notes[0].empty, 'The frames did not show the flagged moment.');
    eq(view.notes[1].empty, null);
    eq(view.notes[1].lines[0].kind, 'kerb use');
    eq(view.notes[1].lines[0].cls, 'conf-high');
  }],
  ['a replayed recording maps session time onto its own clock', () => {
    const sync = { synced: true, rate: 1.001, offset: 90 };
    eq(Math.abs(videoTimeFor(sync, 190.1) - 100) < 1e-9, true);
    eq(videoTimeFor({ synced: false }, 10), null);
    const view = onboardView({ capture: { running: false },
      source: { kind: 'video', video_id: 'v', video: { video_id: 'v', label: 'spa', sync } }, vlm: {} });
    eq(view.mode, 'video');
    eq(view.sourceLine, 'recording: spa');
  }],
];

function eq(actual, expected) {
  if (actual !== expected) {
    throw new Error(`expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

export function runSharedTests() {
  const results = TESTS.map(([name, fn]) => {
    try {
      fn();
      return { name, ok: true };
    } catch (error) {
      return { name, ok: false, error: String(error && error.message ? error.message : error) };
    }
  });
  const passed = results.filter((r) => r.ok).length;
  return { results, passed, total: results.length, ok: passed === results.length };
}
