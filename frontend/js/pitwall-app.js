/**
 * The live pitwall: race state, and the agent layer visibly at work.
 *
 * Architecture, such as it is:
 *
 *   /ws/pitwall ─┬─ state    ─► coalesced to <=10 Hz ─► status / strategy / tower
 *                ├─ radio    ─► radio panel + RadioAudioManager (stage 4)
 *                ├─ event    ─► ticker (+ tower warnings, + fuel history) + timeline
 *                └─ activity ─► shared/activity-model.js ─► flow graph, runs, timeline
 *   /ws/live ────── raw channels ─► telemetry strip
 *
 * Every packet on the flow graph is one record the backend emitted while doing
 * that piece of work (rtv.pitwall.activity); nothing animates on a timer.
 *
 * Everything that decides *what a number means* lives in `pitwall/view-model.js`
 * and is tested without a browser; everything that decides *what it looks like*
 * lives in `pitwall/*-panel.js`; this file is the wiring and nothing else. The
 * split is the same one `audio-manager.js` / `speech.js` made in stage 4, for the
 * same reason.
 *
 * No storage APIs anywhere: mode, volume and mutes live in the URL, so a link is
 * shareable and a mute you cannot see cannot exist.
 */

import { RadioAudioManager } from './audio-manager.js';
import { PushToTalk, RadioSpeaker } from './speech.js';
import { DEFAULT_VOICES } from './radio-voices.js';

import { NA, humanise } from './pitwall/format.js';
import {
  FuelHistory,
  GapTrendTracker,
  TowerWarnings,
  eventLine,
  recommendationView,
  statusView,
  strategyView,
  towerView,
} from './pitwall/view-model.js';
import { StatusPanel } from './pitwall/status-panel.js';
import { StrategyPanel } from './pitwall/strategy-panel.js';
import { TowerPanel } from './pitwall/tower-panel.js';
import { RadioPanel } from './pitwall/radio-panel.js';
import { TickerPanel } from './pitwall/ticker-panel.js';
import { clear, el, setText } from './pitwall/dom.js';
import { AGENTS, apply, createModel, edgesFor, meanLatency } from './shared/activity-model.js';
import { FlowGraph } from './shared/flow-graph.js';
import { Timeline } from './shared/timeline.js';
import { RunsPanel } from './live/runs-panel.js';
import { TelemetryStrip } from './live/telemetry-strip.js';

const API = '/api/v1';
/** The screen is redrawn at most this often, whatever the socket does. */
const RENDER_MS = 100;
const POLL_MS = 5000;

const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------------------
// URL-backed settings (the house rule: never storage)
// --------------------------------------------------------------------------
const params = new URLSearchParams(window.location.search);
const settings = {
  volume: clamp01(params.has('volume') ? Number(params.get('volume')) : 0.9),
  master: params.get('master') !== '0',
  muted: new Set((params.get('muted') || '').split(',').filter(Boolean)),
};

function saveSettings() {
  const next = new URLSearchParams(window.location.search);
  next.set('volume', String(Math.round(settings.volume * 100) / 100));
  next.set('master', settings.master ? '1' : '0');
  if (settings.muted.size) next.set('muted', [...settings.muted].join(','));
  else next.delete('muted');
  window.history.replaceState({}, '', `${window.location.pathname}?${next}`);
}

// --------------------------------------------------------------------------
// audio (stage 4, unchanged -- this app is a second host for the same manager)
// --------------------------------------------------------------------------
let voices = DEFAULT_VOICES;

// The panels are built *before* the audio manager: applying the URL's mutes
// below fires the manager's onChange, and that callback paints the radio panel.
const statusPanel = new StatusPanel($('status-strip'));
const strategyPanel = new StrategyPanel($('panel-strategy'));
const towerPanel = new TowerPanel($('panel-tower'), { onToggle: () => renderNow() });
const radioPanel = new RadioPanel($('panel-radio'), { labels: agentLabel });
const tickerPanel = new TickerPanel($('panel-ticker'));
const runsPanel = new RunsPanel($('panel-runs'), { labels: agentLabel });
const flow = new FlowGraph(document.querySelector('[data-role="flow"]'));
const timeline = new Timeline(document.querySelector('[data-role="timeline"]'), {
  lanes: [
    { id: 'events', label: 'race events' },
    ...AGENTS.map((agent) => ({ id: agent, label: agentLabel(agent) })),
    { id: 'radio', label: 'radio aired' },
  ],
  windowS: 120,
});
const telemetry = new TelemetryStrip($('panel-telemetry'), {
  onFrames: (rate) => {
    app.framesPerS = rate;
  },
});

