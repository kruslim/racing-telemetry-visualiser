/**
 * RaceState -> what a pit stand actually shows. Pure functions, no DOM.
 *
 * The panels in `js/pitwall/panels/*.js` are deliberately dumb: they take one of
 * the view objects built here and paint it. Everything that can be *wrong* --
 * which lap the pit window bar starts at, whether a fuel margin is amber or red,
 * whether a rival is closing or just nearby, which cars are "around" the player
 * -- is decided in this file, where it can be asserted without a browser
 * (js/pitwall/view-model.test.js, run headlessly by
 * `node frontend/js/run-pitwall-tests.mjs`).
 *
 * The one rule everything here obeys: a value the session could not back stays
 * `null` and renders as `n/a`. `RaceState.capabilities` says which groups exist
 * at all, so a car with no tyre channels gets an honest empty widget rather than
 * four zeroes.
 */

import { NA, gap, isNum, lapTime, num, signed } from './format.js';

/** Fuel margin, in laps, at which the strategist stops being relaxed. */
export const MARGIN_AMBER_LAPS = 1.0;

/** How many cars either side of the player the tower shows when collapsed. */
export const TOWER_WINDOW = 3;

/** A gap must move at least this fast (s per s) before it is "closing". */
export const GAP_TREND_RATE = 0.05;

/** Minimum seconds between two gap samples before a rate means anything. */
export const GAP_TREND_MIN_DT = 1.0;

/** Degrees per lap / kPa per lap that count as a real tyre trend. */
export const TYRE_TEMP_DEADBAND = 0.5;
export const TYRE_PRESSURE_DEADBAND = 0.2;

/** A traffic or blue-flag warning stays on a tower row this long (session s). */
export const WARNING_TTL_S = 12.0;

const CORNERS = ['LF', 'RF', 'LR', 'RR'];

const FLAG_LABELS = {
  green: 'GREEN',
  yellow: 'YELLOW — FULL COURSE CAUTION',
  red: 'RED FLAG',
  white: 'WHITE — FINAL LAP',
  checkered: 'CHEQUERED FLAG',
  unknown: 'FLAG UNKNOWN',
};

// --------------------------------------------------------------------------
// status strip
// --------------------------------------------------------------------------
/**
 * The top band. `ctx` carries what the socket and the REST polls know and the
 * race state cannot: whether we are connected, and what the agent layer is doing.
 */
export function statusView(state, ctx = {}) {
  const session = (state && state.session) || {};
  const flags = (state && state.flags) || {};
  const conditions = (state && state.conditions) || {};
  const phase = flags.phase || 'unknown';

  return {
    session: {
      state: session.state || NA,
      source: session.source || NA,
      track: session.track_name || session.track_id || NA,
      car: session.car_id || NA,
    },
    remaining: remainingView(session),
    flag: {
      phase,
      label: FLAG_LABELS[phase] || String(phase).toUpperCase(),
      active: Array.isArray(flags.active) ? flags.active : [],
      // Green is the absence of news: the band only takes the screen when
      // something is actually being flagged.
      alert: phase !== 'green',
    },
    conditions: {
      track: conditions.track_temp,
      air: conditions.air_temp,
      trackTrend: conditions.track_temp_trend,
      airTrend: conditions.air_temp_trend,
      available: !!(state && state.capabilities && state.capabilities.conditions),
    },
    connection: connectionView(state, ctx),
    llm: llmView(ctx.pitwallStatus),
  };
}

function remainingView(session) {
  if (isNum(session.laps_remaining)) {
    const total = isNum(session.laps_total) ? ` / ${session.laps_total}` : '';
    return { kind: 'laps', text: `${session.laps_remaining}${total}`, unit: 'laps left' };
  }
  if (isNum(session.time_remaining)) {
    return { kind: 'time', text: null, seconds: session.time_remaining, unit: 'time left' };
  }
  return { kind: 'unknown', text: NA, unit: 'remaining' };
}

