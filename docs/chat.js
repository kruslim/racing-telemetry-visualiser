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
    foot: $('#chat-foot'),
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

  // ============================================================
  //  OFFLINE COACH — grounded answers from the deterministic model
  //
  //  When there's no backend (the static showcase, or the server
  //  running without ANTHROPIC_API_KEY), the chat still coaches: it
  //  reads the SAME corner findings the Coach worksheet computes,
  //  cites their real numbers, pins them onto the graph — and, like
  //  the backend Layer-3b agent, *refuses* questions the captured
  //  channels can't answer instead of inventing one.
  // ============================================================
  const CAPTURED = ['Speed', 'Throttle', 'Brake', 'Gear', 'RPM',
    'Steering', 'Lateral G', 'Longitudinal G'];
  const REFUSALS = [
    { re: /\b(tyre|tire)s?\b|\b(tyre|tire)\s*(temp|pressure|wear)/, what: 'tyre temperatures or pressures' },
    { re: /\bfuel\b|\bmpg\b/, what: 'fuel load or consumption' },
    { re: /brake\s*(temp|disc|rotor)|\brotor\b|disc\s*temp/, what: 'brake temperatures' },
    { re: /(oil|water|engine)\s*temp|coolant/, what: 'engine, oil or water temperatures' },
    { re: /\b(weather|rain|wind|track\s*temp|air\s*temp|grip\s*level)\b/, what: 'weather or track conditions' },
    { re: /\b(damper|shock|ride\s*height|spring|setup)\b/, what: 'suspension setup values' },
  ];

  const g2 = t => (t >= 0 ? '+' : '−') + Math.abs(t).toFixed(2);
  const g3 = t => (t >= 0 ? '+' : '−') + Math.abs(t).toFixed(3);
  const lapClock = t => { if (!isFinite(t)) return '—'; const m = Math.floor(t / 60); return m + ':' + (t - m * 60).toFixed(2).padStart(5, '0'); };
  const sev = dt => dt > 0.25 ? 'high' : dt > 0.08 ? 'medium' : 'low';

  function offlineModel() {
    const App = global.I2App, C = global.Coach;
    if (!App || !C || !C.getModel) return null;
    try { return C.getModel(App.st); } catch (e) { return null; }
  }
  function annOf(c, note) {
    const worst = c.diags.filter(d => !d.good && !d.variance).sort((a, b) => b.t - a.t)[0];
    return { distance_m: c.apexD, label: c.label, severity: sev(c.netDt),
      gain_s: c.netDt, note: note || (worst ? worst.text : c.type) };
  }
  const losers = m => m.corners.filter(c => c.netDt > 0.02).sort((a, b) => b.netDt - a.netDt);
  const ans = (markdown, annotations) => ({ kind: 'answer', markdown, annotations: annotations || [] });
  const refuse = markdown => ({ kind: 'refusal', markdown, annotations: [] });

  function topReasonTxt(c) {
    const d = c.diags.filter(x => !x.variance).sort((a, b) => b.t - a.t)[0];
    return d ? d.text : (c.minMain < c.minRef ? 'Low minimum speed' : 'Lost on the line');
  }

  function cornerAnswer(c) {
    const bad = c.diags.filter(d => !d.good).sort((a, b) => b.t - a.t);
    const dk = c.minMain - c.minRef;
    if (!bad.length) {
      return ans(`**${c.label}** (${c.type}, S${c.sector}) is one of your stronger corners — ` +
        `${c.netDt <= 0 ? `you're **${g3(c.netDt)}s** up on the reference here` : 'no clear deficit versus the reference'}. ` +
        `Min speed **${Math.round(c.minMain)} km/h** vs ref **${Math.round(c.minRef)}**.`, [annOf(c)]);
    }
    const lines = bad.slice(0, 4).map(d => `- ${d.text}${d.t > 0.004 ? ` _(${g2(d.t)}s)_` : ''}`);
    return ans(
      `**${c.label}** — ${c.type}, Sector ${c.sector}. You're **${g3(c.netDt)}s** off the reference here.\n\n` +
      `Min speed **${Math.round(c.minMain)} km/h** vs ref **${Math.round(c.minRef)}** ` +
      `_(${dk >= 0 ? '+' : '−'}${Math.abs(dk).toFixed(0)} km/h)_. What's costing you:\n\n` +
      lines.join('\n'), [annOf(c)]);
  }

  function offlineAnswer(qRaw) {
    const q = String(qRaw).toLowerCase();
    const m = offlineModel();
    if (!m) return ans("I'm still loading this session's telemetry — ask me again in a moment.");

    // grounded refusal: a channel this session never captured
    for (const r of REFUSALS) {
      if (r.re.test(q)) {
        return refuse(
          `I can't answer that from this session — **${r.what}** weren't captured in the telemetry I have.\n\n` +
          `What I *can* work from: _${CAPTURED.join(' · ')}_. Ask me about brake points, minimum corner speed, ` +
          `throttle application, gearing or where the lap time is going.`);
      }
    }

    const top = losers(m), chief = m.chief;

    // a specific corner: "turn 3", "corner 3", "t3"
    const tm = q.match(/\b(?:turn|corner|t)\s*(\d{1,2})\b/);
    if (tm) {
      const c = m.corners.find(x => x.label.toLowerCase() === ('t' + tm[1]));
      if (c) return cornerAnswer(c);
    }

    // brake points
    if (/\bbrak/.test(q)) {
      const bf = [];
      m.corners.forEach(c => c.diags.forEach(d => { if (d.agent === 'brake') bf.push({ c, d }); }));
      if (!bf.length) return ans("Your braking lines up well with the reference — no corner where you brake meaningfully early or late. The time's going elsewhere; ask me *where am I losing the most time?*", top.slice(0, 3).map(c => annOf(c)));
      bf.sort((a, b) => b.d.mag - a.d.mag);
      const early = bf.filter(x => /early/.test(x.d.text)).length;
      const lead = early
        ? `Yes — you're braking early at ${early} corner${early > 1 ? 's' : ''}. Carry the brake deeper to hold more entry speed:`
        : `A few brake-point deltas stand out versus the reference:`;
      const lines = bf.slice(0, 4).map(({ c, d }) => `- **${c.label}** — ${d.text.toLowerCase()}${d.t > 0.004 ? ` _(${g2(d.t)}s)_` : ''}`);
      return ans(`${lead}\n\n${lines.join('\n')}`, bf.slice(0, 4).map(({ c, d }) => annOf(c, d.text)));
    }

    // sectors
    if (/\bsector|\bs1\b|\bs2\b|\bs3\b|\bsplit/.test(q)) {
      const lines = m.sectors.map(s => `- **${s.name}** — ${lapClock(s.main)} vs ref ${lapClock(s.ref)} _(${g3(s.delta)}s)_`);
      const worst = m.sectors.slice().sort((a, b) => b.delta - a.delta)[0];
      return ans(`Sector splits, main vs reference:\n\n${lines.join('\n')}\n\nMost of the loss is in **${worst.name}** (${g3(worst.delta)}s) — focus your next run there.`,
        top.filter(c => c.sector === worst.i + 1).slice(0, 3).map(c => annOf(c)));
    }

    // throttle / traction
    if (/throttle|power|traction|on the gas|\baccel/.test(q)) {
      const tf = [];
      m.corners.forEach(c => c.diags.forEach(d => { if (d.agent === 'throttle') tf.push({ c, d }); }));
      const km = m.kpis.main.full, kr = m.kpis.ref.full;
      if (!tf.length) return ans(`Your throttle application is tidy — average throttle **${km.toFixed(0)}%** vs the reference's **${kr.toFixed(0)}%**, and no corner where you're clearly late to power.`, []);
      const lines = tf.slice(0, 3).map(({ c, d }) => `- **${c.label}** — ${d.text.toLowerCase()}${d.t > 0.004 ? ` _(${g2(d.t)}s)_` : ''}`);
      return ans(`On throttle you're leaving time here:\n\n${lines.join('\n')}\n\nAverage throttle **${km.toFixed(0)}%** vs ref **${kr.toFixed(0)}%** — get to full throttle earlier on exit.`, tf.slice(0, 3).map(({ c, d }) => annOf(c, d.text)));
    }

    // gearing
    if (/\bgear|shift|\brpm|\brev/.test(q)) {
      const gf = [];
      m.corners.forEach(c => c.diags.forEach(d => { if (d.agent === 'gear') gf.push({ c, d }); }));
      if (!gf.length) return ans("Your gear selection matches the reference through every corner — nothing to change there.", []);
      const lines = gf.slice(0, 4).map(({ c, d }) => `- **${c.label}** — ${d.text.toLowerCase()}`);
      return ans(`Gear-selection differences versus the reference:\n\n${lines.join('\n')}`, gf.slice(0, 4).map(({ c, d }) => annOf(c, d.text)));
    }

    // consistency
    if (/consisten|repeat|lap.to.lap|spread/.test(q)) {
      return ans(`Across the stint your lap-time spread is **${chief.tSpread.toFixed(2)}s**. The biggest min-speed variation is at the priority corners below — nail the same entry every lap and that spread comes down.`, top.slice(0, 3).map(c => annOf(c)));
    }

    // worst corner / how to fix
    if (/\bworst\b|\bfix\b|\bimprove\b|\bbiggest\b|which corner/.test(q)) {
      if (!top.length) return ans("Genuinely clean lap — no corner is costing you meaningful time versus the reference. Keep repeating it.");
      return cornerAnswer(top[0]);
    }

    // focus / next lap / priorities
    if (/\bfocus\b|next lap|priorit|what should|work on|where.*start/.test(q)) {
      if (!chief.top3.length) return ans("Nothing major to fix — you're within a couple of hundredths of the reference everywhere. Just repeat the lap.");
      const lines = chief.top3.map((t, i) => `- **${i + 1}. ${t.label}** — ${t.why.toLowerCase()} _(${g3(t.gain)}s)_`);
      return ans(`There's **${chief.lost.toFixed(2)}s** on the table versus your reference. In priority order:\n\n${lines.join('\n')}\n\nStart with **${chief.top3[0].label}** — it's the single biggest gain.`, chief.top3.map(t => annOf(m.corners[t.idx])));
    }

    // lap time
    if (/lap time|how fast|my time|best lap|fastest/.test(q)) {
      return ans(`Main lap **${lapClock(m.main.time)}**, reference **${lapClock(m.ref.time)}** — a net **${g3(chief.netLap)}s**. You're losing at ${chief.losingN} of ${chief.total} corners, worth **${chief.lost.toFixed(2)}s** if you clean them up.`, top.slice(0, 3).map(c => annOf(c)));
    }

    // default: where am I losing time
    if (!top.length) return ans("This lap is right on the reference — no corner is costing you time. Ask me about a specific corner if you want the detail.");
    const lines = top.slice(0, 3).map(c => `- **${c.label}** (${c.type}) — ${topReasonTxt(c).toLowerCase()} _(${g3(c.netDt)}s)_`);
    return ans(`You're losing **${chief.lost.toFixed(2)}s** to the reference, mostly at these three:\n\n${lines.join('\n')}\n\nClick a flag on the graph to jump there, or ask me *how do I fix ${top[0].label}?*`, top.slice(0, 3).map(c => annOf(c)));
  }

  // ---- send a question ----
  let busy = false;
  async function ask(question) {
    if (busy) return;
    busy = true; els.send.disabled = true;
    addMessage('user', escapeHtml(question));
    const typing = addTyping();
    const online = global.Laps && global.Laps.source === 'api' && global.Laps.sessionId;
    try {
      // ---- offline / static demo: coach locally from the findings ----
      if (!online) {
        await new Promise(res => setTimeout(res, 380 + Math.random() * 320));
        typing.remove();
        const resp = offlineAnswer(question);
        if (resp.kind === 'refusal') renderRefusal(resp); else renderAnswer(resp);
        return;
      }
      // ---- online: the real backend coach ----
      const scope = currentScope();
      if (!scope) {
        typing.remove();
        addMessage('system', 'Select two real laps (not the OPT lap) for the coach to compare.');
        return;
      }
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
      chip.addEventListener('click', () => { if (!busy) ask(chip.textContent); });
    });
  }

  function init() {
    if (!els.form) return;
    buildSuggestions();
    refreshScope();

    const online = global.Laps && global.Laps.source === 'api' && global.Laps.sessionId;
    if (els.sub) els.sub.textContent = online ? 'grounded on your telemetry' : 'grounded on the deterministic findings';
    if (els.foot && !online) {
      els.foot.innerHTML = 'Live demo · simulated session · ' +
        '<a href="https://github.com/kruslim/racing-telemetry-visualiser" target="_blank" rel="noopener">Source ↗</a>';
    }
    addMessage('coach',
      online
        ? "I'm your race engineer. I've loaded this session's laps. Ask me where you're losing time, how to fix a corner, or what to focus on — I'll answer from the telemetry and flag the corners on the graph."
        : "I'm your race engineer. This is a live demo running on a simulated session, so instead of the backend model I answer straight from the deterministic corner findings — the same ground truth the real coach is checked against. Ask me where you're losing time, how to fix your worst corner, or what to focus on, and I'll flag it on the graph. I'll also tell you honestly when the telemetry can't answer.");

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