const speaker = new RadioSpeaker({
  voices,
  onEnd: (id) => manager.finished(id),
  onNotice: (text) => notice(text),
});

const manager = new RadioAudioManager({
  speaker,
  volume: settings.volume,
  onChange: (snapshot) => radioPanel.setSpeaking(snapshot),
});
manager.setMasterMuted(!settings.master);
settings.muted.forEach((agent) => manager.setAgentMuted(agent, true));
setInterval(() => manager.tick(), 500);

const trends = new GapTrendTracker();
const warnings = new TowerWarnings();
const fuelHistory = new FuelHistory();

const app = {
  state: null,
  dirty: false,
  socket: null,
  socketOpen: false,
  socketDetail: 'connecting',
  health: null,
  status: null,
  replay: null,
  recommendation: null,
  activity: createModel(),
  lastTick: null,
  lastPulse: 0,
  framesPerS: null,
};

// --------------------------------------------------------------------------
// the socket
// --------------------------------------------------------------------------
let reconnectDelay = 500;

function connect() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  app.socket = new WebSocket(`${scheme}://${window.location.host}/ws/pitwall`);

  app.socket.onopen = () => {
    reconnectDelay = 500;
    app.socketOpen = true;
    app.socketDetail = 'subscribed';
    // 10 Hz is the render ceiling too; asking for more would only cost bandwidth
    // to produce frames this app would coalesce away.
    app.socket.send(JSON.stringify({ op: 'subscribe', rate_hz: 10 }));
    renderNow();
  };
  app.socket.onclose = () => {
    app.socketOpen = false;
    app.socketDetail = 'reconnecting';
    renderNow();
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  };
  app.socket.onerror = () => {
    app.socketOpen = false;
    app.socketDetail = 'socket error';
  };
  app.socket.onmessage = (raw) => {
    let frame;
    try {
      frame = JSON.parse(raw.data);
    } catch (_) {
      return;
    }
    if (frame.type === 'state') onState(frame.state);
    else if (frame.type === 'radio') onRadio(frame.message);
    else if (frame.type === 'event') onEvent(frame.event);
    else if (frame.type === 'activity') onActivity(frame.activity);
  };
}

function onState(state) {
  // Frames moved through the engine since the last snapshot: one packet per
  // snapshot that actually advanced, never more than four a second.
  const now = performance.now();
  if (state && state.tick !== app.lastTick && now - app.lastPulse > 250) {
    if (app.lastTick !== null) {
      flow.pulse({ from: 'telemetry', to: 'engine' });
      flow.pulse({ from: 'engine', to: 'detectors', delay: true });
    }
    app.lastPulse = now;
  }
  if (state) app.lastTick = state.tick;
  if (state && Number.isFinite(state.session_time)) timeline.setNow(state.session_time);
  app.state = state;
  trends.update(state);
  warnings.clock(state && state.session_time);
  fuelHistory.update(state);
  app.dirty = true; // painted by the render tick, never straight from the socket
}

function onRadio(message) {
  const result = manager.push(message);
  radioPanel.push(message, result);
  if (message.agent === 'strategist' && !message.refused) {
    app.recommendation = recommendationView(message);
    strategyPanel.renderRecommendation(app.recommendation);
  }
}

function onEvent(event) {
  warnings.observe(event);
  tickerPanel.push(eventLine(event));
  flow.pulse({ from: 'detectors', to: 'bus' });
  timeline.addItem('events', {
    t0: event.session_time,
    label: `${event.key || event.event_type} · lap ${event.lap ?? '—'}`,
    cls: `sev-${event.severity || 'info'}`,
    record: event,
  });
  app.dirty = true;
}