/**
 * Four states, in the order they matter. A socket that is down beats everything
 * (nothing on screen is current); a stale race state beats "live", because a
 * frozen "P4, 2.1 s behind" reads identically to a current one.
 */
export function connectionView(state, ctx = {}) {
  if (ctx.socket === false) {
    return { state: 'offline', label: 'OFFLINE', detail: ctx.socketDetail || 'socket closed' };
  }
  if (state && state.stale) {
    return { state: 'stale', label: 'STALE', detail: state.stale_reason || 'telemetry stopped' };
  }
  const replay = ctx.replay || null;
  if (replay && replay.running) {
    const speed = isNum(replay.speed) && replay.speed ? `${replay.speed}×` : 'max';
    return {
      state: 'replay',
      label: 'REPLAY',
      detail: `${replay.session_id || 'scenario'} · ${speed} · ${replay.frames || 0} frames`,
    };
  }
  const source = state && state.session ? state.session.source : null;
  if (source === 'replay') {
    return { state: 'replay', label: 'REPLAY', detail: 'replayed frames' };
  }
  if (state && state.metrics && state.metrics.frames > 0) {
    return { state: 'live', label: 'LIVE', detail: `${state.metrics.frames} frames` };
  }
  return { state: 'idle', label: 'NO DATA', detail: 'waiting for frames' };
}

/** What `/api/v1/pitwall/status` means for a badge in the corner. */
export function llmView(status) {
  if (!status) return { available: false, label: 'AI —', detail: 'status unknown', tone: 'muted' };
  if (!status.available) {
    return {
      available: false,
      label: 'AI OFF',
      detail: status.reason || 'agent layer not mounted',
      tone: 'muted',
    };
  }
  const used = isNum(status.calls_used) ? status.calls_used : null;
  const max = isNum(status.max_calls_per_session) ? status.max_calls_per_session : null;
  const degraded = (status.agents || []).filter((a) => a.degraded || a.disabled_reason);
  const tone = status.budget_exhausted || !status.healthy || degraded.length ? 'warn' : 'ok';
  return {
    available: true,
    label: status.enabled === false ? 'AI MUTED' : 'AI LIVE',
    calls: used,
    maxCalls: max,
    detail:
      `${used === null ? NA : used}${max ? `/${max}` : ''} calls` +
      (degraded.length ? ` · degraded: ${degraded.map((a) => a.name).join(', ')}` : '') +
      (status.budget_exhausted ? ' · budget spent' : ''),
    tone,
  };
}

// --------------------------------------------------------------------------
// strategy
// --------------------------------------------------------------------------
/**
 * The pit-window bar, the fuel block and the tyre widget.
 *
 * The window bar's axis is chosen to contain every mark it has to draw, plus a
 * lap of air either side; drawing a window that runs off the end of the axis
 * would be worse than not drawing it.
 */
export function strategyView(state) {
  const caps = (state && state.capabilities) || {};
  const fuel = (state && state.fuel) || {};
  const player = (state && state.player) || {};
  const session = (state && state.session) || {};
  const lap = isNum(player.lap) ? player.lap : null;

  const fuelLimitLap =
    isNum(lap) && isNum(fuel.laps_remaining) ? lap + fuel.laps_remaining : null;
  const finishLap =
    isNum(lap) && isNum(fuel.laps_to_finish) ? lap + fuel.laps_to_finish : null;

  const marks = [
    lap,
    fuel.pit_window_earliest_lap,
    fuel.pit_window_latest_lap,
    fuelLimitLap,
    finishLap,
  ].filter(isNum);

  const axis = marks.length
    ? { first: Math.max(0, Math.floor(Math.min(...marks)) - 1), last: Math.ceil(Math.max(...marks)) + 1 }
    : null;

  return {
    available: !!caps.fuel,
    window: axis && {
      first: axis.first,
      last: axis.last,
      lap,
      earliest: isNum(fuel.pit_window_earliest_lap) ? fuel.pit_window_earliest_lap : null,
      latest: isNum(fuel.pit_window_latest_lap) ? fuel.pit_window_latest_lap : null,
      fuelLimitLap,
      finishLap,
      open: !!fuel.window_open,
      lapsTotal: isNum(session.laps_total) ? session.laps_total : null,
    },
    fuel: {
      level: fuel.level ?? null,
      capacity: fuel.capacity ?? null,
      perLap: fuel.per_lap ?? null,
      perLapStd: fuel.per_lap_std ?? null,
      samples: fuel.samples || 0,
      lapsRemaining: fuel.laps_remaining ?? null,
      lapsToFinish: fuel.laps_to_finish ?? null,
      marginLaps: fuel.margin_laps ?? null,
      marginL: fuel.margin_l ?? null,
      band: marginBand(fuel.margin_laps),
    },
    tyres: tyreView(state),
    stint: {
      number: player.stint ?? null,
      lapsOnTyres: player.laps_on_tyres ?? null,
      startLap: player.stint_start_lap ?? null,
    },
  };
}

