/**
 * The listening page: /ws/pitwall -> audio discipline -> a voice.
 *
 * Everything hard lives elsewhere. `audio-manager.js` decides what is said and
 * when (and is tested without a browser); `speech.js` makes the noise. This file
 * is the wiring: a socket, a log, some switches, and a push-to-talk button that
 * only appears where it can actually work.
 *
 * State that survives a reload lives in the URL (`?volume=0.6&muted=coach`), not
 * in storage. Deliberate: a link is shareable, a mute you cannot see is not.
 */

import { RadioAudioManager } from './audio-manager.js';
import { PushToTalk, RadioSpeaker } from './speech.js';
import { DEFAULT_VOICES } from './radio-voices.js';

const AGENTS = ['strategist', 'vehicle_engineer', 'spotter', 'coach'];
const API = '/api/v1';

const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------------------
// URL-backed settings (no localStorage anywhere in this project)
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
// audio
// --------------------------------------------------------------------------
let voices = DEFAULT_VOICES;

const speaker = new RadioSpeaker({
  voices,
  onEnd: (id) => manager.finished(id),
  onNotice: (text) => note(text),
});

const manager = new RadioAudioManager({
  speaker,
  volume: settings.volume,
  onChange: renderChannel,
});

manager.setMasterMuted(!settings.master);
settings.muted.forEach((agent) => manager.setAgentMuted(agent, true));

// The channel needs a heartbeat so a stale info message is dropped even when
// nothing else is arriving.
setInterval(() => manager.tick(), 500);

// --------------------------------------------------------------------------
// the socket
// --------------------------------------------------------------------------
let socket = null;
let reconnectDelay = 500;