function onActivity(record) {
  apply(app.activity, record);
  for (const edge of edgesFor(record)) flow.pulse(edge);
  placeOnTimeline(record);
  app.activityDirty = true;
}

function placeOnTimeline(record) {
  if (record.kind === 'run_start' && AGENTS.includes(record.agent)) {
    timeline.addItem(record.agent, {
      t0: record.session_time,
      label: `${agentLabel(record.agent)} woken by ${record.event_key}`,
      cls: `agent-${record.agent}`,
      record,
    });
  } else if (record.kind === 'radio_aired') {
    timeline.addItem('radio', {
      t0: record.session_time,
      label: `${agentLabel(record.agent)} on air (${record.data && record.data.priority})`,
      cls: `agent-${record.agent}`,
      record,
    });
  }
}

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------
function renderNow() {
  app.dirty = true;
  render();
}

function render() {
  if (!app.dirty) return;
  app.dirty = false;
  const state = app.state || {};
  const ctx = {
    socket: app.socketOpen,
    socketDetail: app.socketDetail,
    replay: app.replay,
    pitwallStatus: app.status,
  };
  statusPanel.render(statusView(state, ctx));
  strategyPanel.render(strategyView(state), fuelHistory.points());
  towerPanel.render(
    towerView(state, { expanded: towerPanel.expanded, trends, warnings })
  );
  renderFlow(state);
}

function renderActivity() {
  if (!app.activityDirty) return;
  app.activityDirty = false;
  runsPanel.render(app.activity);
  renderFlow(app.state || {});
}

/** The numbers on the graph's nodes. Each is a counter the backend reported. */
function renderFlow(state) {
  const m = app.activity;
  const metrics = state.metrics || {};
  const mounted = !!(app.status && app.status.available);
  const fps = app.framesPerS;
  flow.setNode('telemetry', {
    sub: fps ? `${fps.toFixed(0)} frames/s seen` : state.session ? state.session.source || '—' : '—',
  });
  flow.setNode('engine', {
    sub: Number.isFinite(metrics.avg_update_ms)
      ? `${metrics.avg_update_ms.toFixed(2)} ms/frame`
      : `tick ${state.tick ?? '—'}`,
  });
  flow.setNode('detectors', { sub: `${metrics.events ?? 0} events` });
  flow.setNode('bus', { sub: `${m.dispatch.events} dispatched` });
  flow.setNode('orchestrator', {
    sub: !mounted
      ? 'agent layer off'
      : m.lastBlocked && m.dispatch.routed === 0
        ? `blocked: ${m.lastBlocked}`
        : `${m.dispatch.routed} routed · ${m.dispatch.cooldown} cooldown`,
    status: mounted ? 'idle' : 'off',
  });
  for (const agent of AGENTS) {
    const a = m.agents[agent];
    const status = !mounted ? 'off' : a.status;
    const sub = !mounted
      ? 'not running'
      : a.status === 'idle'
        ? a.skipped
          ? `idle · ${a.skipped}`
          : `${a.runs} runs${a.refused ? ` · ${a.refused} refused` : ''}`
        : `${a.status}${a.detail ? ` · ${a.detail}` : ''}`;
    flow.setNode(agent, { sub, status });
  }
  const latency = meanLatency(m);
  flow.setNode('llm', {
    sub: `${m.llm.calls} calls${m.llm.inflight ? ` · ${m.llm.inflight} live` : ''}${
      latency !== null ? ` · ${(latency / 1000).toFixed(1)} s avg` : ''
    }`,
    status: m.llm.inflight ? 'thinking' : 'idle',
  });
  flow.setNode('tools', { sub: `${m.tools.calls} calls${m.tools.errors ? ` · ${m.tools.errors} failed` : ''}` });
  flow.setNode('validator', {
    sub: `${m.validator.checks} checks · ${m.validator.failed} failed`,
  });
  flow.setNode('radio', { sub: `${m.radio.queued} queued · ${m.radio.aired} aired` });
  flow.setNode('driver', { sub: state.player && state.player.lap ? `lap ${state.player.lap}` : '' });
  setText(
    document.querySelector('[data-role="flow-summary"]'),
    mounted
      ? `${m.lastSeq} records · ${m.llm.calls} model calls`
      : 'agent layer off: showing telemetry, engine and detectors only'
  );
}

