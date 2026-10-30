/**
 * Deterministic tests for the pitwall view model.
 *
 * Same bargain as the audio-discipline suite: the parts that can be quietly
 * wrong -- an amber fuel margin that should be red, a pit window drawn off the
 * end of its axis, a "closing" arrow on a car that never moved -- are pure
 * functions over plain objects, so they are asserted rather than squinted at.
 *
 * Run in a browser:  open frontend/pitwall-test.html
 * Run headlessly:    node frontend/js/run-pitwall-tests.mjs
 */

import { NA, gap, lapTime, num, trendArrow } from './format.js';
import {
  FuelHistory,
  GapTrendTracker,
  TowerWarnings,
  connectionView,
  eventLine,
  llmView,
  marginBand,
  recommendationView,
  statusView,
  strategyView,
  towerView,
  tyreView,
} from './view-model.js';

// --------------------------------------------------------------------------
// harness
// --------------------------------------------------------------------------
function assert(condition, message) {
  if (!condition) throw new Error(message || 'assertion failed');
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message || 'expected'}: got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);
  }
}

/** A RaceState with every capability off and every number missing. */
function emptyState(overrides = {}) {
  return {
    version: 1,
    tick: 0,
    session_time: 0,
    stale: false,
    stale_reason: null,
    session: {},
    flags: { phase: 'unknown', active: [] },
    standings: { cars: [], player_idx: null, gap_basis: null },
    player: {},
    fuel: {},
    tyres: { temps: {}, pressures: {}, temp_trend: {}, pressure_trend: {}, stint_laps: 0 },
    car_health: {},
    conditions: {},
    capabilities: { missing: [] },
    metrics: { frames: 0, events: 0 },
    ...overrides,
  };
}

/** A plausible mid-race state: fuel, tyres, standings, all backed. */
function racingState(overrides = {}) {
  const state = emptyState({
    session_time: 300,
    session: {
      source: 'replay',
      state: 'Racing',
      laps_remaining: 7,
      laps_total: 12,
      track_name: 'Test Circuit',
    },
    flags: { phase: 'green', active: ['green'] },
    player: { lap: 5, position: 4, stint: 1, laps_on_tyres: 4, stint_start_lap: 1 },
    fuel: {
      level: 3.0,
      capacity: 4.0,
      per_lap: 0.5,
      per_lap_std: 0.02,
      samples: 4,
      laps_remaining: 6.0,
      laps_to_finish: 7.0,
      margin_laps: -1.0,
      margin_l: -0.5,
      pit_window_earliest_lap: 5,
      pit_window_latest_lap: 11,
      window_open: true,
    },
    tyres: {
      temps: { LF: 88.0, RF: 91.0, LR: 84.0, RR: 85.0 },
      pressures: { LF: 165.0, RF: 166.0, LR: 160.0, RR: 161.0 },
      temp_trend: { LF: 3.4, RF: -0.1 },
      pressure_trend: {},
      stint_laps: 4,
    },
    conditions: { air_temp: 24.5, track_temp: 33.0, track_temp_trend: 0.4 },
    capabilities: {
      session: true, flags: true, standings: true, player: true,
      fuel: true, tyres: true, conditions: true, missing: [],
    },
  });
  return { ...state, ...overrides };
}

function field(count, playerAt = 3) {
  const cars = [];
  for (let i = 0; i < count; i += 1) {
    cars.push({
      idx: i,
      is_player: i === playerAt,
      position: i + 1,
      lap: 5,
      laps_completed: 5 - i * 0.01,
      last_lap_time: 92.5 + i * 0.2,
      best_lap_time: 92.0,
      on_pit_road: false,
      gap_to_player: (playerAt - i) * 1.2,
    });
  }
  return cars;
}