function connect() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${window.location.host}/ws/pitwall`);

  socket.onopen = () => {
    reconnectDelay = 500;
    setStatus('connected', 'live');
    socket.send(JSON.stringify({ op: 'subscribe', rate_hz: 2 }));
  };
  socket.onclose = () => {
    setStatus('offline', 'reconnecting');
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  };
  socket.onerror = () => setStatus('offline', 'socket error');
  socket.onmessage = (event) => {
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (frame.type === 'radio') onRadio(frame.message);
    else if (frame.type === 'state') onState(frame.state);
  };
}

function onState(state) {
  const player = state.player || {};
  const flags = state.flags || {};
  $('state-line').textContent =
    `lap ${player.lap ?? '-'} · P${player.position ?? '-'} · ` +
    `${flags.phase || 'unknown'} · v${state.version}`;
}

function onRadio(message) {
  const result = manager.push(message);
  logMessage(message, result);
}

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------
function logMessage(message, result) {
  const row = document.createElement('div');
  const priority = message.priority || 'info';
  row.className = `msg ${priority} ${message.agent === 'driver' ? 'driver' : ''}`;
  const action = result ? result.action : 'logged';
  const reason = result && result.reason ? ` (${result.reason})` : '';
  row.innerHTML = `
    <div class="msg-head">
      <span class="agent agent-${message.agent}">${label(message.agent)}</span>
      <span class="pri">${priority}</span>
      <span class="action">${action}${reason}</span>
    </div>
    <div class="spoken">${escapeHtml(message.spoken_text || '')}</div>
    <div class="detail">${escapeHtml(message.detail_text || '')}</div>`;
  const log = $('log');
  log.prepend(row);
  while (log.children.length > 80) log.removeChild(log.lastChild);
}

function renderChannel(snapshot) {
  const speaking = snapshot.speaking;
  $('now').textContent = speaking
    ? `${label(speaking.agent)}: ${speaking.spoken_text}`
    : 'channel clear';
  $('now').className = speaking ? `now ${speaking.priority}` : 'now';
  $('queue').textContent = snapshot.queue.length
    ? snapshot.queue.map((m) => `${label(m.agent)} (${m.priority})`).join('  ·  ')
    : '—';
  const c = snapshot.counters;
  $('counters').textContent =
    `spoken ${c.spoken} · queued ${c.queued} · superseded ${c.superseded} · ` +
    `interrupted ${c.interrupted} · stale ${c.droppedStale} · muted ${c.droppedMuted}`;
}

function label(agent) {
  const profile = voices[agent];
  if (profile && profile.label) return profile.label;
  return String(agent || '?').replace(/_/g, ' ');
}

function setStatus(cls, text) {
  const el = $('conn');
  el.textContent = text;
  el.className = `pill ${cls}`;
}

function note(text) {
  $('notice').textContent = text;
  $('notice').hidden = !text;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// --------------------------------------------------------------------------
// controls
// --------------------------------------------------------------------------
function buildMutes() {
  const host = $('mutes');
  AGENTS.forEach((agent) => {
    const button = document.createElement('button');
    button.className = `mute agent-${agent}`;
    button.dataset.agent = agent;
    const sync = () => {
      const muted = manager.mutedAgents().includes(agent);
      button.classList.toggle('off', muted);
      button.textContent = `${muted ? '🔇' : '🔊'} ${label(agent)}`;
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
    host.appendChild(button);
  });
}

function wireControls() {
  const volume = $('volume');
  volume.value = String(settings.volume);
  volume.oninput = () => {
    settings.volume = clamp01(Number(volume.value));
    manager.setVolume(settings.volume);
    $('volume-value').textContent = `${Math.round(settings.volume * 100)}%`;
    saveSettings();
  };
  $('volume-value').textContent = `${Math.round(settings.volume * 100)}%`;

  const master = $('master');
  const syncMaster = () => {
    master.textContent = settings.master ? '🔊 Radio on' : '🔇 Radio muted';
    master.classList.toggle('off', !settings.master);
  };
  master.onclick = () => {
    settings.master = !settings.master;
    manager.setMasterMuted(!settings.master);
    saveSettings();
    syncMaster();
  };
  syncMaster();

  $('enable').onclick = () => {
    // Browsers require a gesture before anything makes a sound.
    speaker.prime();
    $('enable').hidden = true;
    const engine = speaker.describe();
    note(
      engine.webSpeech
        ? ''
        : 'This browser has no Web Speech API; radio will be shown, not spoken.'
    );
  };

  $('demo').onclick = () => {
    speaker.prime();
    playSyntheticScenario();
  };

  $('replay').onclick = async () => {
    speaker.prime();
    $('replay').disabled = true;
    try {
      await fetch(`${API}/replay/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: 'scenario', speed: 1.0 }),
      });
      if (!agentsLive) {
        // The race is running but nobody is on the radio: the agent layer needs
        // RTV_PITWALL_AGENTS and a key. Say so, and play the scripted calls so
        // the replay is still audible -- clearly labelled as scripted, because
        // pretending a canned line came from an agent is exactly the kind of
        // ungrounded claim this project refuses everywhere else.
        note(
          'Agents are not mounted (RTV_PITWALL_AGENTS + ANTHROPIC_API_KEY), so the ' +
            'radio below is the scripted demo, not live agent output.'
        );
        playSyntheticScenario();
      }
    } finally {
      setTimeout(() => ($('replay').disabled = false), 1000);
    }
  };
}

// --------------------------------------------------------------------------
// the synthetic scenario -- the demo that needs no API key
// --------------------------------------------------------------------------
/**
 * A scripted minute of race radio, pushed through the *same* manager the socket
 * feeds. Third call is a critical spotter shout that lands while the strategist
 * is mid-sentence: that is the interrupt, audible.
 */