setInterval(render, RENDER_MS);
setInterval(renderActivity, RENDER_MS);
window.addEventListener('resize', () => {
  strategyPanel.drawWindow();
  strategyPanel.drawSpark();
});

function agentLabel(agent) {
  if (agent === 'driver') return 'driver';
  const profile = voices[agent];
  if (profile && profile.label) return profile.label;
  return humanise(agent) || '?';
}

function notice(text) {
  const node = $('app-notice');
  setText(node, text || '');
  node.hidden = !text;
}

// --------------------------------------------------------------------------
// recorded sessions (the replay picker)
// --------------------------------------------------------------------------
/**
 * Replays come from sessions in the store: real captures and .ibt imports. The
 * picker lists them; there is no built-in synthetic race to fall back to.
 */
async function loadSessions() {
  const select = $('replay-session');
  let sessions = [];
  try {
    const response = await fetch(`${API}/sessions?limit=50`);
    const body = await response.json();
    sessions = (body && body.sessions) || [];
  } catch (_) {
    sessions = [];
  }
  const keep = select.value;
  clear(select);
  if (!sessions.length) {
    const option = el('option', '', 'no recorded sessions: import an .ibt first');
    option.value = '';
    select.append(option);
    return;
  }
  for (const session of sessions) {
    const label = [session.session_id, session.track_id, session.car_id]
      .filter(Boolean)
      .join(' · ');
    const option = el('option', '', label || NA);
    option.value = session.session_id;
    select.append(option);
  }
  if (keep && sessions.some((s) => s.session_id === keep)) select.value = keep;
}

// --------------------------------------------------------------------------
// replay controls (and the demo)
// --------------------------------------------------------------------------
async function startReplay(sessionId) {
  if (!sessionId) {
    notice('Pick a recorded session to replay (import an .ibt from the lap analysis app first).');
    return;
  }
  const speed = Number($('replay-speed').value) || 1;
  try {
    const response = await fetch(`${API}/replay/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, speed }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      notice(`replay refused: ${body.detail || response.status}`);
      return;
    }
    resetDerived();
    await poll();
    // Say why the radio column will stay empty, rather than letting a quiet
    // channel read as a broken one. Nothing here plays canned calls: a scripted
    // line drawn as an agent's call would be the ungrounded claim this codebase
    // refuses everywhere else.
    const live = app.status && app.status.available && app.status.enabled !== false;
    notice(
      live
        ? ''
        : 'Agents are not mounted (RTV_PITWALL_AGENTS + KIMI_CODE_API_KEY), so the ' +
          'agents and the radio stay idle. Telemetry, the engine and the detectors are live.'
    );
  } catch (error) {
    notice('could not reach the replay API');
  }
}

async function stopReplay() {
  try {
    await fetch(`${API}/replay/stop`, { method: 'POST' });
  } catch (_) {
    /* the poll below will report whatever actually happened */
  }
  await poll();
}

/**
 * Client-side accumulators are per-race. A replay that starts over would
 * otherwise show a gap trend against the previous run's numbers.
 */
function resetDerived() {
  trends.reset();
  warnings.reset();
  fuelHistory.reset();
  app.activity = createModel();
  app.activityDirty = true;
  app.lastTick = null;
  timeline.clear();
  app.recommendation = null;
  strategyPanel.renderRecommendation(null);
}

function wireReplay() {
  $('replay-start').onclick = () => {
    speaker.prime();
    startReplay($('replay-session').value.trim());
  };
  $('replay-stop').onclick = () => stopReplay();
  $('replay-speed').onchange = () => {
    if (app.replay && app.replay.running) notice('speed applies to the next replay');
  };
}

function renderReplayBar() {
  const replay = app.replay;
  const live = app.health && app.health.live;
  // The controls exist when a replay is running, and when nothing else is
  // feeding the engine -- which is the same state the demo is started from.
  const relevant = !!(replay && replay.running) || !(live && live.running);
  $('replay-bar').hidden = !relevant;
  $('replay-stop').disabled = !(replay && replay.running);
  $('replay-start').disabled = !!(replay && replay.running) || !!(live && live.running);
  setText(
    $('replay-progress'),
    replay && replay.running
      ? `${replay.session_id} · ${replay.speed || 'max'}× · ${replay.frames}${replay.total ? `/${replay.total}` : ''} frames`
      : replay && replay.finished
        ? `finished · ${replay.frames} frames`
        : live && live.running
          ? `live poller running (${live.state})`
          : 'idle'
  );
}

// --------------------------------------------------------------------------
// driver input
// --------------------------------------------------------------------------
async function sendDriverMessage(text, source) {
  const trimmed = String(text || '').trim();
  if (!trimmed) return;
  try {
    const response = await fetch(`${API}/pitwall/driver-message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: trimmed, source: source || 'text' }),
    });
    if (response.ok) {
      notice('');
      return;
    }
    const body = await response.json().catch(() => ({}));
    // 503 is the honest, common case: the agent layer is opt-in. Show the
    // message locally so the transcript is still a transcript, and say why the
    // pitwall did not answer rather than letting it look ignored.
    radioPanel.push(
      {
        agent: 'driver',
        priority: 'info',
        spoken_text: trimmed,
        detail_text: body.detail || `pitwall replied ${response.status}`,
        speak: false,
        message_id: `local-driver-${Date.now()}`,
        event_ref: {},
      },
      { action: 'not sent', reason: String(response.status) }
    );
    notice(body.detail || 'The pitwall agent layer is not mounted, so nobody heard that.');
  } catch (error) {
    notice('could not reach the pitwall');
  }
}