/**
 * Green / amber / red on the fuel margin.
 *
 * `margin_laps` is slack against the *finish*, so negative means a stop is
 * required, not that the car is about to stop on track -- which is exactly why
 * amber starts at one lap of slack rather than at zero.
 */
export function marginBand(marginLaps) {
  if (!isNum(marginLaps)) return 'unknown';
  if (marginLaps < 0) return 'red';
  if (marginLaps < MARGIN_AMBER_LAPS) return 'amber';
  return 'green';
}

export function tyreView(state) {
  const caps = (state && state.capabilities) || {};
  const tyres = (state && state.tyres) || {};
  const temps = tyres.temps || {};
  const pressures = tyres.pressures || {};
  const tempTrend = tyres.temp_trend || {};
  const pressureTrend = tyres.pressure_trend || {};

  const corners = CORNERS.map((key) => ({
    key,
    temp: isNum(temps[key]) ? temps[key] : null,
    pressure: isNum(pressures[key]) ? pressures[key] : null,
    tempTrend: isNum(tempTrend[key]) ? tempTrend[key] : null,
    pressureTrend: isNum(pressureTrend[key]) ? pressureTrend[key] : null,
  }));

  return {
    available: !!caps.tyres,
    stintLaps: tyres.stint_laps || 0,
    corners,
    // The axle spread the vehicle engineer watches, restated for the eye.
    imbalance: {
      front: axleSpread(temps.LF, temps.RF),
      rear: axleSpread(temps.LR, temps.RR),
    },
  };
}

function axleSpread(left, right) {
  if (!isNum(left) || !isNum(right)) return null;
  return left - right;
}

/**
 * The strategist's latest call, reduced to one line for the panel header.
 * Reads only `RadioMessage.data` -- which is the agent's own validated contract,
 * so nothing here can put a figure on screen the validator did not accept.
 */
export function recommendationView(message) {
  if (!message) return null;
  const data = message.data || {};
  const parts = [];
  if (isNum(data.target_lap)) parts.push(`lap ${data.target_lap}`);
  if (isNum(data.fuel_to_add_l)) parts.push(`${num(data.fuel_to_add_l, 1)} L`);
  if (isNum(data.rejoin_position)) parts.push(`rejoin P${data.rejoin_position}`);
  if (isNum(data.confidence)) parts.push(`conf ${num(data.confidence, 2)}`);
  return {
    call: String(data.recommendation || (message.refused ? 'refused' : 'hold'))
      .replace(/_/g, ' ')
      .toUpperCase(),
    parts,
    risks: Array.isArray(data.risks) ? data.risks : [],
    spoken: message.spoken_text || '',
    grounded: message.grounded !== false,
    refused: !!message.refused,
    lap: message.event_ref ? message.event_ref.lap : null,
  };
}

