/**
 * The post-race review as data: journal records and lap rows in, timeline lanes,
 * the agent audit and the radio log out.
 *
 * Pure, like shared/activity-model.js: no DOM, no fetch. Every figure the page
 * shows either comes from the backend as-is (lap times, findings, the summary) or
 * is a grouping of recorded records here; nothing is computed into existence.
 * Tested under node by review-model.test.js.
 */

import { AGENTS, roundFigures } from '../shared/activity-model.js';

/** Race events that already have a lane of their own (laps, flags) or are noise there. */
const OWN_LANE = new Set(['lap_completed', 'flag_change']);

/**
 * Records whose session_time is the race clock at the moment they were written.
 * Agent records instead carry the session time of the event that woke them.
 */
const CLOCK_KINDS = new Set(['state', 'race_event', 'radio_aired', 'lifecycle']);

/**
 * Map wall-clock ms onto session time using the journal's own clock records, or
 * null when there are fewer than two. A run's end, or the moment a call aired, is
 * only on the race clock through the records written around it.
 */
export function sessionClock(records) {
  const pairs = (records || [])
    .filter(
      (r) => CLOCK_KINDS.has(r.kind) && Number.isFinite(r.wall_ms) && Number.isFinite(r.session_time)
    )
    .map((r) => [r.wall_ms, r.session_time])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (pairs.length < 2) return null;
  return (wallMs) => {
    if (!Number.isFinite(wallMs)) return null;
    let lo = 0;
    let hi = pairs.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pairs[mid][0] < wallMs) lo = mid + 1;
      else hi = mid;
    }
    if (lo <= 0) return pairs[0][1];
    if (lo >= pairs.length) return pairs[pairs.length - 1][1];
    const [w0, s0] = pairs[lo - 1];
    const [w1, s1] = pairs[lo];
    return s0 + (s1 - s0) * (w1 > w0 ? (wallMs - w0) / (w1 - w0) : 0);
  };
}

/** message_id -> the session time it went on air, from radio_aired records. */
export function airedTimes(records) {
  const out = new Map();
  for (const rec of records || []) {
    if (rec.kind !== 'radio_aired') continue;
    const id = (rec.data || {}).message_id;
    if (id && Number.isFinite(rec.session_time) && !out.has(id)) out.set(id, rec.session_time);
  }
  return out;
}

/**
 * What the radio decided at air time: {kind: 'late', text: 'late +27s'} for a call
 * aired after its agent's limit, {kind: 'expired', text: 'not aired', reason} for
 * one dropped because it had stopped being true, else null.
 */
export function airBadge(message) {
  if (!message || typeof message !== 'object') return null;
  if (message.expired) {
    return { kind: 'expired', text: 'not aired', reason: message.expired_reason || 'no longer valid' };
  }
  if (message.late) {
    const by = message.late_by_s;
    return { kind: 'late', text: Number.isFinite(by) ? `late +${Math.round(by)}s` : 'late' };
  }
  return null;
}

/** When a call reached the channel: aired, or (for a dropped one) dropped. */
function radioWhen(data, aired, t) {
  if (data.expired && Number.isFinite(data.air_session_time)) return data.air_session_time;
  return aired.has(data.message_id) ? aired.get(data.message_id) : t;
}

/** A lap is "clean" when it is valid and neither an in- nor an out-lap. */
export function isClean(lap) {
  return !!(lap && lap.is_valid && !lap.is_in_lap && !lap.is_out_lap && lap.lap_time > 0);
}

/**
 * Default comparison: the best clean lap as the reference, and the clean lap
 * closest to the clean median as the lap under review -- a typical lap against
 * the best one is where the time is.
 */
export function defaultLaps(laps) {
  const clean = (laps || []).filter(isClean);
  if (!clean.length) return { main: null, ref: null };
  const best = clean.reduce((a, b) => (b.lap_time < a.lap_time ? b : a));
  const others = clean.filter((l) => l.lap !== best.lap);
  if (!others.length) return { main: best.lap, ref: best.lap };
  const times = clean.map((l) => l.lap_time).sort((a, b) => a - b);
  const median = times[Math.floor(times.length / 2)];
  const typical = others.reduce((a, b) =>
    Math.abs(b.lap_time - median) < Math.abs(a.lap_time - median) ? b : a
  );
  return { main: typical.lap, ref: best.lap };
}

/** The lap being driven at session time t (by lap start times), or null. */
export function lapAt(laps, t) {
  let found = null;
  for (const lap of laps || []) {
    if (Number.isFinite(lap.start_session_time) && lap.start_session_time <= t) {
      if (!found || lap.start_session_time > found.start_session_time) found = lap;
    }
  }
  return found;
}

/** Session-time bounds of a run, from its summary or else its laps. */
export function bounds(run, laps) {
  let t0 = run && run.session_time_start;
  let t1 = run && run.session_time_end;
  for (const lap of laps || []) {
    if (!Number.isFinite(lap.start_session_time)) continue;
    const end = lap.start_session_time + (lap.lap_time || 0);
    t0 = Number.isFinite(t0) ? Math.min(t0, lap.start_session_time) : lap.start_session_time;
    t1 = Number.isFinite(t1) ? Math.max(t1, end) : end;
  }
  if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) return null;
  return [t0, t1];
}

