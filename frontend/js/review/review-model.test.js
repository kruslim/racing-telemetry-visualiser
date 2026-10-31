/**
 * Tests for the post-race review's pure parts. Run by
 * `node frontend/js/run-review-tests.mjs`, and by pytest when node is installed.
 * The records are shaped exactly like a recorded journal (rtv.store.journal).
 */

import {
  auditRows,
  bounds,
  cornerSpan,
  defaultLaps,
  eventsUpTo,
  filterAudit,
  isClean,
  lapAt,
  laneItems,
  pointAtPct,
  radioUpTo,
  toMetres,
  trackEvents,
} from './review-model.js';
import { mergeCompare, stateAt, staticBase, staticPlan } from './static-data.js';

const LAPS = [
  { lap: 0, start_session_time: 20, lap_time: 59, is_valid: false, is_in_lap: false, is_out_lap: false },
  { lap: 1, start_session_time: 79, lap_time: 149.2, is_valid: true, is_in_lap: false, is_out_lap: false },
  { lap: 2, start_session_time: 228.2, lap_time: 141.9, is_valid: true, is_in_lap: false, is_out_lap: false },
  { lap: 3, start_session_time: 370.1, lap_time: 140.6, is_valid: true, is_in_lap: false, is_out_lap: false },
  { lap: 4, start_session_time: 510.7, lap_time: 146.3, is_valid: true, is_in_lap: true, is_out_lap: false },
  { lap: 5, start_session_time: 657.0, lap_time: 164.0, is_valid: true, is_in_lap: false, is_out_lap: true },
  { lap: 6, start_session_time: 821.0, lap_time: 141.0, is_valid: true, is_in_lap: false, is_out_lap: false },
];

function journal() {
  let seq = 0;
  const r = (kind, t, extra = {}) => ({
    seq: ++seq,
    // The wall clock runs with the race clock, as in a real journal: the review
    // puts a run's end on the race clock through it.
    wall_ms: 1_000_000 + Math.round(t * 1000),
    session_time: t,
    tick: Math.round(t * 60),
    kind,
    agent: null,
    run_id: null,
    event_key: null,
    data: {},
    ...extra,
  });
  return [
    r('lifecycle', 20, { data: { phase: 'start', source: 'live' } }),
    r('race_event', 80, { data: { event_type: 'flag_change', severity: 'info', payload: { from: 'unknown', to: 'green' } } }),
    r('race_event', 300, { data: { event_type: 'offtrack', severity: 'advisory', lap: 2, payload: { lap_dist_pct: 0.48 } } }),
    r('race_event', 400, { data: { event_type: 'flag_change', severity: 'critical', payload: { from: 'green', to: 'yellow' } } }),
    r('race_event', 430, { data: { event_type: 'flag_change', severity: 'info', payload: { from: 'yellow', to: 'green' } } }),
    r('race_event', 449, { data: { event_type: 'lap_completed', lap: 3 } }),
    r('race_event', 500, { data: { event_type: 'pit_window_open', severity: 'advisory', lap: 4 } }),
    r('run_start', 500.1, { agent: 'strategist', run_id: 'strategist-1', event_key: 'pit_window_open', data: { model: 'kimi-k3', context_radio: [{ agent: 'spotter' }] } }),
    r('llm_call_end', 501.5, { agent: 'strategist', run_id: 'strategist-1', data: { call: 1, ok: true, latency_ms: 1400 } }),
    r('tool_call', 501.6, { agent: 'strategist', run_id: 'strategist-1', data: { tool: 'get_fuel_projection', ok: true } }),
    r('validate', 503, { agent: 'strategist', run_id: 'strategist-1', data: { ok: false, unsupported: ['12.4'], repaired: true } }),
    r('run_end', 503.2, { agent: 'strategist', run_id: 'strategist-1', data: { outcome: 'radio', message_id: 'm1', latency_ms: 3100 } }),
    r('radio', 504, { agent: 'strategist', data: { agent: 'strategist', message_id: 'm1', spoken_text: 'Box this lap.', grounded: true, refused: false } }),
    r('run_start', 600, { agent: 'coach', run_id: 'coach-1', event_key: 'recurring_issue', data: { model: 'kimi-k3' } }),
    r('run_end', 601, { agent: 'coach', run_id: 'coach-1', data: { outcome: 'refused', latency_ms: 900 } }),
    r('run_start', 700, { agent: 'spotter', run_id: 'spotter-1', event_key: 'traffic_close', data: {} }),
    r('lifecycle', 962, { data: { phase: 'end' } }),
  ];
}