// --------------------------------------------------------------------------
// timing tower
// --------------------------------------------------------------------------
/**
 * The cars either side of us. Collapsed shows P±3, expanded shows the field.
 *
 * Gaps come straight from the engine, which returns `null` for all of them when
 * it has no basis to convert lap-distance into seconds -- so an unknown gap
 * prints `n/a` here rather than a plausible-looking zero.
 */
export function towerView(state, options = {}) {
  const { expanded = false, window = TOWER_WINDOW, trends = null, warnings = null } = options;
  const standings = (state && state.standings) || {};
  const cars = Array.isArray(standings.cars) ? standings.cars.slice() : [];
  const caps = (state && state.capabilities) || {};

  cars.sort(orderCars);

  const playerAt = cars.findIndex((car) => car.is_player);
  const rows = cars.map((car) => ({
    idx: car.idx,
    label: `#${car.idx}`,
    isPlayer: !!car.is_player,
    position: isNum(car.position) ? car.position : null,
    lap: isNum(car.lap) ? car.lap : null,
    lastLap: isNum(car.last_lap_time) && car.last_lap_time > 0 ? car.last_lap_time : null,
    bestLap: isNum(car.best_lap_time) && car.best_lap_time > 0 ? car.best_lap_time : null,
    gapToPlayer: isNum(car.gap_to_player) ? car.gap_to_player : null,
    onPitRoad: car.on_pit_road === true,
    trackSurface: car.track_surface || null,
    trend: trends ? trends.trend(car.idx) : null,
    warning: warnings ? warnings.for(car.idx) : null,
  }));

  let visible = rows;
  let hidden = 0;
  if (!expanded && playerAt >= 0 && rows.length > window * 2 + 1) {
    const first = Math.max(0, playerAt - window);
    const last = Math.min(rows.length, playerAt + window + 1);
    visible = rows.slice(first, last);
    hidden = rows.length - visible.length;
  }

  return {
    available: !!caps.standings,
    gapBasis: standings.gap_basis || null,
    // No basis means every gap in the payload is null. Say so once at the top
    // of the tower instead of printing a column of n/a with no explanation.
    gapsKnown: !!standings.gap_basis,
    rows: visible,
    total: rows.length,
    hidden,
    expanded,
  };
}

function orderCars(a, b) {
  const pa = isNum(a.position) ? a.position : Infinity;
  const pb = isNum(b.position) ? b.position : Infinity;
  if (pa !== pb) return pa - pb;
  const la = isNum(a.laps_completed) ? a.laps_completed : -Infinity;
  const lb = isNum(b.laps_completed) ? b.laps_completed : -Infinity;
  if (la !== lb) return lb - la;
  return a.idx - b.idx;
}

/**
 * Per-car gap trend: closing, opening, or steady.
 *
 * Two guards, both learned upstream. A sample pair closer than
 * `GAP_TREND_MIN_DT` apart is noise; and a pair that straddles a change of
 * `gap_basis` is discarded outright, because when a lap time first becomes known
 * *every* gap moves at once while nobody has moved (the same false positive the
 * backend's traffic detector had to fix).
 */
export class GapTrendTracker {
  constructor(options = {}) {
    this.minDt = options.minDt ?? GAP_TREND_MIN_DT;
    this.rate = options.rate ?? GAP_TREND_RATE;
    this._samples = new Map();
    this._trends = new Map();
  }

  update(state) {
    const standings = (state && state.standings) || {};
    const time = state && isNum(state.session_time) ? state.session_time : null;
    const basis = standings.gap_basis || null;
    if (time === null || !Array.isArray(standings.cars)) return this;

    for (const car of standings.cars) {
      if (!isNum(car.gap_to_player)) continue;
      const previous = this._samples.get(car.idx);
      this._samples.set(car.idx, { gap: car.gap_to_player, time, basis });
      if (!previous) continue;
      const dt = time - previous.time;
      if (dt < this.minDt) {
        this._samples.set(car.idx, previous); // keep the older anchor
        continue;
      }
      if (previous.basis !== basis) continue; // the ruler changed, not the car
      const closing = (Math.abs(previous.gap) - Math.abs(car.gap_to_player)) / dt;
      if (closing > this.rate) this._trends.set(car.idx, 'closing');
      else if (closing < -this.rate) this._trends.set(car.idx, 'opening');
      else this._trends.set(car.idx, 'steady');
    }
    return this;
  }