/**
 * Every timeline lane as items: { laps, flags, events, <agent>..., radio }.
 * Agent items are spans from the waking event to the run's end on the race
 * clock, and radio items sit where the call aired, so a slow model call is
 * visibly long.
 */
export function laneItems(records, laps) {
  const clockAt = sessionClock(records);
  const aired = airedTimes(records);
  const lanes = { laps: [], flags: [], events: [], radio: [] };
  for (const agent of AGENTS) lanes[agent] = [];

  for (const lap of laps || []) {
    if (!Number.isFinite(lap.start_session_time) || !(lap.lap_time > 0)) continue;
    const tags = [lap.is_in_lap && 'in', lap.is_out_lap && 'out', !lap.is_valid && 'invalid']
      .filter(Boolean)
      .join(', ');
    lanes.laps.push({
      t0: lap.start_session_time,
      t1: lap.start_session_time + lap.lap_time,
      label: `lap ${lap.lap}${tags ? ` (${tags})` : ''}`,
      cls: isClean(lap) ? 'lap-clean' : 'lap-other',
      record: { kind: 'lap', data: lap },
    });
  }

  let flag = null;
  const runs = new Map();
  const sorted = [...(records || [])].sort((a, b) => (a.seq || 0) - (b.seq || 0));
  for (const rec of sorted) {
    const t = rec.session_time;
    if (!Number.isFinite(t)) continue;
    const data = rec.data || {};
    if (rec.kind === 'race_event') {
      if (data.event_type === 'flag_change') {
        if (flag) flag.t1 = t;
        const phase = (data.payload && data.payload.to) || 'flag';
        flag = { t0: t, t1: null, label: `${phase} flag`, cls: `flag-${phase}`, record: rec };
        lanes.flags.push(flag);
      } else if (!OWN_LANE.has(data.event_type)) {
        lanes.events.push({
          t0: t,
          label: `${data.event_type}${data.lap != null ? ` · lap ${data.lap}` : ''}`,
          cls: `sev-${data.severity || 'info'}`,
          record: rec,
        });
      }
    } else if (rec.kind === 'run_start' && lanes[rec.agent]) {
      const item = {
        t0: t,
        t1: t,
        label: `${rec.agent} woken by ${rec.event_key || '?'}`,
        cls: `agent-${rec.agent}`,
        record: rec,
      };
      runs.set(rec.run_id, item);
      lanes[rec.agent].push(item);
    } else if (rec.kind === 'run_end' && runs.has(rec.run_id)) {
      const item = runs.get(rec.run_id);
      const endT = clockAt ? clockAt(rec.wall_ms) : null;
      item.t1 = Math.max(item.t0, Number.isFinite(endT) ? endT : t);
      item.label += ` → ${data.outcome || '?'}`;
      if (data.outcome && data.outcome !== 'radio') item.cls += ' run-quiet';
    } else if (rec.kind === 'radio') {
      const badge = airBadge(data);
      let note = '';
      if (badge && badge.kind === 'expired') note = ` (not aired: ${badge.reason})`;
      else if (badge) note = ` (${badge.text})`;
      lanes.radio.push({
        t0: radioWhen(data, aired, t),
        label: `${data.agent}${note}: ${data.spoken_text || ''}`,
        cls: `agent-${data.agent}${data.refused ? ' refused' : ''}${badge ? ` ${badge.kind}` : ''}`,
        late: !!data.late,
        expired: !!data.expired,
        record: rec,
      });
    }
  }
  const end = sorted.length ? sorted[sorted.length - 1].session_time : null;
  if (flag && flag.t1 == null) flag.t1 = Number.isFinite(end) ? end : flag.t0;
  // A run that never ended (the session stopped mid-call) stays a point.
  for (const item of runs.values()) if (item.t1 === item.t0) delete item.t1;
  return lanes;
}

/**
 * One row per agent run for the audit table: what woke it, what it asked the
 * model and the tools, what the validator said, and what went on air.
 */