// --------------------------------------------------------------------------
// cases
// --------------------------------------------------------------------------
const CASES = [
  // ---- formatting ------------------------------------------------------
  {
    name: 'a missing number is n/a, never zero',
    run() {
      assertEqual(num(null, 2), NA);
      assertEqual(num(undefined), NA);
      assertEqual(gap(null), NA);
      assertEqual(lapTime(null), NA);
      assertEqual(lapTime(0), NA, 'a zero lap time has not been set yet');
      assertEqual(trendArrow(null), '', 'an unknown trend draws nothing at all');
    },
  },
  {
    name: 'lap times and gaps read the way a driver reads them',
    run() {
      assertEqual(lapTime(92.418), '1:32.418');
      assertEqual(lapTime(52.104), '52.104');
      assertEqual(gap(1.234), '+1.23');
      assertEqual(gap(-0.4), '−0.40');
      assertEqual(gap(0), '0.00');
    },
  },

  // ---- status strip ----------------------------------------------------
  {
    name: 'laps remaining wins over time remaining when the session has both',
    run() {
      const view = statusView(racingState({
        session: { laps_remaining: 7, laps_total: 12, time_remaining: 900 },
      }));
      assertEqual(view.remaining.kind, 'laps');
      assertEqual(view.remaining.text, '7 / 12');
    },
  },
  {
    name: 'a timed session falls back to the clock, and an unknown one to n/a',
    run() {
      const timed = statusView(racingState({ session: { time_remaining: 3723 } }));
      assertEqual(timed.remaining.kind, 'time');
      assertEqual(timed.remaining.seconds, 3723);
      const unknown = statusView(emptyState());
      assertEqual(unknown.remaining.kind, 'unknown');
      assertEqual(unknown.remaining.text, NA);
    },
  },
  {
    name: 'the flag band only takes the screen when it is not green',
    run() {
      assertEqual(statusView(racingState()).flag.alert, false);
      const yellow = statusView(racingState({ flags: { phase: 'yellow', active: ['yellow'] } }));
      assertEqual(yellow.flag.alert, true);
      assert(yellow.flag.label.includes('YELLOW'));
      assertEqual(statusView(emptyState()).flag.alert, true, 'unknown is not green');
    },
  },
  {
    name: 'conditions are n/a rather than zero when the session has no weather channels',
    run() {
      const view = statusView(emptyState());
      assertEqual(view.conditions.available, false);
      assertEqual(view.conditions.track, undefined);
    },
  },

  // ---- connection ------------------------------------------------------
  {
    name: 'a closed socket outranks everything: nothing on screen is current',
    run() {
      const view = connectionView(racingState({ stale: true }), { socket: false, replay: { running: true } });
      assertEqual(view.state, 'offline');
    },
  },
  {
    name: 'a stale race state outranks live, because frozen numbers read as current ones',
    run() {
      const view = connectionView(
        racingState({ stale: true, stale_reason: 'iRacing disconnected' }),
        { socket: true }
      );
      assertEqual(view.state, 'stale');
      assertEqual(view.detail, 'iRacing disconnected');
    },
  },
  {
    name: 'a running replay is reported as a replay, with its speed',
    run() {
      const view = connectionView(racingState(), {
        socket: true,
        replay: { running: true, session_id: 'scenario', speed: 4, frames: 120 },
      });
      assertEqual(view.state, 'replay');
      assert(view.detail.includes('4×'));
    },
  },
  {
    name: 'live once frames have actually arrived, and "no data" before that',
    run() {
      const live = connectionView(
        racingState({ session: { source: 'live' }, metrics: { frames: 3000 } }),
        { socket: true }
      );
      assertEqual(live.state, 'live');
      assertEqual(connectionView(emptyState(), { socket: true }).state, 'idle');
    },
  },

  // ---- LLM status ------------------------------------------------------
  {
    name: 'an unmounted agent layer is a state, not an error',
    run() {
      const view = llmView({ available: false, reason: 'not mounted' });
      assertEqual(view.available, false);
      assertEqual(view.label, 'AI OFF');
      assertEqual(view.tone, 'muted');
    },
  },
  {
    name: 'calls used and the session ceiling are both shown, and a breaker warns',
    run() {
      const ok = llmView({
        available: true, enabled: true, healthy: true,
        calls_used: 24, max_calls_per_session: 400, agents: [],
      });
      assertEqual(ok.tone, 'ok');
      assert(ok.detail.includes('24/400'));
      const bad = llmView({
        available: true, enabled: true, healthy: true,
        calls_used: 24, max_calls_per_session: 400,
        agents: [{ name: 'spotter', disabled_reason: 'breaker' }],
      });
      assertEqual(bad.tone, 'warn');
      assert(bad.detail.includes('spotter'));
    },
  },

  // ---- strategy --------------------------------------------------------
  {
    name: 'no fuel channels means no window and no invented numbers',
    run() {
      const view = strategyView(emptyState());
      assertEqual(view.available, false);
      assertEqual(view.window, null);
      assertEqual(view.fuel.perLap, null);
      assertEqual(view.fuel.band, 'unknown');
    },
  },
  {
    name: 'the pit-window axis contains every mark it has to draw',
    run() {
      const view = strategyView(racingState());
      const w = view.window;
      assert(w.first <= w.earliest && w.last >= w.latest, 'window edges are on the axis');
      assert(w.first <= w.lap && w.last >= w.lap, 'the current lap is on the axis');
      assert(w.last >= w.fuelLimitLap, 'the dry lap is on the axis');
      assert(w.last >= w.finishLap, 'the finish is on the axis');
      assertEqual(w.fuelLimitLap, 11, 'lap 5 + 6.0 laps of fuel');
      assertEqual(w.finishLap, 12, 'lap 5 + 7 laps to run');
    },
  },
  {
    name: 'the fuel margin bands where a strategist would band it',
    run() {
      assertEqual(marginBand(null), 'unknown');
      assertEqual(marginBand(-0.01), 'red', 'negative slack means a stop is required');
      assertEqual(marginBand(0.5), 'amber');
      assertEqual(marginBand(1.0), 'green');
      assertEqual(marginBand(6.0), 'green');
    },
  },
  {
    name: 'a corner with no channel is null, and the ones that exist keep their trend',
    run() {
      const view = tyreView(racingState({
        tyres: {
          temps: { LF: 88.0 }, pressures: {}, temp_trend: { LF: 3.4 },
          pressure_trend: {}, stint_laps: 4,
        },
      }));
      assertEqual(view.available, true);
      const byKey = Object.fromEntries(view.corners.map((c) => [c.key, c]));
      assertEqual(byKey.LF.temp, 88.0);
      assertEqual(byKey.LF.tempTrend, 3.4);
      assertEqual(byKey.RF.temp, null, 'a corner nobody measured is null, not 0');
      assertEqual(byKey.RF.pressure, null);
      assertEqual(view.corners.length, 4, 'all four corners are always shown');
    },
  },
  {
    name: 'axle imbalance needs both sides of the axle',
    run() {
      const full = tyreView(racingState());
      assertEqual(Math.round(full.imbalance.front * 10) / 10, -3.0);
      const half = tyreView(racingState({
        tyres: { temps: { LF: 88 }, pressures: {}, temp_trend: {}, pressure_trend: {}, stint_laps: 1 },
      }));
      assertEqual(half.imbalance.front, null);
    },
  },
  {
    name: 'the strategist summary copies only what the agent contract carried',
    run() {
      const view = recommendationView({
        agent: 'strategist',
        spoken_text: 'Box this lap, three litres, we come out P8.',
        grounded: true,
        refused: false,
        event_ref: { lap: 5 },
        data: { recommendation: 'box_now', target_lap: 5, fuel_to_add_l: 3.0, rejoin_position: 8, confidence: 0.8, risks: ['traffic'] },
      });
      assertEqual(view.call, 'BOX NOW');
      assert(view.parts.includes('lap 5'));
      assert(view.parts.includes('rejoin P8'));
      assertEqual(view.risks.length, 1);
      const refused = recommendationView({ refused: true, data: {}, spoken_text: 'Standby.' });
      assertEqual(refused.call, 'REFUSED');
      assertEqual(refused.parts.length, 0, 'a refusal carries no figures');
    },
  },

  // ---- timing tower ----------------------------------------------------
  {
    name: 'collapsed, the tower is the three cars either side of us',
    run() {
      const state = racingState({ standings: { cars: field(12, 5), gap_basis: 'lap_time_pct' } });
      const view = towerView(state);
      assertEqual(view.rows.length, 7);
      assertEqual(view.total, 12);
      assertEqual(view.hidden, 5);
      assert(view.rows.some((r) => r.isPlayer), 'the player is in the window');
      assertEqual(view.rows[3].isPlayer, true, 'and is in the middle of it');
    },
  },
  {
    name: 'expanded, it is the whole field, still in running order',
    run() {
      const state = racingState({ standings: { cars: field(12, 5), gap_basis: 'lap_time_pct' } });
      const view = towerView(state, { expanded: true });
      assertEqual(view.rows.length, 12);
      assertEqual(view.hidden, 0);
      const positions = view.rows.map((r) => r.position);
      assertEqual(String(positions), String([...positions].sort((a, b) => a - b)));
    },
  },
  {
    name: 'with no gap basis every gap is null, and the tower says why once',
    run() {
      const cars = field(8, 3).map((car) => ({ ...car, gap_to_player: null }));
      const view = towerView(racingState({ standings: { cars, gap_basis: null } }));
      assertEqual(view.gapsKnown, false);
      assert(view.rows.every((r) => r.gapToPlayer === null));
    },
  },
  {
    name: 'no standings channels at all is reported, not rendered as an empty field',
    run() {
      const view = towerView(emptyState());
      assertEqual(view.available, false);
      assertEqual(view.rows.length, 0);
    },
  },
  {
    name: 'a lap time of zero has not been set, so it shows as unknown',
    run() {
      const cars = field(8, 3).map((car) => ({ ...car, last_lap_time: 0 }));
      const view = towerView(racingState({ standings: { cars, gap_basis: 'lap_time_pct' } }));
      assert(view.rows.every((r) => r.lastLap === null));
    },
  },

  // ---- gap trends ------------------------------------------------------
  {
    name: 'a car eating into our gap is closing, one dropping away is opening',
    run() {
      const tracker = new GapTrendTracker();
      const make = (t, gapToPlayer) => racingState({
        session_time: t,
        standings: { gap_basis: 'lap_time_pct', cars: [{ idx: 7, gap_to_player: gapToPlayer }] },
      });
      tracker.update(make(0, -2.0));
      tracker.update(make(2, -1.0));
      assertEqual(tracker.trend(7), 'closing');
      tracker.update(make(4, -3.0));
      assertEqual(tracker.trend(7), 'opening');
      tracker.update(make(6, -3.02));
      assertEqual(tracker.trend(7), 'steady');
    },
  },
  {
    name: 'a change of gap basis is discarded, not reported as everyone moving at once',
    run() {
      const tracker = new GapTrendTracker();
      tracker.update(racingState({
        session_time: 0,
        standings: { gap_basis: 'lap_length_speed', cars: [{ idx: 7, gap_to_player: -8.0 }] },
      }));
      tracker.update(racingState({
        session_time: 2,
        standings: { gap_basis: 'lap_time_pct', cars: [{ idx: 7, gap_to_player: -1.0 }] },
      }));
      assertEqual(tracker.trend(7), null, 'the ruler changed, not the car');
    },
  },
  {
    name: 'two samples too close together are noise, not a trend',
    run() {
      const tracker = new GapTrendTracker();
      const make = (t, g) => racingState({
        session_time: t,
        standings: { gap_basis: 'lap_time_pct', cars: [{ idx: 7, gap_to_player: g }] },
      });
      tracker.update(make(0, -2.0));
      tracker.update(make(0.1, -1.9));
      assertEqual(tracker.trend(7), null);
      tracker.update(make(1.5, -1.0));
      assertEqual(tracker.trend(7), 'closing', 'the older anchor was kept');
    },
  },

  // ---- tower warnings --------------------------------------------------
  {
    name: 'a blue flag pins to its car and ages out on session time',
    run() {
      const warnings = new TowerWarnings(10);
      warnings.observe({ event_type: 'blue_flag', session_time: 100, payload: { car_idx: 3, gap: 1.2 } });
      warnings.clock(101);
      assertEqual(warnings.for(3).kind, 'blue_flag');
      assertEqual(warnings.for(4), null);
      warnings.clock(120);
      assertEqual(warnings.for(3), null, 'it expired on the session clock, not the wall clock');
    },
  },
  {
    name: 'an event with no car index cannot warn about a car',
    run() {
      const warnings = new TowerWarnings();
      warnings.observe({ event_type: 'lockup', session_time: 1, payload: { wheel: 'RF' } });
      warnings.observe({ event_type: 'blue_flag', session_time: 1, payload: {} });
      warnings.clock(2);
      assertEqual(warnings.for(0), null);
    },
  },

  // ---- fuel history ----------------------------------------------------
  {
    name: 'the consumption sparkline takes one point per lap and refreshes it in place',
    run() {
      const history = new FuelHistory();
      history.update(racingState({ player: { lap: 4 }, fuel: { per_lap: 0.50 } }));
      history.update(racingState({ player: { lap: 4 }, fuel: { per_lap: 0.52 } }));
      assertEqual(history.points().length, 1);
      assertEqual(history.points()[0].perLap, 0.52);
      history.update(racingState({ player: { lap: 5 }, fuel: { per_lap: 0.49 } }));
      assertEqual(history.points().length, 2);
      assertEqual(history.points()[1].lap, 5);
    },
  },
  {
    name: 'a lap with no learned consumption contributes no point',
    run() {
      const history = new FuelHistory();
      history.update(emptyState({ player: { lap: 2 }, fuel: {} }));
      history.update(racingState({ player: {}, fuel: { per_lap: 0.5 } }));
      assertEqual(history.points().length, 0);
    },
  },

  // ---- ticker ----------------------------------------------------------
  {
    name: 'every event type the catalog can emit renders a line',
    run() {
      const line = eventLine({
        event_type: 'lockup', key: 'lockup', severity: 'advisory',
        lap: 4, session_time: 42.5, payload: { wheel: 'RF', slip: 0.42 },
      });
      assertEqual(line.lap, 4);
      assertEqual(line.severity, 'advisory');
      assert(line.text.includes('RF'));
      assert(line.text.includes('0.42'));

      const flag = eventLine({
        event_type: 'flag_change', key: 'flag_change:yellow', severity: 'critical',
        lap: 3, session_time: 30, payload: { from: 'green', to: 'yellow' },
      });
      assertEqual(flag.key, 'flag_change:yellow');
      assert(flag.text.includes('yellow'));
    },
  },
  {
    name: 'an event type this UI has never heard of is still shown, not dropped',
    run() {
      const line = eventLine({
        event_type: 'something_new', key: 'something_new', severity: 'info',
        lap: 1, session_time: 1, payload: { a: 1, b: 2 },
      });
      assert(line !== null);
      assert(line.text.includes('a=1'));
    },
  },
  {
    name: 'a director-injected event keeps its provenance in the ticker',
    run() {
      const line = eventLine({
        event_type: 'flag_change', key: 'flag_change:yellow', severity: 'critical',
        lap: 3, session_time: 30,
        payload: { from: 'green', to: 'yellow', injected: true, director_id: 'fcy_lap_3' },
      });
      assertEqual(line.injected, true);
      assertEqual(line.directorId, 'fcy_lap_3');
      const real = eventLine({ event_type: 'flag_change', key: 'flag_change:yellow', payload: { to: 'yellow' } });
      assertEqual(real.injected, false);
    },
  },
  {
    name: 'an off-track with no lap distance does not claim it happened at 0%',
    run() {
      const line = eventLine({
        event_type: 'offtrack', key: 'offtrack', severity: 'advisory',
        lap: 2, session_time: 20, payload: { surface: 'off_track' },
      });
      assert(line.text.includes(NA), line.text);
    },
  },
];

export function runPitwallTests() {
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

export default runPitwallTests;