  trend(idx) {
    return this._trends.get(idx) || null;
  }

  reset() {
    this._samples.clear();
    this._trends.clear();
  }
}

/**
 * Blue flags and closing traffic, pinned to the car they are about.
 *
 * Both are ordinary deterministic events; this just keeps the most recent one
 * per car alive for a few seconds so the tower row can carry the badge. It ages
 * out on *session* time, so a 4x replay warns for the same stretch of race.
 */
export class TowerWarnings {
  constructor(ttl = WARNING_TTL_S) {
    this.ttl = ttl;
    this._byCar = new Map();
    this._now = 0;
  }

  observe(event) {
    if (!event) return this;
    const kind = event.event_type;
    if (kind !== 'blue_flag' && kind !== 'traffic_close' && kind !== 'rival_pitted') return this;
    const idx = (event.payload || {}).car_idx;
    if (!isNum(idx)) return this;
    this._byCar.set(idx, {
      kind,
      at: isNum(event.session_time) ? event.session_time : this._now,
      payload: event.payload || {},
    });
    return this;
  }

  clock(sessionTime) {
    if (isNum(sessionTime)) this._now = sessionTime;
    return this;
  }

  for(idx) {
    const warning = this._byCar.get(idx);
    if (!warning) return null;
    if (this._now - warning.at > this.ttl) {
      this._byCar.delete(idx);
      return null;
    }
    return warning;
  }

  reset() {
    this._byCar.clear();
  }
}

// --------------------------------------------------------------------------
// fuel history (the consumption sparkline)
// --------------------------------------------------------------------------
/**
 * One sample of the engine's rolling per-lap consumption per completed lap.
 *
 * Deliberately the engine's own number rather than a client-side subtraction of
 * fuel levels: there is already one place in this system that decides what a lap
 * of fuel costs, and a second one that disagreed with it by a tenth would be
 * worse than no sparkline at all.
 */
export class FuelHistory {
  constructor(limit = 40) {
    this.limit = limit;
    this.samples = [];
    this._lastLap = null;
  }

  update(state) {
    const player = (state && state.player) || {};
    const fuel = (state && state.fuel) || {};
    if (!isNum(player.lap) || !isNum(fuel.per_lap)) return false;
    if (this._lastLap === player.lap) {
      // Refresh the current lap in place: per_lap moves within a lap as the
      // rolling mean refreshes, and the sparkline should show the latest.
      const last = this.samples[this.samples.length - 1];
      if (last && last.lap === player.lap) {
        last.perLap = fuel.per_lap;
        return false;
      }
    }
    this._lastLap = player.lap;
    this.samples.push({ lap: player.lap, perLap: fuel.per_lap });
    while (this.samples.length > this.limit) this.samples.shift();
    return true;
  }

  points() {
    return this.samples.slice();
  }

  reset() {
    this.samples = [];
    this._lastLap = null;
  }
}