function wireDriverInput() {
  const field = $('driver-text');
  const send = () => {
    sendDriverMessage(field.value, 'text');
    field.value = '';
  };
  $('driver-send').onclick = send;
  field.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') send();
  });

  const button = $('driver-ptt');
  const ptt = new PushToTalk({
    onResult: (text) => sendDriverMessage(text, 'voice'),
    onState: (state, detail) => {
      button.classList.toggle('listening', state === 'listening');
      setText(button, state === 'listening' ? '🎙 listening…' : '🎙 Hold to talk');
      if (state === 'error') notice(`microphone: ${detail}`);
    },
  });
  if (!ptt.supported) {
    // No recognition API in this browser: hide the control rather than offer one
    // that cannot work. The text box is always there.
    $('driver-ptt-row').hidden = true;
    return;
  }
  const start = (event) => {
    event.preventDefault();
    ptt.start();
  };
  button.addEventListener('mousedown', start);
  button.addEventListener('touchstart', start, { passive: false });
  ['mouseup', 'mouseleave', 'touchend'].forEach((type) =>
    button.addEventListener(type, () => ptt.stop())
  );
}

// --------------------------------------------------------------------------
// radio controls (mutes + master volume live in the feed column)
// --------------------------------------------------------------------------
function buildMutes() {
  const host = $('radio-mutes');
  clear(host);
  AGENTS.forEach((agent) => {
    const button = el('button', `mute agent-${agent}`);
    button.dataset.agent = agent;
    const sync = () => {
      const muted = manager.mutedAgents().includes(agent);
      button.classList.toggle('off', muted);
      setText(button, `${muted ? '🔇' : '🔊'} ${agentLabel(agent)}`);
    };
    button.onclick = () => {
      const muted = manager.mutedAgents().includes(agent);
      manager.setAgentMuted(agent, !muted);
      if (muted) settings.muted.delete(agent);
      else settings.muted.add(agent);
      saveSettings();
      sync();
    };
    sync();
    host.append(button);
  });
}

