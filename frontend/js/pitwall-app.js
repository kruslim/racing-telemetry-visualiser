/**
 * The pitwall app: mode switching, one socket, five panels and a voice.
 *
 * Architecture, such as it is:
 *
 *   /ws/pitwall ─┬─ state  ─► coalesced to <=10 Hz ─► status / strategy / tower
 *                ├─ radio  ─► radio panel + RadioAudioManager (stage 4)
 *                └─ event  ─► ticker  (+ tower warnings, + fuel history)
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

const API = '/api/v1';
const AGENTS = ['strategist', 'vehicle_engineer', 'spotter', 'coach'];
/** The screen is redrawn at most this often, whatever the socket does. */
const RENDER_MS = 100;
const POLL_MS = 5000;

const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------------------
// URL-backed settings (the house rule: never storage)
// --------------------------------------------------------------------------
const params = new URLSearchParams(window.location.search);
const settings = {
  mode: params.get('mode') === 'analysis' ? 'analysis' : 'pitwall',
  volume: clamp01(params.has('volume') ? Number(params.get('volume')) : 0.9),
  master: params.get('master') !== '0',
  muted: new Set((params.get('muted') || '').split(',').filter(Boolean)),
  analysis: params.get('analysis') || '',
};

function saveSettings() {
  const next = new URLSearchParams(window.location.search);
  next.set('mode', settings.mode);
  next.set('volume', String(Math.round(settings.volume * 100) / 100));
  next.set('master', settings.master ? '1' : '0');
  if (settings.muted.size) next.set('muted', [...settings.muted].join(','));
  else next.delete('muted');
  if (settings.analysis) next.set('analysis', settings.analysis);
  else next.delete('analysis');
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
  };
}

function onState(state) {
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
  app.dirty = true;
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
}

setInterval(render, RENDER_MS);
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
// modes
// --------------------------------------------------------------------------
function setMode(mode) {
  settings.mode = mode === 'analysis' ? 'analysis' : 'pitwall';
  $('mode-pitwall').hidden = settings.mode !== 'pitwall';
  $('mode-analysis').hidden = settings.mode !== 'analysis';
  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.classList.toggle('active', button.dataset.mode === settings.mode);
  });
  saveSettings();
  if (settings.mode === 'pitwall') renderNow();
  else loadSessions();
}

function wireModes() {
  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.onclick = () => setMode(button.dataset.mode);
  });
}

// --------------------------------------------------------------------------
// analysis mode
// --------------------------------------------------------------------------
/**
 * The MoTeC-style worksheets are a separate app served on its own origin (see
 * docs/PITWALL.md), so this mode is a launcher rather than a re-implementation:
 * the v1 session list, and a link into whichever build of the analysis UI you
 * are running. Point it somewhere with the field and the URL remembers.
 */
async function loadSessions() {
  const host = $('analysis-sessions');
  clear(host);
  host.append(el('div', 'muted', 'loading sessions…'));
  let body;
  try {
    const response = await fetch(`${API}/sessions?limit=50`);
    body = await response.json();
  } catch (error) {
    clear(host);
    host.append(el('div', 'muted', `could not reach ${API}/sessions`));
    return;
  }
  clear(host);
  const sessions = (body && body.sessions) || [];
  if (!sessions.length) {
    host.append(el('div', 'muted', 'no captured sessions yet'));
    return;
  }
  for (const session of sessions) {
    const row = el('div', 'session-row');
    row.append(el('span', 'sid', session.session_id || NA));
    row.append(el('span', 'track', session.track_id || NA));
    row.append(el('span', 'car', session.car_id || NA));
    row.append(el('span', 'kind', session.kind || NA));
    const actions = el('span', 'actions');
    if (settings.analysis) {
      const link = el('a', 'button-link', 'open in analysis');
      link.href = `${settings.analysis}${settings.analysis.includes('?') ? '&' : '?'}session=${encodeURIComponent(session.session_id)}`;
      link.target = '_blank';
      link.rel = 'noreferrer';
      actions.append(link);
    }
    const replay = el('button', '', '⟲ replay on the pitwall');
    replay.onclick = () => startReplay(session.session_id);
    actions.append(replay);
    row.append(actions);
    host.append(row);
  }
}

function wireAnalysis() {
  const field = $('analysis-url');
  field.value = settings.analysis;
  field.oninput = () => {
    settings.analysis = field.value.trim();
    saveSettings();
  };
  $('analysis-open').onclick = () => {
    if (settings.analysis) window.open(settings.analysis, '_blank', 'noreferrer');
    else notice('Set the analysis app URL first — it is served separately from this API.');
  };
  $('analysis-refresh').onclick = () => loadSessions();
}

// --------------------------------------------------------------------------
// replay controls (and the demo)
// --------------------------------------------------------------------------
async function startReplay(sessionId) {
  const speed = Number($('replay-speed').value) || 1;
  try {
    const response = await fetch(`${API}/replay/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId || 'scenario', speed }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      notice(`replay refused: ${body.detail || response.status}`);
      return;
    }
    setMode('pitwall');
    resetDerived();
    await poll();
    // Say why the radio column will stay empty, rather than letting a quiet
    // channel read as a broken one. This page deliberately does *not* play the
    // scripted calls /radio.html falls back to -- a canned line rendered as an
    // agent's call, in a panel whose whole job is showing agent calls, would be
    // the ungrounded claim this codebase refuses everywhere else.
    const live = app.status && app.status.available && app.status.enabled !== false;
    notice(
      live
        ? ''
        : 'Agents are not mounted (RTV_PITWALL_AGENTS + ANTHROPIC_API_KEY), so the ' +
          'radio column stays empty. Everything else below is live. /radio.html ' +
          'plays the scripted demo calls, labelled as scripted.'
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
  app.recommendation = null;
  strategyPanel.renderRecommendation(null);
}

function wireReplay() {
  $('replay-start').onclick = () => {
    speaker.prime();
    startReplay($('replay-session').value.trim() || 'scenario');
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
wireModes();
wireRadioControls();
wireDriverInput();
wireReplay();
wireAnalysis();
buildMutes();
strategyPanel.renderRecommendation(null);
radioPanel.setSpeaking(manager.snapshot());
setMode(settings.mode);
loadVoices();
seedHistory();
poll();
setInterval(poll, POLL_MS);
connect();

// Exposed for the console and for any later browser automation; nothing in the
// app reads it back.
window.RTV_PITWALL = {
  app,
  manager,
  speaker,
  panels: { statusPanel, strategyPanel, towerPanel, radioPanel, tickerPanel },
};