// --------------------------------------------------------------------------
// event ticker
// --------------------------------------------------------------------------
const EVENT_TEXT = {
  flag_change: (p) => `flag ${p.from || '?'} → ${p.to || '?'}`,
  lap_completed: (p) => `lap ${p.lap ?? '?'} in ${lapTime(p.lap_time)}${p.green === false ? ' (not green)' : ''}`,
  personal_best: (p) => `personal best ${lapTime(p.lap_time)}`,
  session_fastest_lap: (p) => `session fastest ${lapTime(p.lap_time)}`,
  lockup: (p) => `lockup ${p.wheel || p.corner || ''} slip ${num(p.slip, 2)}`.trim(),
  wheelspin: (p) => `wheelspin ${p.wheel || p.corner || ''} slip ${num(p.slip, 2)}`.trim(),
  offtrack: (p) =>
    `off track (${p.surface || '?'}) at ` +
    `${isNum(p.lap_dist_pct) ? num(p.lap_dist_pct * 100, 0) : NA}%`,
  incident: (p) => `incident +${p.delta ?? 1} (${p.incidents ?? '?'} total)`,
  pit_entry: (p) => `pit entry, stint ${p.stint ?? '?'}, ${num(p.fuel, 1)} L`,
  pit_exit: (p) => `pit exit, stint ${p.stint ?? '?'}, ${num(p.fuel, 1)} L`,
  stint_start: (p) => `stint ${p.stint ?? '?'} begins`,
  pit_window_open: (p) => `pit window lap ${p.earliest_lap ?? '?'}–${p.latest_lap ?? '?'}`,
  pit_window_closing: (p) => `pit window closing, latest lap ${p.latest_lap ?? '?'}`,
  fuel_margin_low: (p) => `fuel margin ${signed(p.margin_laps, 2)} laps`,
  fuel_critical: (p) => `fuel critical: ${num(p.laps_remaining, 2)} laps left`,
  stint_lap_milestone: (p) => `${p.laps_on_tyres ?? '?'} laps on this set`,
  rival_pitted: (p) => `car #${p.car_idx ?? '?'} (P${p.position ?? '?'}) pitted`,
  blue_flag: (p) => `blue flag: car #${p.car_idx ?? '?'} at ${gap(p.gap)}`,
  recurring_issue: (p) => `${p.issue || 'issue'} ×${p.occurrences ?? '?'} at ${p.corner || '?'}`,
  tyre_out_of_band: (p) => `${p.measure || 'tyre'} ${p.corner || ''} = ${num(p.value, 1)}`.trim(),
  car_health_warning: (p) =>
    `${p.measure || 'health'}${p.value === undefined ? '' : ` ${num(p.value, 1)}`}` +
    (Array.isArray(p.warnings) && p.warnings.length ? ` [${p.warnings.join(', ')}]` : ''),
  traffic_close: (p) => `car #${p.car_idx ?? '?'} ${p.side || ''} at ${gap(p.gap)}`.replace('  ', ' '),
  weather_change: (p) => `${p.measure || 'weather'} ${p.from ?? '?'} → ${p.to ?? '?'}`,
  regulation_change: (p) => `${p.regulation || 'regulation'} from lap ${p.applies_from_lap ?? '?'}`,
};

/**
 * One compact line for the deterministic ticker. Unknown event types degrade to
 * their key plus the payload, rather than being dropped: an event the UI has
 * never heard of is still a fact about the race.
 */
export function eventLine(event) {
  if (!event) return null;
  const payload = event.payload || {};
  const render = EVENT_TEXT[event.event_type];
  let text;
  if (render) {
    try {
      text = render(payload);
    } catch (_) {
      text = null;
    }
  }
  if (!text) {
    const keys = Object.keys(payload).slice(0, 3);
    text = keys.length
      ? `${keys.map((k) => `${k}=${payload[k]}`).join(' ')}`
      : '';
  }
  return {
    key: event.key || event.event_type,
    type: event.event_type,
    severity: event.severity || 'info',
    lap: isNum(event.lap) ? event.lap : null,
    sessionTime: isNum(event.session_time) ? event.session_time : null,
    text,
    // Stage 5's provenance keys: a scripted safety car is behaviourally
    // identical to a real one, but the log is allowed to know the difference.
    injected: payload.injected === true,
    directorId: payload.director_id || null,
  };
}

export default {
  statusView,
  strategyView,
  towerView,
  tyreView,
  recommendationView,
  eventLine,
  marginBand,
  connectionView,
  llmView,
  GapTrendTracker,
  TowerWarnings,
  FuelHistory,
};