const SCENARIO = [
  { at: 0, agent: 'strategist', priority: 'advisory', subject: 'pit_stop',
    spoken_text: 'Fuel window opens lap five. Plan is fuel only, no tyres.',
    detail_text: 'Window lap 5 to 7 on 0.5 litres a lap.' },
  { at: 1200, agent: 'vehicle_engineer', priority: 'advisory', subject: 'tyres',
    spoken_text: 'Left front is climbing three degrees a lap. Ease the entry.',
    detail_text: 'Stint trend over four green laps.' },
  { at: 2600, agent: 'spotter', priority: 'critical', subject: 'traffic',
    spoken_text: 'Yellow, yellow! Car stopped at turn four, slow down.',
    detail_text: 'Flag change into yellow.' },
  { at: 6000, agent: 'strategist', priority: 'advisory', subject: 'pit_stop',
    spoken_text: 'Box this lap under the yellow, three litres.',
    detail_text: 'Supersedes the earlier window call.' },
  { at: 6400, agent: 'strategist', priority: 'advisory', subject: 'pit_stop',
    spoken_text: 'Correction: box next lap, three point two litres.',
    detail_text: 'This should supersede the queued call, not follow it.' },
  { at: 9000, agent: 'coach', priority: 'info', subject: 'coaching',
    spoken_text: 'Braking is five metres early into turn one.',
    detail_text: 'Info: only aired if the channel is free.' },
];

let demoRun = 0;
function playSyntheticScenario() {
  const run = ++demoRun;
  SCENARIO.forEach((item, index) => {
    setTimeout(() => {
      if (run !== demoRun) return;
      const { at, ...message } = item;
      onRadio({
        ...message,
        message_id: `demo-${run}-${index}`,
        grounded: true,
        refused: false,
        data: {},
      });
    }, item.at);
  });
}

// --------------------------------------------------------------------------
// push to talk
// --------------------------------------------------------------------------
function wirePushToTalk() {
  const button = $('ptt');
  const ptt = new PushToTalk({
    onResult: async (text) => {
      $('ptt-text').textContent = `"${text}"`;
      try {
        await fetch(`${API}/pitwall/driver-message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, source: 'voice' }),
        });
      } catch (_) {
        $('ptt-text').textContent = 'could not reach the pitwall';
      }
    },
    onState: (state, detail) => {
      button.classList.toggle('listening', state === 'listening');
      button.textContent = state === 'listening' ? '🎙 listening…' : '🎙 Hold to talk';
      if (state === 'error') $('ptt-text').textContent = `microphone: ${detail}`;
    },
  });

  if (!ptt.supported) {
    // No recognition API: hide the control rather than offer one that cannot work.
    $('ptt-row').hidden = true;
    return;
  }
  const start = (e) => {
    e.preventDefault();
    ptt.start();
  };
  const stop = () => ptt.stop();
  button.addEventListener('mousedown', start);
  button.addEventListener('touchstart', start, { passive: false });
  ['mouseup', 'mouseleave', 'touchend'].forEach((type) =>
    button.addEventListener(type, stop)
  );
}

// --------------------------------------------------------------------------
// boot
// --------------------------------------------------------------------------
let agentsLive = false;

async function loadStatus() {
  try {
    const response = await fetch(`${API}/pitwall/status`);
    const body = await response.json();
    agentsLive = !!body.available && body.enabled !== false;
    $('agents').textContent = agentsLive
      ? `agents: ${(body.agents || []).length} live`
      : 'agents: not mounted';
    $('agents').className = `pill ${agentsLive ? 'connected' : ''}`;
  } catch (_) {
    $('agents').textContent = 'agents: unknown';
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
    }
    $('engine').textContent =
      body.engine === 'backend'
        ? `backend voice: ${body.provider}`
        : `browser Web Speech${speaker.hasWebSpeech ? '' : ' (unavailable)'}`;
  } catch (_) {
    $('engine').textContent = 'browser Web Speech';
  }
}

function clamp01(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 1;
  return Math.min(1, Math.max(0, number));
}

buildMutes();
wireControls();
wirePushToTalk();
renderChannel(manager.snapshot());
loadVoices();
loadStatus();
connect();

// Exposed for manual poking in the console; nothing in the app reads it back.
window.RTV_RADIO = { manager, speaker, playSyntheticScenario };
