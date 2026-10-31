/**
 * The post-race review: pick a recorded session, replay what the pitwall saw,
 * did and said, and dig into the laps.
 *
 *   /sessions, /laps, /summary          ─► session bar + race summary
 *   /pitwall-runs, /pitwall-log         ─► timeline, flow graph, runs, radio, audit
 *   /pitwall-log/state?t=               ─► the car at the cursor
 *   /compare, /coaching/delta, /trackmap ─► lap traces + map
 *   /coaching/lap-findings              ─► corners, sectors, driver inputs
 *
 * The replay reads the *recorded* journal; nothing is re-run and no model is
 * called. The flow graph and the runs panel are the live view's own components
 * fed by shared/activity-model.js's reduce() up to the cursor, so the review and
 * the live view cannot disagree about what happened.
 *
 * Grouping lives in review-model.js (tested under node); this file is wiring.
 * State lives in the URL (session, run, main, ref, t), never in storage.
 */

import { AGENTS, edgesFor, meanLatency, reduce } from '../shared/activity-model.js';
import { FlowGraph } from '../shared/flow-graph.js';
import { Timeline, clock as tlClock } from '../shared/timeline.js';
import { RunsPanel } from '../live/runs-panel.js';
import { NA, humanise, isNum, lapTime, num, signed } from '../pitwall/format.js';
import { clear, el, setText } from '../pitwall/dom.js';
import { TraceStack, TrackMap } from './charts.js';
import {
  airBadge,
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

const API = '/api/v1';
const $ = (id) => document.getElementById(id);
const q = (role) => document.querySelector(`[data-role="${role}"]`);

/** Channels on the lap traces: [channel, label, unit, scale, digits]. */
const TRACE_CHANNELS = [
  ['Speed', 'speed', 'km/h', 3.6, 0],
  ['Throttle', 'throttle', '%', 100, 0],
  ['Brake', 'brake', '%', 100, 0],
  ['SteeringWheelAngle', 'steering', 'deg', 180 / Math.PI, 0],
  ['Gear', 'gear', '', 1, 0],
];

const params = new URLSearchParams(window.location.search);
const view = {
  sessions: [],
  sessionId: params.get('session') || '',
  runs: [],
  runId: params.get('run') || '',
  laps: [],
  records: [],
  audit: [],
  range: null,
  t: params.has('t') ? Number(params.get('t')) : null,
  playing: false,
  hasAgents: false,
  main: params.has('main') ? Number(params.get('main')) : params.has('lap') ? Number(params.get('lap')) : null,
  ref: params.has('ref') ? Number(params.get('ref')) : null,
  findings: null,
  mapPoints: [],
  lapLength: null,
};

const timeline = new Timeline(q('timeline'), {
  lanes: [
    { id: 'laps', label: 'laps' },
    { id: 'flags', label: 'flags' },
    { id: 'events', label: 'race events' },
    ...AGENTS.map((a) => ({ id: a, label: humanise(a) })),
    { id: 'radio', label: 'radio' },
  ],
});
const flow = new FlowGraph(q('flow'));
const runsPanel = new RunsPanel($('panel-runs'), { labels: humanise });
const traces = new TraceStack(q('traces'), { onHover: (m) => hoverAt(m) });
const trackMap = new TrackMap(q('trackmap'));

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------
async function getJson(path) {
  const response = await fetch(`${API}${path}`);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || `${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function notice(text) {
  setText($('app-notice'), text || '');
  $('app-notice').hidden = !text;
}

let urlTimer = null;
function saveUrl() {
  clearTimeout(urlTimer);
  urlTimer = setTimeout(() => {
    const next = new URLSearchParams();
    if (view.sessionId) next.set('session', view.sessionId);
    if (view.runId) next.set('run', view.runId);
    if (isNum(view.main)) next.set('main', String(view.main));
    if (isNum(view.ref)) next.set('ref', String(view.ref));
    if (isNum(view.t)) next.set('t', view.t.toFixed(1));
    window.history.replaceState({}, '', `${window.location.pathname}?${next}`);
  }, 300);
}

function row(cells, className) {
  const tr = el('tr', className || '');
  for (const cell of cells) {
    if (cell instanceof Node) {
      const td = el('td');
      td.append(cell);
      tr.append(td);
    } else if (cell && typeof cell === 'object') {
      tr.append(el('td', cell.cls || '', cell.text));
    } else {
      tr.append(el('td', '', cell));
    }
  }
  return tr;
}

function head(table, labels) {
  const tr = el('tr');
  for (const label of labels) {
    const [text, cls] = Array.isArray(label) ? label : [label, ''];
    tr.append(el('th', cls, text));
  }
  table.append(tr);
}

// --------------------------------------------------------------------------
// sessions and runs
// --------------------------------------------------------------------------
async function loadSessions(select) {
  try {
    view.sessions = (await getJson('/sessions?limit=200')).sessions || [];
  } catch (_) {
    view.sessions = [];
  }
  const box = $('session-select');
  clear(box);
  if (!view.sessions.length) {
    box.append(el('option', '', 'no sessions yet: import an .ibt or record one live'));
    setText($('session-status'), '');
    renderEmpty();
    return;
  }
  for (const s of view.sessions) {
    const when = (s.started_at || '').slice(0, 16).replace('T', ' ');
    const label = [s.label, s.track_name || s.track_id, s.kind, when].filter(Boolean).join(' · ');
    const option = el('option', '', label || s.session_id);
    option.value = s.session_id;
    box.append(option);
  }
  const wanted = select || view.sessionId;
  const pick = view.sessions.some((s) => s.session_id === wanted) ? wanted : view.sessions[0].session_id;
  box.value = pick;
  await selectSession(pick, { keepUrl: pick === view.sessionId });
}

function renderEmpty() {
  clear(q('summary-grid'));
  q('summary-grid').append(
    el('div', 'muted', 'Nothing recorded yet. Import an iRacing .ibt file above, or drive with the live poller running.')
  );
}

async function selectSession(sessionId, { keepUrl = false } = {}) {
  view.sessionId = sessionId;
  if (!keepUrl) {
    view.runId = '';
    view.main = null;
    view.ref = null;
    view.t = null;
  }
  setText($('session-status'), 'loading…');
  try {
    view.laps = (await getJson(`/sessions/${sessionId}/laps`)).laps || [];
  } catch (_) {
    view.laps = [];
  }
  await loadRuns();
  setupLapPickers();
  await Promise.all([loadRun(), loadLapAnalysis()]);
  saveUrl();
}

async function loadRuns() {
  let body = { runs: [], default_run: null };
  try {
    body = await getJson(`/sessions/${view.sessionId}/pitwall-runs`);
  } catch (_) {
    /* no journal yet */
  }
  view.runs = body.runs || [];
  if (!view.runs.some((r) => r.run_id === view.runId)) view.runId = body.default_run || '';
  const box = $('run-select');
  clear(box);
  if (!view.runs.length) {
    box.append(el('option', '', 'none recorded'));
    box.disabled = true;
  } else {
    box.disabled = false;
    for (const run of [...view.runs].reverse()) {
      const agents = run.has_agents ? `${run.counts.run_start || 0} agent runs` : 'no agents';
      const option = el('option', '', `${run.source} · ${run.run_id.slice(0, 15)} · ${agents}`);
      option.value = run.run_id;
      box.append(option);
    }
    box.value = view.runId;
  }
  const recording = body.recording && body.recording.session_id === view.sessionId;
  setText(
    $('session-status'),
    recording
      ? 'being recorded right now; reload when it ends'
      : view.runs.length
        ? `${view.laps.length} laps · ${view.runs.length} pitwall run${view.runs.length === 1 ? '' : 's'}`
        : `${view.laps.length} laps · no pitwall run recorded yet`
  );
}

async function loadRun() {
  const run = view.runs.find((r) => r.run_id === view.runId) || null;
  view.records = [];
  if (run) {
    try {
      const body = await getJson(
        `/sessions/${view.sessionId}/pitwall-log?run=${encodeURIComponent(run.run_id)}&limit=200000`
      );
      view.records = body.records || [];
    } catch (error) {
      notice(`could not read the pitwall log: ${error.message}`);
    }
  }
  view.hasAgents = !!(run && run.has_agents);
  view.audit = auditRows(view.records);
  view.range = bounds(run, view.laps);

  const lanes = laneItems(view.records, view.laps);
  for (const [id, items] of Object.entries(lanes)) timeline.setItems(id, items);
  if (view.range) timeline.setRange(view.range[0], view.range[1]);
  setText(
    q('replay-sub'),
    !run
      ? 'no pitwall run recorded for this session: use "Record a run from the telemetry"'
      : view.hasAgents
        ? `${run.source} run · ${run.counts.run_start || 0} agent runs · ${run.counts.radio || 0} radio calls`
        : `${run.source} run · race engine only (no agents were running, so no radio)`
  );
  $('analyse').hidden = false;
  renderAuditFilters();
  renderAudit();
  await loadSummary();
  const start = view.range ? view.range[0] : 0;
  seek(isNum(view.t) && view.range && view.t >= view.range[0] && view.t <= view.range[1] ? view.t : start);
  drawMarkers();
}

// --------------------------------------------------------------------------
// summary
// --------------------------------------------------------------------------
async function loadSummary() {
  const grid = q('summary-grid');
  clear(grid);
  let s;
  try {
    const run = view.runId ? `?run=${encodeURIComponent(view.runId)}` : '';
    s = await getJson(`/sessions/${view.sessionId}/summary${run}`);
  } catch (error) {
    grid.append(el('div', 'muted', `no summary: ${error.message}`));
    return;
  }
  const session = view.sessions.find((x) => x.session_id === view.sessionId) || {};
  setText(q('summary-sub'), [session.track_name, session.kind].filter(Boolean).join(' · '));
  const cell = (k, v, sub) => {
    const c = el('div', 'summary-cell');
    c.append(el('span', 'k', k), el('span', 'v', v));
    if (sub) c.append(el('span', 'sub', sub));
    grid.append(c);
  };
  const pos = (p) => (isNum(p) ? `P${p}` : NA);
  cell('result', `${pos(s.start_position)} → ${pos(s.finish_position)}`, 'start → finish');
  cell('best lap', lapTime(s.best_time), isNum(s.best_lap) ? `lap ${s.best_lap}` : '');
  cell('median clean lap', lapTime(s.median_time), isNum(s.stdev_time) ? `σ ${num(s.stdev_time, 2, ' s')}` : '');
  cell('laps', `${s.laps}`, `${s.clean_laps} clean`);
  cell('pit stops', `${s.pit_stops}`, `${(s.stints || []).length} stints`);
  cell('fuel used', num(s.fuel_used_l, 1, ' L'), isNum(s.fuel_per_lap_l) ? `${num(s.fuel_per_lap_l, 2)} L/lap` : '');
  cell('incidents', isNum(s.incidents) ? `${s.incidents}x` : NA, `${(s.events || {}).offtrack || 0} off-tracks`);
  cell('race events', `${Object.values(s.events || {}).reduce((a, b) => a + b, 0)}`, 'from the detectors');
  cell('radio calls', `${s.radio_calls}`, s.radio_refused ? `${s.radio_refused} refused` : '');
  cell(
    'agent runs',
    `${s.agent_runs}`,
    s.model_calls ? `${s.model_calls} model calls · ${num((s.model_latency_ms || 0) / 1000, 1, ' s')} avg` : ''
  );

  const stints = q('stints');
  clear(stints);
  if ((s.stints || []).length > 1) {
    head(stints, ['stint', 'laps', ['clean', 'num'], ['best', 'num'], ['median', 'num'], ['σ', 'num']]);
    for (const st of s.stints) {
      stints.append(
        row([
          `${st.stint}`,
          `${st.from_lap}–${st.to_lap}`,
          { text: `${st.clean_laps}`, cls: 'num' },
          { text: lapTime(st.best_time), cls: 'num' },
          { text: lapTime(st.median_time), cls: 'num' },
          { text: num(st.stdev_time, 2), cls: 'num' },
        ])
      );
    }
  }
  const notes = q('summary-notes');
  clear(notes);
  for (const n of s.notes || []) notes.append(el('li', '', n));
}

// --------------------------------------------------------------------------
// replay
// --------------------------------------------------------------------------
let stateTimer = null;
let stateSeq = 0;

function seek(t, { fromPlay = false } = {}) {
  if (!isNum(t)) return;
  const prev = view.t;
  view.t = t;
  timeline.setCursor(t);
  const lap = lapAt(view.laps, t);
  setText(q('clock'), `${tlClock(t)}${lap ? ` · lap ${lap.lap}` : ''}`);

  const model = reduce(view.records, t);
  runsPanel.render(model);
  renderFlow(model);
  if (fromPlay && isNum(prev) && t > prev) pulseBetween(prev, t);
  renderLogs(t);
  clearTimeout(stateTimer);
  stateTimer = setTimeout(() => loadState(t), fromPlay ? 0 : 120);
  saveUrl();
}

function pulseBetween(t0, t1) {
  let n = 0;
  for (const rec of view.records) {
    const t = rec.session_time;
    if (!isNum(t) || t <= t0 || t > t1) continue;
    for (const edge of edgesFor(rec)) flow.pulse(edge);
    if (++n > 30) break; // a fast replay would flood the graph
  }
}

function renderFlow(model) {
  const on = view.hasAgents;
  const events = view.records.filter((r) => r.kind === 'race_event' && r.session_time <= view.t).length;
  flow.setNode('telemetry', { sub: 'recorded telemetry' });
  flow.setNode('engine', { sub: 'race-state engine' });
  flow.setNode('detectors', { sub: `${events} events so far` });
  flow.setNode('bus', { sub: on ? `${model.dispatch.events} dispatched` : `${events} published` });
  flow.setNode('orchestrator', {
    sub: on ? `${model.dispatch.routed} routed · ${model.dispatch.cooldown} cooldown` : 'agents were off',
    status: on ? 'idle' : 'off',
  });
  for (const agent of AGENTS) {
    const a = model.agents[agent];
    flow.setNode(agent, {
      sub: on
        ? a.status === 'idle'
          ? `${a.runs} runs${a.refused ? ` · ${a.refused} refused` : ''}`
          : `${a.status}${a.detail ? ` · ${a.detail}` : ''}`
        : 'not running',
      status: on ? a.status : 'off',
    });
  }
  const latency = meanLatency(model);
  flow.setNode('llm', {
    sub: `${model.llm.calls} calls${latency !== null ? ` · ${(latency / 1000).toFixed(1)} s avg` : ''}`,
    status: on ? (model.llm.inflight ? 'thinking' : 'idle') : 'off',
  });
  flow.setNode('tools', { sub: `${model.tools.calls} calls`, status: on ? 'idle' : 'off' });
  flow.setNode('validator', {
    sub: `${model.validator.checks} checks · ${model.validator.failed} failed`,
    status: on ? 'idle' : 'off',
  });
  flow.setNode('radio', { sub: `${model.radio.aired} aired`, status: on ? 'idle' : 'off' });
  const lap = lapAt(view.laps, view.t);
  flow.setNode('driver', { sub: lap ? `lap ${lap.lap}` : '' });
}

function renderLogs(t) {
  const radio = q('radio-log');
  clear(radio);
  const calls = radioUpTo(view.records, t);
  if (!calls.length) {
    radio.append(el('li', 'muted', view.hasAgents ? 'Nothing said yet.' : 'No agents ran in this run.'));
  }
  for (const rec of calls) {
    const d = rec.data || {};
    const li = el('li', `agent-${d.agent}`);
    li.append(el('span', 't', tlClock(rec.session_time)));
    li.append(el('strong', '', `${humanise(d.agent)}: `));
    // A call dropped at air time is listed, struck, with why; it was never said.
    const badge = airBadge(d);
    if (badge) li.append(el('em', '', `[${badge.text}] `));
    const text = el('span', d.refused ? 'refused' : '', d.spoken_text || d.detail_text || '');
    if (badge && badge.kind === 'expired') text.style.textDecoration = 'line-through';
    li.append(text);
    li.title = badge && badge.kind === 'expired' ? `Not aired: ${badge.reason}` : d.detail_text || '';
    li.onclick = () => seek(rec.session_time);
    radio.append(li);
  }
  const events = q('event-log');
  clear(events);
  for (const rec of eventsUpTo(view.records, t)) {
    const d = rec.data || {};
    const li = el('li', `sev-${d.severity || 'info'}`);
    li.append(el('span', 't', tlClock(rec.session_time)));
    li.append(el('span', '', `${humanise(d.event_type)}${d.lap != null ? ` · lap ${d.lap}` : ''}`));
    li.onclick = () => seek(rec.session_time);
    events.append(li);
  }
}

async function loadState(t) {
  const grid = q('state-grid');
  if (!view.runId) {
    clear(grid);
    grid.append(el('div', 'muted', 'No recorded race state.'));
    return;
  }
  const mine = ++stateSeq;
  let rec;
  try {
    rec = (
      await getJson(
        `/sessions/${view.sessionId}/pitwall-log/state?run=${encodeURIComponent(view.runId)}&t=${t}`
      )
    ).record;
  } catch (_) {
    return;
  }
  if (mine !== stateSeq) return; // a newer seek won
  const s = rec.data || {};
  const p = s.player || {};
  const f = s.fuel || {};
  const ty = s.tyres || {};
  const flags = s.flags || {};
  clear(grid);
  const cell = (k, v) => {
    const c = el('div', 'state-cell');
    c.append(el('span', 'k', k), el('span', 'v', v));
    grid.append(c);
  };
  cell('lap', isNum(p.lap) ? `${p.lap}` : NA);
  cell('position', isNum(p.position) && p.position > 0 ? `P${p.position}` : NA);
  cell('flag', flags.phase || NA);
  cell('speed', num(isNum(p.speed) ? p.speed * 3.6 : null, 0, ' km/h'));
  cell('gear', isNum(p.gear) ? `${p.gear}` : NA);
  cell('last lap', lapTime(p.last_lap_time));
  cell('fuel', num(f.level, 1, ' L'));
  cell('per lap', num(f.per_lap, 2, ' L'));
  cell('laps of fuel', num(f.laps_remaining, 1));
  const temps = ty.temps || {};
  cell(
    'tyre temps',
    ['LF', 'RF', 'LR', 'RR'].map((k) => (isNum(temps[k]) ? temps[k].toFixed(0) : '–')).join(' ') || NA
  );
  cell('stint', isNum(p.stint) ? `${p.stint} · ${p.laps_on_tyres ?? 0} laps` : NA);
  cell('pit road', p.on_pit_road == null ? NA : p.on_pit_road ? 'yes' : 'no');
  setText(q('state-sub'), `snapshot at ${tlClock(rec.session_time)}`);
}

// play
let lastFrame = null;
let lastSeek = 0;
function playLoop(now) {
  if (!view.playing) return;
  if (lastFrame !== null && view.range) {
    const speed = Number($('play-speed').value) || 1;
    const t = Math.min(view.range[1], view.t + ((now - lastFrame) / 1000) * speed);
    if (now - lastSeek > 100 || t >= view.range[1]) {
      seek(t, { fromPlay: true });
      lastSeek = now;
    } else {
      view.t = t;
    }
    if (t >= view.range[1]) setPlaying(false);
  }
  lastFrame = now;
  requestAnimationFrame(playLoop);
}

function setPlaying(on) {
  view.playing = on && !!view.range;
  setText($('play'), view.playing ? '❚❚' : '▶');
  if (view.playing) {
    if (view.t >= view.range[1]) view.t = view.range[0];
    lastFrame = null;
    requestAnimationFrame(playLoop);
  }
}

timeline.onSeek = (t) => {
  setPlaying(false);
  seek(t);
};
timeline.onSelect = (item) => {
  setPlaying(false);
  seek(item.t0);
  const rec = item.record || {};
  if (rec.kind === 'lap' && rec.data && isNum(rec.data.lap)) {
    view.main = rec.data.lap;
    setupLapPickers();
    loadLapAnalysis();
  }
};

// --------------------------------------------------------------------------
// laps, traces, map, findings
// --------------------------------------------------------------------------
function setupLapPickers() {
  const laps = view.laps.filter((l) => l.lap_time > 0);
  const defaults = defaultLaps(view.laps);
  if (!laps.some((l) => l.lap === view.main)) view.main = defaults.main;
  if (!laps.some((l) => l.lap === view.ref)) view.ref = defaults.ref;
  for (const [id, key] of [['main-lap', 'main'], ['ref-lap', 'ref']]) {
    const box = $(id);
    clear(box);
    for (const lap of laps) {
      const tags = [lap.is_in_lap && 'in', lap.is_out_lap && 'out', !lap.is_valid && 'invalid'].filter(Boolean);
      const option = el('option', '', `${lap.lap} · ${lapTime(lap.lap_time)}${tags.length ? ` (${tags.join(', ')})` : ''}`);
      option.value = String(lap.lap);
      box.append(option);
    }
    if (isNum(view[key])) box.value = String(view[key]);
  }
  renderLapTable();
}

function renderLapTable() {
  const table = q('lap-table');
  clear(table);
  const best = Math.min(...view.laps.filter(isClean).map((l) => l.lap_time));
  head(table, ['lap', ['time', 'num'], ['Δ best', 'num'], '']);
  for (const lap of view.laps) {
    if (!(lap.lap_time > 0)) continue;
    const tags = [lap.is_in_lap && 'in', lap.is_out_lap && 'out', !lap.is_valid && 'invalid'].filter(Boolean);
    const cls = ['clickable', lap.lap === view.main && 'selected', lap.lap === view.ref && 'ref']
      .filter(Boolean)
      .join(' ');
    const d = isFinite(best) && isClean(lap) ? lap.lap_time - best : null;
    const tr = row(
      [
        `${lap.lap}`,
        { text: lapTime(lap.lap_time), cls: 'num' },
        { text: d === 0 ? 'best' : isNum(d) ? signed(d, 3) : '', cls: `num ${d ? 'loss' : 'gain'}` },
        { text: tags.join(', '), cls: 'muted' },
      ],
      cls
    );
    tr.title = 'click: review this lap · shift-click: use as the reference';
    tr.onclick = (event) => {
      if (event.shiftKey) view.ref = lap.lap;
      else view.main = lap.lap;
      setupLapPickers();
      loadLapAnalysis();
    };
    table.append(tr);
  }
}

async function loadLapAnalysis() {
  const { sessionId, main, ref } = view;
  setText(q('main-label'), isNum(main) ? `${main}` : NA);
  setText(q('ref-label'), isNum(ref) ? `${ref}` : NA);
  if (!sessionId || !isNum(main) || !isNum(ref)) {
    setText(q('laps-sub'), 'no complete laps to analyse');
    return;
  }
  const mainLap = view.laps.find((l) => l.lap === main);
  const refLap = view.laps.find((l) => l.lap === ref);
  setText(
    q('laps-sub'),
    `lap ${main} ${lapTime(mainLap && mainLap.lap_time)} vs lap ${ref} ${lapTime(refLap && refLap.lap_time)}`
  );
  saveUrl();
  const base = `session_id=${encodeURIComponent(sessionId)}&main_lap=${main}&ref_lap=${ref}`;
  const [findings, delta, map, ...channels] = await Promise.all([
    getJson(`/coaching/lap-findings?${base}`).catch((e) => ({ error: e.message })),
    getJson(`/coaching/delta?${base}`).catch(() => null),
    getJson(`/sessions/${sessionId}/trackmap?lap=${ref}&max_points=1500`).catch(() => null),
    ...TRACE_CHANNELS.map(([name]) =>
      getJson(`/sessions/${sessionId}/compare?name=${name}&laps=${main},${ref}&grid=1000`).catch(() => null)
    ),
  ]);
  if (main !== view.main || ref !== view.ref) return; // the user moved on

  view.findings = findings && !findings.error ? findings : null;
  view.lapLength = (view.findings && view.findings.lap_length_m) || (delta && delta.lap_length_m) || null;
  const series = [];
  let x = null;
  const missing = [];
  TRACE_CHANNELS.forEach(([name, label, unit, scale, digits], i) => {
    const c = channels[i];
    if (!c || !c.laps || !c.laps[String(main)]) {
      missing.push(name);
      return;
    }
    x = x || toMetres(c.x_values, view.lapLength);
    const conv = (arr) => (arr || []).map((v) => (v == null ? null : v * scale));
    series.push({ id: name, label, unit, digits, main: conv(c.laps[String(main)]), ref: conv(c.laps[String(ref)]) });
  });
  if (x && view.lapLength) {
    traces.setData({
      x,
      series,
      lapLength: view.lapLength,
      delta: delta ? { x: delta.distance_m, y: delta.delta_s } : null,
    });
  }
  traces.highlight(null);
  view.mapPoints = (map && map.points) || [];
  trackMap.setPath(view.mapPoints);
  drawMarkers();
  renderFindings(findings, missing);
}

function drawMarkers() {
  if (!view.mapPoints.length) return;
  trackMap.setMarkers(
    trackEvents(view.records).map((e) => ({ point: pointAtPct(view.mapPoints, e.pct), severity: e.severity }))
  );
}

function hoverAt(metres) {
  if (!isNum(metres) || !view.lapLength) {
    trackMap.setDot(null);
    return;
  }
  trackMap.setDot(pointAtPct(view.mapPoints, metres / view.lapLength));
}

function renderFindings(f, missing) {
  const chief = q('chief');
  const corners = q('corners');
  const sectors = q('sectors');
  const kpis = q('kpis');
  const notes = q('findings-notes');
  [chief, corners, sectors, kpis, notes].forEach(clear);
  for (const name of missing || []) notes.append(el('li', '', `${name} is not in this session's telemetry.`));
  if (!f || f.error) {
    chief.append(el('div', 'muted', `No findings for this pair: ${(f && f.error) || 'unavailable'}`));
    return;
  }
  const c = f.chief || {};
  const stat = (label, value, cls) => {
    const d = el('div');
    d.append(el('div', 'k', label), el('div', `big ${cls || ''}`, value));
    chief.append(d);
  };
  stat('lap delta', isNum(c.net_lap) ? `${signed(c.net_lap, 3)} s` : NA, c.net_lap > 0 ? 'loss' : 'gain');
  stat('lost in corners', isNum(c.lost) ? `${num(c.lost, 2)} s` : NA, 'loss');
  stat('corners losing', `${c.losing_n ?? NA} of ${c.total ?? NA}`);
  stat('clean-lap spread', isNum(c.time_spread) ? `${num(c.time_spread, 2)} s` : NA);
  if ((c.top3 || []).length) {
    const d = el('div');
    d.append(el('div', 'k', 'where the time is'));
    for (const t of c.top3) d.append(el('div', '', `${t.label}: ${num(t.gain, 2)} s · ${t.why}`));
    chief.append(d);
  }

  head(corners, ['', 'type', ['min speed', 'num'], ['Δt', 'num'], 'what the analysts saw']);
  for (const corner of f.corners || []) {
    const diags = el('td', 'diags');
    for (const dg of corner.diags || []) {
      const line = el('span', `diag ${dg.good ? 'good' : 'bad'}`);
      line.append(el('span', 'tag', dg.agent), document.createTextNode(dg.text));
      diags.append(line);
    }
    const tr = row(
      [
        corner.label,
        { text: corner.type, cls: 'muted' },
        { text: `${num(corner.min_main, 0)} / ${num(corner.min_ref, 0)}`, cls: 'num' },
        { text: signed(corner.net_dt, 3), cls: `num ${corner.net_dt > 0 ? 'loss' : 'gain'}` },
      ],
      'clickable'
    );
    tr.append(diags);
    tr.onclick = () => {
      corners.querySelectorAll('tr.selected').forEach((r) => r.classList.remove('selected'));
      tr.classList.add('selected');
      traces.highlight(cornerSpan(corner));
      if (view.lapLength) trackMap.setDot(pointAtPct(view.mapPoints, corner.distance / view.lapLength));
    };
    corners.append(tr);
  }

  head(sectors, ['', ['lap', 'num'], ['ref', 'num'], ['Δ', 'num']]);
  for (const s of f.sectors || []) {
    sectors.append(
      row([
        s.name,
        { text: num(s.main, 3), cls: 'num' },
        { text: num(s.ref, 3), cls: 'num' },
        { text: signed(s.delta, 3), cls: `num ${s.delta > 0 ? 'loss' : 'gain'}` },
      ])
    );
  }

  const m = (f.kpis && f.kpis.main) || {};
  const r = (f.kpis && f.kpis.ref) || {};
  head(kpis, ['', ['lap', 'num'], ['ref', 'num']]);
  for (const [key, label, digits, unit] of [
    ['trail_brake_pct', 'trail braking', 0, ' %'],
    ['avg_throttle_pct', 'average throttle', 0, ' %'],
    ['throttle_smoothness', 'throttle smoothness', 0, ''],
    ['steer_corrections', 'steering corrections', 0, ''],
    ['coasting_pct', 'coasting', 1, ' %'],
  ]) {
    kpis.append(row([label, { text: num(m[key], digits, unit), cls: 'num' }, { text: num(r[key], digits, unit), cls: 'num' }]));
  }
  for (const n of f.notes || []) notes.append(el('li', '', n));
}

// --------------------------------------------------------------------------
// audit
// --------------------------------------------------------------------------
function renderAuditFilters() {
  const box = $('audit-agent');
  const keep = box.value;
  clear(box);
  box.append(el('option', '', 'all agents'));
  box.firstChild.value = '';
  for (const agent of [...new Set(view.audit.map((r) => r.agent).filter(Boolean))]) {
    const option = el('option', '', humanise(agent));
    option.value = agent;
    box.append(option);
  }
  box.value = keep;
}

function renderAudit() {
  const table = q('audit');
  clear(table);
  const rows = filterAudit(view.audit, {
    agent: $('audit-agent').value,
    outcome: $('audit-outcome').value,
    sort: $('audit-sort').value,
  });
  setText(
    q('audit-sub'),
    view.audit.length ? `${rows.length} of ${view.audit.length} agent runs` : 'no agent ran in this run'
  );
  if (!view.audit.length) {
    table.append(
      row([
        {
          text: 'Agents only run when the pitwall agent layer is on (RTV_PITWALL_AGENTS with a Kimi Code key). This run recorded the race engine only.',
          cls: 'muted',
        },
      ])
    );
    return;
  }
  head(table, ['time', 'agent', 'woken by', 'model', ['calls', 'num'], 'tools', 'validator', 'outcome', ['took', 'num'], 'said']);
  for (const r of rows) {
    const validator = !r.validations
      ? '–'
      : r.unsupported.length
        ? `unsupported ${r.unsupported.join(', ')}${r.repaired ? ' (repaired)' : ''}`
        : 'grounded';
    const tr = row(
      [
        tlClock(r.t),
        { text: humanise(r.agent), cls: 'agent' },
        r.eventKey || '',
        { text: r.model || '', cls: 'muted' },
        { text: `${r.llmCalls}${r.llmErrors ? ` (${r.llmErrors} failed)` : ''}`, cls: 'num' },
        r.tools.join(', '),
        { text: validator, cls: r.unsupported.length ? 'loss' : '' },
        { text: r.outcome || 'unfinished', cls: `outcome-${r.outcome || 'running'}` },
        { text: isNum(r.latencyMs) ? `${(r.latencyMs / 1000).toFixed(1)} s` : '', cls: 'num' },
        { text: r.spoken || '', cls: 'said' },
      ],
      `clickable agent-${r.agent}`
    );
    tr.title = r.contextFrom.length ? `heard on the radio first: ${r.contextFrom.join(', ')}` : '';
    tr.onclick = () => {
      seek(r.t);
      $('panel-replay').scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    table.append(tr);
  }
}

// --------------------------------------------------------------------------
// recording a run, importing a file
// --------------------------------------------------------------------------
async function analyse() {
  let status = null;
  try {
    status = await getJson('/pitwall/status');
  } catch (_) {
    /* 503: the agent layer is not mounted, so this replay is free */
  }
  if (status && status.available && status.enabled !== false) {
    const go = window.confirm(
      'The pitwall agents are switched on, so replaying this session will call Kimi and use your quota. Continue?'
    );
    if (!go) return;
  }
  try {
    const response = await fetch(`${API}/replay/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: view.sessionId, speed: status && status.available ? 1 : 0 }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      notice(`could not start: ${body.detail || response.status}`);
      return;
    }
  } catch (_) {
    notice('could not reach the replay API');
    return;
  }
  $('analyse').disabled = true;
  notice('');
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 700));
    let replay;
    try {
      replay = await getJson('/replay/status');
    } catch (_) {
      break;
    }
    setText(
      $('session-status'),
      `recording a run… ${replay.frames}${replay.total ? ` / ${replay.total}` : ''} frames`
    );
    if (!replay.running) break;
  }
  await new Promise((resolve) => setTimeout(resolve, 400)); // the run closes on the replay thread
  $('analyse').disabled = false;
  view.runId = '';
  await loadRuns();
  await loadRun();
}

async function importFile(file) {
  if (!file) return;
  setText($('session-status'), `uploading ${file.name}…`);
  let job;
  try {
    const response = await fetch(`${API}/import/upload?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    job = await response.json();
    if (!response.ok) throw new Error(job.detail || response.status);
  } catch (error) {
    notice(`upload failed: ${error.message}`);
    return;
  }
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 800));
    let s;
    try {
      s = await getJson(`/import/${job.job_id}`);
    } catch (_) {
      break;
    }
    setText($('session-status'), `importing ${file.name}… ${Math.round((s.progress || 0) * 100)}%`);
    if (s.status === 'error') {
      notice(`import failed: ${s.error}`);
      return;
    }
    if (s.status === 'complete') {
      await loadSessions(s.session_id);
      return;
    }
  }
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------
$('session-select').onchange = (e) => selectSession(e.target.value);
$('run-select').onchange = async (e) => {
  view.runId = e.target.value;
  await loadRun();
  saveUrl();
};
$('main-lap').onchange = (e) => {
  view.main = Number(e.target.value);
  setupLapPickers();
  loadLapAnalysis();
};
$('ref-lap').onchange = (e) => {
  view.ref = Number(e.target.value);
  setupLapPickers();
  loadLapAnalysis();
};
$('play').onclick = () => setPlaying(!view.playing);
$('analyse').onclick = () => analyse();
$('ibt-file').onchange = (e) => importFile(e.target.files && e.target.files[0]);
for (const id of ['audit-agent', 'audit-outcome', 'audit-sort']) $(id).onchange = () => renderAudit();

loadSessions();

window.RTV_REVIEW = { view, timeline, flow, traces, trackMap };
