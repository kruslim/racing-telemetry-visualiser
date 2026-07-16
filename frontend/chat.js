/* ============================================================
   chat.js — the AI race-engineer chat pane + LLM graph annotations.

   Sends the user's question plus the currently-selected main/ref laps to
   POST /api/v1/coaching/chat, renders the coach's answer, and pins the corners
   it flagged onto the Time/Distance graph as annotation markers (positioned via
   I2Graphs.getLayout().xOf(distance)). Clicking a corner jumps the graph cursor.
   ============================================================ */
(function (global) {
  'use strict';
  const API = '/api/v1';
  const $ = s => document.querySelector(s);

  const els = {
    pane: $('#chat-pane'),
    messages: $('#chat-messages'),
    form: $('#chat-form'),
    input: $('#chat-input'),
    send: $('#chat-send'),
    suggest: $('#chat-suggest'),
    scopeLaps: $('#chat-scope-laps'),
    sub: $('#chat-sub'),
  };

  const SUGGESTIONS = [
    'Where am I losing the most time?',
    'How do I fix my worst corner?',
    'Am I braking too early anywhere?',
    'What should I focus on next lap?',
  ];

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // very small markdown → HTML (bold, em, bullet lists, paragraphs)
  function mdToHtml(md) {
    const lines = String(md).split('\n');
    let html = '', inList = false;
    for (const raw of lines) {
      const line = raw.trimEnd();
      let esc = escapeHtml(line)
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/_(.+?)_/g, '<em>$1</em>');
      if (/^- /.test(line)) {
        if (!inList) { html += '<ul>'; inList = true; }
        html += '<li>' + esc.replace(/^-\s/, '') + '</li>';
      } else {
        if (inList) { html += '</ul>'; inList = false; }
        if (line !== '') html += '<p>' + esc + '</p>';
      }
    }
    if (inList) html += '</ul>';
    return html;
  }

  function scrollDown() { els.messages.scrollTop = els.messages.scrollHeight; }

  function addMessage(cls, html) {
    const div = document.createElement('div');
    div.className = 'msg ' + cls;
    div.innerHTML = html;
    els.messages.appendChild(div);
    scrollDown();
    return div;
  }

  function addTyping() {
    return addMessage('coach', '<span class="typing"><i></i><i></i><i></i></span>');
  }

  // ---- current lap context from the app state ----
  function currentScope() {
    const L = global.Laps, App = global.I2App;
    if (!L || !App || L.source !== 'api' || !L.sessionId) return null;
    const main = L.stint[App.st.mainIdx], ref = L.stint[App.st.refIdx];
    if (!main || !ref) return null;
    const mainNo = main.synthetic ? null : main.lapNo;
    const refNo = ref.synthetic ? null : ref.lapNo;
    if (mainNo == null || refNo == null) return null; // OPT lap can't be coached by number
    return { session_id: L.sessionId, main_lap: mainNo, ref_lap: refNo };
  }

  function refreshScope() {
    const L = global.Laps, App = global.I2App;
    if (!els.scopeLaps) return;
    if (!L || !App) { els.scopeLaps.textContent = '—'; return; }
    const main = L.stint[App.st.mainIdx], ref = L.stint[App.st.refIdx];
    const tag = l => l ? (l.synthetic ? 'OPT' : 'L' + l.lapNo) : '—';
    els.scopeLaps.textContent = `${tag(main)} vs ${tag(ref)}`;
  }

  // ---- render a coach answer ----
  function renderAnswer(resp) {
    let html = mdToHtml(resp.markdown || '');
    if (resp.annotations && resp.annotations.length) {
      const chips = resp.annotations.map((a, i) =>
        `<button class="corner-chip" data-d="${a.distance_m}">
           <span class="cc-dot ${a.severity}"></span>${escapeHtml(a.label)}
           <span class="cc-gain">+${Math.abs(a.gain_s).toFixed(2)}s</span>
         </button>`).join('');
      html += `<div class="msg-corners">${chips}</div>`;
    }
    const node = addMessage('coach', html);
    node.querySelectorAll('.corner-chip').forEach(btn => {
      btn.addEventListener('click', () => jumpToCorner(+btn.dataset.d));
    });
    ChatAnnotations.set(resp.annotations || []);
  }

  function renderRefusal(resp) {
    addMessage('coach refusal', mdToHtml(resp.markdown || 'I cannot answer that.'));
    ChatAnnotations.clear();
  }

  function jumpToCorner(distance) {
    const App = global.I2App;
    if (!App) return;
    if (App.st.ws !== 'timedist') App.setWorksheet('timedist');
    App.jumpTo(distance, 820);
  }

  // ---- send a question ----
  let busy = false;
  async function ask(question) {
    if (busy) return;
    const scope = currentScope();
    if (!scope) {
      addMessage('system', 'The chatbot needs the backend telemetry session. It is not available in offline mode.');
      return;
    }
    busy = true; els.send.disabled = true;
    addMessage('user', escapeHtml(question));
    const typing = addTyping();
    try {
      const r = await fetch(API + '/coaching/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...scope, question }),
      });
      typing.remove();
      if (r.status === 503) {
        const d = await r.json().catch(() => ({}));
        addMessage('system', escapeHtml(d.detail || 'The coaching model is not configured (set ANTHROPIC_API_KEY).'));
        return;
      }
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        addMessage('error', escapeHtml('Coaching error: ' + (d.detail || r.status)));
        return;
      }
      const resp = await r.json();
      if (resp.kind === 'refusal') renderRefusal(resp);
      else renderAnswer(resp);
    } catch (e) {
      typing.remove();
      addMessage('error', 'Network error reaching the coach. Is the server running?');
    } finally {
      busy = false; els.send.disabled = false;
    }
  }

  // ============================================================
  //  LLM GRAPH ANNOTATIONS  (window.ChatAnnotations)
  // ============================================================
  const ChatAnnotations = (function () {
    let anns = [];
    let container = null;
    let flags = [];

    function ensureContainer() {
      if (container && document.body.contains(container)) return container;
      const wrap = document.querySelector('#ws-timedist .graphwrap');
      if (!wrap) return null;
      container = document.createElement('div');
      container.id = 'llm-gmarkers';
      wrap.appendChild(container);
      return container;
    }

    function render() {
      const box = ensureContainer();
      if (!box) return;
      box.innerHTML = anns.map((a, i) =>
        `<button class="llm-flag ${a.severity}" data-i="${i}">${escapeHtml(a.label)}` +
        `<span class="lf-gain">+${Math.abs(a.gain_s).toFixed(2)}s</span>` +
        `<span class="lf-note">${escapeHtml(a.note || '')}</span></button>`).join('');
      flags = Array.from(box.querySelectorAll('.llm-flag'));
      flags.forEach(el => {
        el.addEventListener('click', () => {
          const a = anns[+el.dataset.i];
          if (a) jumpToCorner(a.distance_m);
        });
      });
      reposition();
    }

    function reposition() {
      const box = container;
      const G = global.I2Graphs, App = global.I2App;
      if (!box || !G || !App) return;
      const visible = App.st.ws === 'timedist' && anns.length > 0;
      box.style.display = visible ? 'block' : 'none';
      if (!visible) return;
      const lay = G.getLayout();
      if (!lay || !lay.xOf) return;
      flags.forEach((el, i) => {
        const a = anns[i];
        const x = lay.xOf(a.distance_m);
        el.style.left = x + 'px';
        el.style.display = (x < lay.px0 - 2 || x > lay.px0 + lay.pw + 2) ? 'none' : 'block';
      });
    }

    return {
      set(list) { anns = list || []; render(); },
      clear() { anns = []; if (container) container.innerHTML = ''; flags = []; },
      reposition,
    };
  })();
  global.ChatAnnotations = ChatAnnotations;

  // ============================================================
  //  INIT
  // ============================================================
  function autoGrow() {
    els.input.style.height = 'auto';
    els.input.style.height = Math.min(120, els.input.scrollHeight) + 'px';
  }

  function buildSuggestions() {
    els.suggest.innerHTML = SUGGESTIONS.map(s => `<span class="suggest-chip">${escapeHtml(s)}</span>`).join('');
    els.suggest.querySelectorAll('.suggest-chip').forEach(chip => {
      chip.addEventListener('click', () => { els.input.value = chip.textContent; autoGrow(); els.input.focus(); });
    });
  }

  function init() {
    if (!els.form) return;
    buildSuggestions();
    refreshScope();

    const online = global.Laps && global.Laps.source === 'api' && global.Laps.sessionId;
    if (els.sub) els.sub.textContent = online ? 'grounded on your telemetry' : 'offline mode — backend not connected';
    addMessage('coach',
      online
        ? "I'm your race engineer. I've loaded this session's laps. Ask me where you're losing time, how to fix a corner, or what to focus on — I'll answer from the telemetry and flag the corners on the graph."
        : "Backend telemetry isn't connected, so I can't coach right now. Start the RTV server and reload to chat.");

    els.form.addEventListener('submit', e => {
      e.preventDefault();
      const q = els.input.value.trim();
      if (!q) return;
      els.input.value = ''; autoGrow();
      ask(q);
    });
    els.input.addEventListener('input', autoGrow);
    els.input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); els.form.requestSubmit(); }
    });

    // keep the scope label in sync when the user changes main/ref laps
    const origRender = global.I2App && global.I2App.renderAll;
    window.addEventListener('rtv:lapchange', refreshScope);
  }

  // Patch lap selection so the scope label + a fresh annotation context follow.
  function hookLapChange() {
    const App = global.I2App;
    if (!App) return;
    ['setMain', 'setRef'].forEach(fn => {
      const orig = App[fn];
      if (typeof orig !== 'function') return;
      App[fn] = function (i) {
        const r = orig.call(this, i);
        refreshScope();
        return r;
      };
    });
  }

  init();
  hookLapChange();
})(window);