export function auditRows(records) {
  const rows = new Map();
  const byMessage = new Map();
  const sorted = [...(records || [])].sort((a, b) => (a.seq || 0) - (b.seq || 0));
  for (const rec of sorted) {
    const data = rec.data || {};
    if (rec.kind === 'radio') {
      byMessage.set(data.message_id, data);
      continue;
    }
    if (!rec.run_id) continue;
    let row = rows.get(rec.run_id);
    if (!row) {
      row = {
        runId: rec.run_id,
        agent: rec.agent,
        eventKey: rec.event_key,
        t: rec.session_time,
        model: null,
        llmCalls: 0,
        llmErrors: 0,
        llmMs: 0,
        tools: [],
        toolErrors: 0,
        validations: 0,
        unsupported: [],
        reasons: [],
        repaired: false,
        outcome: null,
        latencyMs: null,
        messageId: null,
        contextFrom: [],
      };
      rows.set(rec.run_id, row);
    }
    switch (rec.kind) {
      case 'run_start':
        row.model = data.model || row.model;
        row.contextFrom = (data.context_radio || []).map((m) => m.agent).filter(Boolean);
        break;
      case 'llm_call_end':
        row.llmCalls += 1;
        if (!data.ok) row.llmErrors += 1;
        row.llmMs += Number(data.latency_ms) || 0;
        break;
      case 'tool_call':
        row.tools.push(data.tool);
        if (!data.ok) row.toolErrors += 1;
        break;
      case 'validate':
        row.validations += 1;
        if (!data.ok) {
          for (const u of data.unsupported || []) {
            const shown = roundFigures(u);
            if (!row.unsupported.includes(shown)) row.unsupported.push(shown);
          }
          for (const r of data.unsupported_detail || []) {
            const shown = roundFigures(r);
            if (!row.reasons.includes(shown)) row.reasons.push(shown);
          }
        }
        if (data.repaired) row.repaired = true;
        break;
      case 'run_end':
        row.outcome = data.outcome || null;
        row.latencyMs = Number.isFinite(data.latency_ms) ? data.latency_ms : null;
        row.messageId = data.message_id || null;
        break;
      default:
        break;
    }
  }
  return [...rows.values()].map((row) => {
    const message = row.messageId ? byMessage.get(row.messageId) : null;
    return {
      ...row,
      spoken: message ? message.spoken_text : null,
      grounded: message ? message.grounded !== false : null,
    };
  });
}

/** Filter + sort the audit rows. sort: 'time' | 'latency' | 'calls'. */
export function filterAudit(rows, { agent = '', outcome = '', sort = 'time' } = {}) {
  const out = rows.filter(
    (r) => (!agent || r.agent === agent) && (!outcome || (r.outcome || 'unfinished') === outcome)
  );
  const key = {
    time: (r) => r.t ?? 0,
    latency: (r) => -(r.latencyMs ?? -1),
    calls: (r) => -(r.llmCalls + r.tools.length),
  }[sort] || ((r) => r.t ?? 0);
  return out.sort((a, b) => key(a) - key(b));
}

/** Radio calls that had aired at or before t, newest first. */
export function radioUpTo(records, t, limit = 40) {
  const aired = airedTimes(records);
  const when = (rec) => radioWhen(rec.data || {}, aired, rec.session_time);
  const out = [];
  for (const rec of records || []) {
    if (rec.kind !== 'radio') continue;
    if (Number.isFinite(t) && Number.isFinite(when(rec)) && when(rec) > t) continue;
    out.push(rec);
  }
  out.sort((a, b) => (when(b) || 0) - (when(a) || 0));
  return out.slice(0, limit);
}

/** Race events at or before t, newest first. */
export function eventsUpTo(records, t, limit = 12) {
  const out = (records || []).filter(
    (r) =>
      r.kind === 'race_event' &&
      (!Number.isFinite(t) || !Number.isFinite(r.session_time) || r.session_time <= t)
  );
  out.sort((a, b) => (b.session_time ?? 0) - (a.session_time ?? 0));
  return out.slice(0, limit);
}

/**
 * Map lap-distance-fraction samples from /compare onto metres, so the traces
 * share an axis with the findings (which are in metres).
 */
export function toMetres(xValues, lapLength) {
  if (!(lapLength > 0)) return xValues || [];
  return (xValues || []).map((x) => x * lapLength);
}

/** Corner region for highlighting on the traces: [entry, exit] in metres. */
export function cornerSpan(corner) {
  if (!corner) return null;
  const a = Number.isFinite(corner.entry_m) ? corner.entry_m : corner.distance - 60;
  const b = Number.isFinite(corner.exit_m) ? corner.exit_m : corner.distance + 60;
  return [Math.min(a, b), Math.max(a, b)];
}

/** Events the detectors pinned to a place on track, for the map: offs, incidents, lock-ups. */
export function trackEvents(records) {
  const out = [];
  for (const rec of records || []) {
    if (rec.kind !== 'race_event') continue;
    const data = rec.data || {};
    const pct = data.payload && data.payload.lap_dist_pct;
    if (!Number.isFinite(pct)) continue;
    out.push({
      pct,
      type: data.event_type,
      severity: data.severity || 'info',
      lap: data.lap,
      t: rec.session_time,
    });
  }
  return out;
}

/** The nearest map point to a lap-distance fraction, for placing a marker. */
export function pointAtPct(points, pct) {
  let best = null;
  let bestD = Infinity;
  for (const p of points || []) {
    if (!Number.isFinite(p.lap_dist_pct)) continue;
    let d = Math.abs(p.lap_dist_pct - pct);
    d = Math.min(d, 1 - d); // the lap wraps at the line
    if (d < bestD) {
      bestD = d;
      best = p;
    }
  }
  return best;
}