const tests = [];
const test = (name, fn) => tests.push({ name, fn });
function eq(a, b, msg) {
  const ja = JSON.stringify(a);
  const jb = JSON.stringify(b);
  if (ja !== jb) throw new Error(`${msg || 'not equal'}: ${ja} !== ${jb}`);
}
function ok(v, msg) {
  if (!v) throw new Error(msg || 'expected truthy');
}

test('clean laps exclude invalid, in- and out-laps', () => {
  eq(LAPS.filter(isClean).map((l) => l.lap), [1, 2, 3, 6]);
});

test('default comparison is a typical clean lap against the best one', () => {
  eq(defaultLaps(LAPS), { main: 2, ref: 3 }); // median of 140.6, 141.0, 141.9, 149.2
  eq(defaultLaps([]), { main: null, ref: null });
  eq(defaultLaps([LAPS[3]]), { main: 3, ref: 3 });
});

test('lapAt finds the lap being driven', () => {
  eq(lapAt(LAPS, 400).lap, 3);
  eq(lapAt(LAPS, 10), null);
});

test('bounds span the laps and the run', () => {
  eq(bounds({ session_time_start: 20, session_time_end: 962 }, LAPS), [20, 962]);
  eq(bounds(null, []), null);
});

test('lanes: laps are spans, flags close at the next change, agent runs span start to end', () => {
  const lanes = laneItems(journal(), LAPS);
  eq(lanes.laps.length, 7);
  eq(lanes.laps[4].label, 'lap 4 (in)');
  eq(lanes.flags.map((f) => [f.label, f.t0, f.t1]), [
    ['green flag', 80, 400],
    ['yellow flag', 400, 430],
    ['green flag', 430, 962],
  ]);
  // lap_completed and flag_change have their own lanes
  eq(lanes.events.map((e) => e.record.data.event_type), ['offtrack', 'pit_window_open']);
  eq([lanes.strategist[0].t0, lanes.strategist[0].t1], [500.1, 503.2]);
  ok(lanes.coach[0].cls.includes('run-quiet'), 'a refused run is marked quiet');
  eq(lanes.spotter[0].t1, undefined, 'an unfinished run stays a point');
  eq(lanes.radio.map((r) => r.label), ['strategist: Box this lap.']);
});

test('agent records carry the waking time: run ends and radio go on the race clock', () => {
  // As recorded: run_end and radio keep the event's session time (500); the clock
  // records around them (state, radio_aired) say when they really happened.
  const records = [
    { seq: 1, kind: 'state', wall_ms: 10_000, session_time: 500, data: {} },
    { seq: 2, kind: 'run_start', agent: 'strategist', run_id: 'r1', wall_ms: 10_000, session_time: 500, data: {} },
    { seq: 3, kind: 'run_end', agent: 'strategist', run_id: 'r1', wall_ms: 22_000, session_time: 500, data: { outcome: 'radio', message_id: 'm1' } },
    { seq: 4, kind: 'radio', agent: 'strategist', wall_ms: 22_000, session_time: 500, data: { agent: 'strategist', message_id: 'm1' } },
    { seq: 5, kind: 'radio_aired', agent: 'strategist', wall_ms: 23_000, session_time: 513, data: { message_id: 'm1' } },
    { seq: 6, kind: 'state', wall_ms: 30_000, session_time: 520, data: {} },
  ];
  const lanes = laneItems(records, []);
  eq([lanes.strategist[0].t0, lanes.strategist[0].t1], [500, 512]);
  eq(lanes.radio[0].t0, 513);
  eq(radioUpTo(records, 510).length, 0, 'not on air yet');
  eq(radioUpTo(records, 513).length, 1);
});

test('audit rows join each run to what it said', () => {
  const rows = auditRows(journal());
  eq(rows.map((r) => r.runId), ['strategist-1', 'coach-1', 'spotter-1']);
  const s = rows[0];
  eq([s.model, s.llmCalls, s.llmMs, s.tools, s.unsupported, s.repaired], [
    'kimi-k3', 1, 1400, ['get_fuel_projection'], ['12.4'], true,
  ]);
  eq([s.outcome, s.latencyMs, s.spoken, s.grounded, s.contextFrom], [
    'radio', 3100, 'Box this lap.', true, ['spotter'],
  ]);
  eq([rows[1].outcome, rows[1].spoken], ['refused', null]);
  eq(rows[2].outcome, null);
});