function wireRadioControls() {
  const volume = $('radio-volume');
  volume.value = String(settings.volume);
  const syncVolume = () => setText($('radio-volume-value'), `${Math.round(settings.volume * 100)}%`);
  volume.oninput = () => {
    settings.volume = clamp01(Number(volume.value));
    manager.setVolume(settings.volume);
    syncVolume();
    saveSettings();
  };
  syncVolume();

  const master = $('radio-master');
  const syncMaster = () => {
    setText(master, settings.master ? '🔊 Radio on' : '🔇 Radio muted');
    master.classList.toggle('off', !settings.master);
  };
  master.onclick = () => {
    settings.master = !settings.master;
    manager.setMasterMuted(!settings.master);
    saveSettings();
    syncMaster();
  };
  syncMaster();

  $('radio-enable').onclick = () => {
    speaker.prime();
    $('radio-enable').hidden = true;
    const engine = speaker.describe();
    notice(engine.webSpeech ? '' : 'This browser has no Web Speech API; radio is shown, not spoken.');
  };
}

// --------------------------------------------------------------------------
// polls
// --------------------------------------------------------------------------
async function poll() {
  await Promise.all([pollHealth(), pollStatus()]);
  renderReplayBar();
  renderNow();
}

async function pollHealth() {
  try {
    const response = await fetch(`${API}/pitwall/health`);
    app.health = await response.json();
    app.replay = app.health ? app.health.replay : null;
  } catch (_) {
    app.health = null;
  }
}

async function pollStatus() {
  try {
    const response = await fetch(`${API}/pitwall/status`);
    app.status = await response.json();
  } catch (_) {
    app.status = null;
  }
}

async function seedHistory() {
  try {
    const response = await fetch(`${API}/racestate/events?limit=60`);
    const body = await response.json();
    const events = (body && body.events) || [];
    events.forEach((event) => warnings.observe(event));
    tickerPanel.seed(events.map(eventLine).filter(Boolean));
  } catch (_) {
    /* an empty ticker is honest before the first event */
  }
  try {
    const response = await fetch(`${API}/pitwall/radio?limit=40`);
    if (!response.ok) return; // 503 = agent layer not mounted; the panel says so
    const body = await response.json();
    const messages = (body && body.messages) || [];
    radioPanel.seed(messages);
    // Seeded history is *shown*, never spoken: the driver has already driven
    // past it. The manager still learns the ids so a reconnect cannot repeat it.
    messages.forEach((message) => manager.push({ ...message, speak: false }));
    const last = [...messages].reverse().find((m) => m.agent === 'strategist' && !m.refused);
    if (last) {
      app.recommendation = recommendationView(last);
      strategyPanel.renderRecommendation(app.recommendation);
    }
  } catch (_) {
    /* likewise */
  }
}

async function seedActivity() {
  try {
    const response = await fetch(`${API}/pitwall/activity?limit=2000`);
    if (!response.ok) return;
    const body = await response.json();
    // Folded into the model, not animated: these already happened.
    for (const record of (body && body.records) || []) {
      apply(app.activity, record);
      placeOnTimeline(record);
    }
    app.activityDirty = true;
  } catch (_) {
    /* the socket fills the model from here on */
  }
}

async function loadVoices() {
  try {
    const response = await fetch(`${API}/pitwall/tts`);
    if (!response.ok) return;
    const body = await response.json();
    if (body.voices && Object.keys(body.voices).length) {
      voices = { ...DEFAULT_VOICES, ...body.voices };
      speaker.setVoices(voices);
      buildMutes();
    }
  } catch (_) {
    /* the offline table in radio-voices.js is the fallback, by design */
  }
}

function clamp01(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 1;
  return Math.min(1, Math.max(0, number));
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------
wireRadioControls();
wireDriverInput();
wireReplay();
buildMutes();
strategyPanel.renderRecommendation(null);
radioPanel.setSpeaking(manager.snapshot());
saveSettings();
loadVoices();
loadSessions();
seedHistory();
seedActivity();
poll();
setInterval(poll, POLL_MS);
connect();

// Exposed for the console and for any later browser automation; nothing in the
// app reads it back.
window.RTV_PITWALL = {
  app,
  manager,
  speaker,
  panels: { statusPanel, strategyPanel, towerPanel, radioPanel, tickerPanel, runsPanel },
  flow,
  timeline,
  telemetry,
};
