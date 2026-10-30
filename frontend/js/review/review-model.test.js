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
    wall_ms: 1000 + seq,
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