test('audit filter and sort', () => {
  const rows = auditRows(journal());
  eq(filterAudit(rows, { agent: 'coach' }).map((r) => r.runId), ['coach-1']);
  eq(filterAudit(rows, { outcome: 'unfinished' }).map((r) => r.runId), ['spotter-1']);
  eq(filterAudit(rows, { sort: 'latency' }).map((r) => r.runId), ['strategist-1', 'coach-1', 'spotter-1']);
});

test('radio and events up to the cursor, newest first', () => {
  eq(radioUpTo(journal(), 503).length, 0);
  eq(radioUpTo(journal(), 600).map((r) => r.data.message_id), ['m1']);
  eq(eventsUpTo(journal(), 420).map((r) => r.session_time), [400, 300, 80]);
});

test('geometry helpers', () => {
  eq(toMetres([0, 0.5, 1], 7000), [0, 3500, 7000]);
  eq(cornerSpan({ distance: 390, entry_m: 300, exit_m: 450 }), [300, 450]);
  eq(cornerSpan({ distance: 390 }), [330, 450]);
  const points = [{ lap_dist_pct: 0.01 }, { lap_dist_pct: 0.5 }, { lap_dist_pct: 0.99 }];
  eq(pointAtPct(points, 0.999).lap_dist_pct, 0.99);
  eq(pointAtPct(points, 0.002).lap_dist_pct, 0.01);
  eq(trackEvents(journal()).map((e) => [e.type, e.pct]), [['offtrack', 0.48]]);
});

test('static mode only reads a relative export folder', () => {
  eq(staticBase('../data/spa'), '../data/spa/');
  eq(staticBase('data/spa/'), 'data/spa/');
  for (const bad of [null, '', 'https://x.test/', '//x.test/', '/data/', 'javascript:alert(1)', '..\\x']) {
    eq(staticBase(bad), null);
  }
});

test('static plan: the file layout scripts/export_session.py writes', () => {
  const S = 'fe4b';
  const R = '20261031T043329Z-01-replay';
  eq(staticPlan('/sessions?limit=200'), { file: 'sessions.json' });
  eq(staticPlan(`/sessions/${S}/laps`), { file: `sessions/${S}/laps.json` });
  eq(staticPlan(`/sessions/${S}/pitwall-runs`), { file: `sessions/${S}/pitwall-runs.json` });
  eq(staticPlan(`/sessions/${S}/summary?run=${R}`), { file: `sessions/${S}/summary/${R}.json` });
  eq(staticPlan(`/sessions/${S}/summary`), { file: `sessions/${S}/summary/none.json` });
  eq(staticPlan(`/sessions/${S}/pitwall-log?run=${R}&limit=200000`), {
    file: `sessions/${S}/pitwall-log/${R}.json`,
  });
  eq(staticPlan(`/sessions/${S}/pitwall-log/state?run=${R}&t=12.5`), {
    state: { file: `sessions/${S}/pitwall-log/${R}-state.json`, t: 12.5 },
  });
  eq(staticPlan(`/sessions/${S}/trackmap?lap=6&max_points=1500`), { file: `sessions/${S}/trackmap/6.json` });
  eq(staticPlan(`/sessions/${S}/compare?name=Speed&laps=9,6&grid=1000`), {
    compare: { name: 'Speed', laps: ['9', '6'], files: [`sessions/${S}/compare/Speed/9.json`, `sessions/${S}/compare/Speed/6.json`] },
  });
  const base = `session_id=${S}&main_lap=9&ref_lap=6`;
  eq(staticPlan(`/coaching/lap-findings?${base}`), { file: `sessions/${S}/lap-findings/9-6.json` });
  eq(staticPlan(`/coaching/delta?${base}`), { file: `sessions/${S}/delta/9-6.json` });
  eq(staticPlan('/pitwall/status'), null);
  eq(staticPlan(`/sessions/${S}/pitwall-log`), null);
});

test('static compare merges per-lap files; state is the last snapshot at or before t', () => {
  const a = { channel: 'Speed', x_values: [0, 1], laps: { 9: [1, 2] } };
  const b = { channel: 'Speed', x_values: [0, 1], laps: { 6: [3, 4] } };
  eq(mergeCompare([a, null, b]).laps, { 9: [1, 2], 6: [3, 4] });
  eq(mergeCompare([null]), null);
  const states = [
    { kind: 'state', session_time: 10, data: 'a' },
    { kind: 'state', session_time: 20, data: 'b' },
  ];
  eq(stateAt(states, 15).data, 'a');
  eq(stateAt(states, 25).data, 'b');
  eq(stateAt(states, 5).data, 'a');
  eq(stateAt([], 5), null);
});

export function runReviewTests() {
  const results = tests.map(({ name, fn }) => {
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
