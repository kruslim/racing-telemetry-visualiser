/* ============================================================
   i2menus.js — makes the menu bar real.

   File · Edit · View · Channels · Maths · Tools · Help now open
   working dropdowns that drive the same app state the worksheets
   read: hide/show graph channels, build derived math channels,
   swap main/ref, pick the theoretical-best reference, and export
   a printable debrief. No façade chrome.
   ============================================================ */
(function (global) {
  'use strict';
  const L = global.Laps, G = global.I2Graphs, App = global.I2App, Coach = global.Coach;
  const $ = s => document.querySelector(s);
  const st = App.st;

  // ---------- derived (math) channel library ----------
  const MATHS = [
    { id: 'combg', title: 'Combined G', unit: 'g', w: 1.05, min: 0, max: 3.2,
      ch: [{ key: 'combg', color: '#ff6ec7', label: '|G|', conv: v => v, dec: 2 }],
      _fn: (d, i) => Math.hypot(d.latG[i], d.lonG[i]),
      desc: 'Total grip vector √(lat²+lon²)' },
    { id: 'trailo', title: 'Trail-Brake Overlap', unit: '%', w: 0.95, min: 0, max: 100,
      ch: [{ key: 'trailo', color: '#ff9234', label: 'Brake×Steer', conv: v => v * 100, dec: 0 }],
      _fn: (d, i) => d.brk[i] * Math.min(1, Math.abs(d.steer[i]) / 0.4),
      desc: 'Brake pressure while the wheel is turned' },
    { id: 'balance', title: 'Balance · US ◂▸ OS', unit: '', w: 1.0, min: -1, max: 1, center: true,
      ch: [{ key: 'balance', color: '#7ee787', label: 'Rotation', conv: v => v, dec: 2 }],
      _fn: (d, i) => (Math.abs(d.latG[i]) / 2.6) - Math.abs(d.steer[i]),
      desc: 'Grip realised vs steering asked — <0 understeer, >0 rotation' }
  ];

  // ---------- menu definitions (rebuilt each open so checks stay live) ----------
  function fastestRealIdx() {
    let bi = 0, bt = Infinity;
    L.stint.forEach((l, i) => { if (!l.synthetic && l.time < bt) { bt = l.time; bi = i; } });
    return bi;
  }
  function swapMainRef() {
    if (L.stint[st.refIdx].synthetic) { flash('Theoretical lap can only be a reference'); return; }
    const a = st.mainIdx, b = st.refIdx;
    st.mainIdx = b; st.refIdx = a;
    App.buildLapList(); App.renderAll();
  }

  const DEFS = {
    File: () => [
      { label: 'Export Debrief…', sub: 'PDF-ready session report', act: exportDebrief },
      { label: 'Print Current Worksheet', act: () => window.print() },
      { sep: 1 },
      { label: 'Open Live Pitwall', sub: 'switch to the trackside console', act: () => location.href = 'Pitwall.html' }
    ],
    Edit: () => [
      { label: 'Swap Main ⇄ Reference', act: swapMainRef },
      { label: 'Reset Zoom — Fit Lap', act: fitLap },
      { sep: 1 },
      { label: 'Jump to Biggest Time Loss', sub: 'cursor → steepest Δt slope', act: jumpWorst }
    ],
    View: () => ([
      ['timedist', 'Time / Distance'], ['coach', 'Coach'], ['track', 'Track Report'],
      ['histogram', 'Histogram'], ['susp', 'Suspension']
    ].map(([id, label]) => ({
      label, radio: st.ws === id, act: () => App.setWorksheet(id)
    })).concat([
      { sep: 1 },
      { label: 'Fit Lap (reset zoom)', act: fitLap }
    ])),
    Channels: () => G.LANES.map(ln => ({
      label: ln.title,
      check: !G.isHidden(ln.id),
      disabled: ln.id === 'var',
      swatch: ln.ch && ln.ch[0] ? ln.ch[0].color : null,
      act: () => { G.setLaneHidden(ln.id, !G.isHidden(ln.id)); App.renderAll(); }
    })),
    Maths: () => MATHS.map(m => ({
      label: m.title, sub: m.desc, check: G.hasMath(m.id), swatch: m.ch[0].color,
      act: () => { G.hasMath(m.id) ? G.removeMathChannel(m.id) : G.addMathChannel(m); App.renderAll(); }
    })),
    Tools: () => [
      { label: 'Set Theoretical Best as Reference', sub: 'fastest mini-sectors stitched', act: () => { App.setRef(L.optIdx); } },
      { label: 'Set Fastest Lap as Reference', act: () => { App.setRef(fastestRealIdx()); } },
      { label: 'Swap Main ⇄ Reference', act: swapMainRef },
      { sep: 1 },
      { label: 'Clear all Math channels', disabled: !MATHS.some(m => G.hasMath(m.id)),
        act: () => { MATHS.forEach(m => G.removeMathChannel(m.id)); App.renderAll(); } }
    ],
    Help: () => [
      { label: 'Keyboard & Mouse', sub: 'scroll zoom · drag pan · ← → step', act: showShortcuts },
      { label: 'About Pitwall i2', act: showAbout }
    ]
  };

  function fitLap() { st.view.d0 = 0; st.view.d1 = L.LAP; App.renderAll(); }
  function jumpWorst() {
    const V = G.variance(st); let bi = 1, bm = -1e9;
    for (let i = 6; i < V.arr.length - 1; i++) { const s = V.arr[i + 1] - V.arr[i - 1]; if (s > bm) { bm = s; bi = i; } }
    App.setWorksheet('timedist');
    App.jumpTo(bi * L.STEP, 820);
  }

  // ---------- dropdown rendering ----------
  let openMenu = null;
  const layer = document.createElement('div');
  layer.className = 'menu-layer'; layer.style.display = 'none';
  document.body.appendChild(layer);

  function closeMenu() { openMenu = null; layer.style.display = 'none'; layer.innerHTML = ''; syncActive(); }
  function syncActive() {
    document.querySelectorAll('.menus .m').forEach(m => m.classList.toggle('open', m.dataset.menu === openMenu));
  }

  function openFor(name, anchor) {
    openMenu = name;
    const items = DEFS[name] ? DEFS[name]() : [];
    layer.innerHTML = `<div class="menu-pop"></div>`;
    const pop = layer.querySelector('.menu-pop');
    items.forEach(it => {
      if (it.sep) { const s = document.createElement('div'); s.className = 'menu-sep'; pop.appendChild(s); return; }
      const row = document.createElement('button');
      row.className = 'menu-item' + (it.disabled ? ' disabled' : '');
      const mark = it.check != null ? `<span class="mi-check ${it.check ? 'on' : ''}"></span>`
        : it.radio != null ? `<span class="mi-radio ${it.radio ? 'on' : ''}"></span>`
        : `<span class="mi-gap"></span>`;
      const sw = it.swatch ? `<span class="mi-sw" style="background:${it.swatch}"></span>` : '';
      row.innerHTML = `${mark}${sw}<span class="mi-body"><span class="mi-label">${it.label}</span>${it.sub ? `<span class="mi-sub">${it.sub}</span>` : ''}</span>`;
      if (!it.disabled) row.addEventListener('click', e => {
        e.stopPropagation();
        const keepOpen = it.check != null;      // checkbox menus stay open for multi-toggle
        it.act && it.act();
        if (keepOpen) { openFor(name, anchor); } else { closeMenu(); }
      });
      pop.appendChild(row);
    });
    const r = anchor.getBoundingClientRect();
    layer.style.display = 'block';
    pop.style.left = Math.round(r.left) + 'px';
    pop.style.top = Math.round(r.bottom + 2) + 'px';
    // clamp to viewport
    requestAnimationFrame(() => {
      const pr = pop.getBoundingClientRect();
      if (pr.right > window.innerWidth - 6) pop.style.left = Math.round(window.innerWidth - pr.width - 6) + 'px';
    });
    syncActive();
  }

  function init() {
    const items = document.querySelectorAll('.menus .m');
    items.forEach(m => {
      const name = m.textContent.trim();
      m.dataset.menu = name;
      if (!DEFS[name]) return;
      m.style.cursor = 'pointer';
      m.addEventListener('click', e => {
        e.stopPropagation();
        if (openMenu === name) closeMenu(); else openFor(name, m);
      });
      m.addEventListener('mouseenter', () => { if (openMenu && openMenu !== name) openFor(name, m); });
    });
    document.addEventListener('click', () => { if (openMenu) closeMenu(); });
    window.addEventListener('keydown', e => { if (e.key === 'Escape' && openMenu) closeMenu(); });
    window.addEventListener('resize', () => { if (openMenu) closeMenu(); });
  }

  // ---------- transient toast ----------
  let toastEl = null, toastT = 0;
  function flash(msg) {
    if (!toastEl) { toastEl = document.createElement('div'); toastEl.className = 'menu-toast'; document.body.appendChild(toastEl); }
    toastEl.textContent = msg; toastEl.classList.add('on');
    clearTimeout(toastT); toastT = setTimeout(() => toastEl.classList.remove('on'), 2200);
  }

  // ---------- modal popovers (Help) ----------
  function modal(title, bodyHTML) {
    const back = document.createElement('div'); back.className = 'menu-modal-back';
    back.innerHTML = `<div class="menu-modal"><div class="mm-head">${title}<button class="mm-x">✕</button></div><div class="mm-body">${bodyHTML}</div></div>`;
    document.body.appendChild(back);
    const close = () => back.remove();
    back.addEventListener('click', e => { if (e.target === back) close(); });
    back.querySelector('.mm-x').addEventListener('click', close);
  }
  function showShortcuts() {
    modal('Keyboard &amp; Mouse', `<table class="mm-keys">
      <tr><td><kbd>scroll</kbd></td><td>zoom the channel graph around the cursor</td></tr>
      <tr><td><kbd>drag</kbd> overview</td><td>pan / resize the zoom window</td></tr>
      <tr><td><kbd>dbl-click</kbd> overview</td><td>fit the full lap</td></tr>
      <tr><td><kbd>←</kbd> <kbd>→</kbd></td><td>step the cursor (hold <kbd>Shift</kbd> = ×5)</td></tr>
      <tr><td><kbd>click</kbd> a lap</td><td>set it as Main · the <b>M</b>/<b>R</b> chips pick Main/Reference</td></tr>
      <tr><td><kbd>right-click</kbd> a lap</td><td>set it as Reference</td></tr>
    </table>`);
  }
  function showAbout() {
    modal('About Pitwall i2', `<p class="mm-p"><b>Pitwall i2 v3.4</b> — post-session telemetry analysis.</p>
      <p class="mm-p">A GT3 endurance stint sampled uniformly by distance so any two laps overlay and a time-variance channel is computed between them. Every worksheet, the Coach agents and the math channels read one shared main-vs-reference selection.</p>
      <p class="mm-p mm-dim">Simulated data · deterministic seed · 60 Hz logging.</p>`);
  }

  // ============================================================
  //  DEBRIEF EXPORT  — clean printable session report
  // ============================================================
  function exportDebrief() {
    const m = Coach ? Coach.getModel(st) : null;
    if (!m) { flash('Coach model unavailable'); return; }
    const fmtLap = App.fmtLap;
    const sgn = (v, d) => (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(d);
    const cf = m.chief;

    const priorities = cf.top3.map((t, i) => `
      <tr><td class="rk">${i + 1}</td><td class="cn">${t.label}</td><td>${t.why}</td>
      <td class="num lose">+${t.gain.toFixed(3)}s</td></tr>`).join('') ||
      `<tr><td colspan="4">Clean lap — no significant losses vs reference.</td></tr>`;

    const sectors = m.sectors.map(s => `
      <tr><td>${s.name}</td><td class="num">${s.main.toFixed(3)}</td><td class="num dim">${s.ref.toFixed(3)}</td>
      <td class="num ${s.delta > 0.001 ? 'lose' : s.delta < -0.001 ? 'gain' : ''}">${sgn(s.delta, 3)}</td></tr>`).join('');

    const kdefs = [['trail', 'Trail-brake %', 0], ['full', 'Avg throttle %', 0], ['smooth', 'Throttle smooth', 0], ['corr', 'Steer corrections', 0], ['coast', 'Coasting %', 1]];
    const kpis = kdefs.map(([k, lab, dc]) => `
      <tr><td>${lab}</td><td class="num">${m.kpis.main[k].toFixed(dc)}</td><td class="num dim">${m.kpis.ref[k].toFixed(dc)}</td></tr>`).join('');

    const corners = m.corners.map(c => `
      <tr><td class="cn">${c.label}</td><td>${c.type}</td><td class="num">${Math.round(c.minMain)}</td>
      <td class="num dim">${Math.round(c.minRef)}</td>
      <td class="num ${(c.minMain - c.minRef) > 0.5 ? 'gain' : (c.minMain - c.minRef) < -0.5 ? 'lose' : ''}">${sgn(c.minMain - c.minRef, 0)}</td>
      <td class="num ${c.netDt > 0.02 ? 'lose' : c.netDt < -0.02 ? 'gain' : ''}">${sgn(c.netDt, 3)}</td></tr>`).join('');

    const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>Debrief — L${m.main.lapNo} vs ${m.ref.synthetic ? 'OPTIMAL' : 'L' + m.ref.lapNo}</title>
    <style>
      @page{margin:16mm}
      *{box-sizing:border-box}
      body{font:13px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1d20;background:#fff;margin:0;padding:28px 34px;max-width:920px}
      h1{font-size:21px;margin:0 0 2px;letter-spacing:.01em}
      .sub{color:#6b7176;font-size:12.5px;margin-bottom:18px}
      .mono{font-family:"SF Mono",Menlo,Consolas,monospace}
      .band{display:flex;gap:26px;border:1px solid #e3e6e8;border-radius:10px;padding:16px 20px;margin:0 0 22px;align-items:center;background:#fafbfb}
      .big{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:34px;font-weight:700;color:#d9622b;line-height:1}
      .big small{display:block;font-size:11px;font-weight:600;color:#6b7176;letter-spacing:.06em;text-transform:uppercase;margin-top:5px}
      .kv{display:flex;flex-direction:column;gap:4px;font-size:12.5px}
      .kv b{font-family:"SF Mono",Menlo,Consolas,monospace}
      h2{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:#8b9296;margin:24px 0 8px;border-bottom:1px solid #e3e6e8;padding-bottom:5px}
      table{width:100%;border-collapse:collapse;font-size:12.5px}
      th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#9aa1a5;font-weight:600;padding:4px 8px;border-bottom:1px solid #e3e6e8}
      td{padding:5px 8px;border-bottom:1px solid #f0f2f3}
      td.num,th.num{text-align:right;font-family:"SF Mono",Menlo,Consolas,monospace}
      td.dim{color:#9aa1a5}
      td.cn{font-weight:700;color:#d9622b}
      td.rk{font-family:"SF Mono",Menlo,Consolas,monospace;color:#fff;background:#d9622b;width:22px;text-align:center;border-radius:4px}
      .lose{color:#c8362b}.gain{color:#1f8a4c}
      .foot{margin-top:26px;color:#9aa1a5;font-size:11px;border-top:1px solid #e3e6e8;padding-top:10px;display:flex;justify-content:space-between}
      .pbtn{position:fixed;top:14px;right:16px;background:#d9622b;color:#fff;border:0;border-radius:7px;padding:9px 16px;font-size:13px;font-weight:600;cursor:pointer}
      @media print{.pbtn{display:none}}
    </style></head><body>
      <button class="pbtn" onclick="window.print()">Print / Save PDF</button>
      <h1>Session Debrief</h1>
      <div class="sub">Autodrome Nazionale · Grand Prix 4.10 km · GT3 #07 · R. LIM · 04 Jun 2026 · Outing 2</div>
      <div class="band">
        <div class="big">+${cf.lost.toFixed(2)}<small>time available</small></div>
        <div class="kv">
          <span>Main lap <b>L${m.main.lapNo}</b> · <b>${fmtLap(m.main.time)}</b></span>
          <span>Reference <b>${m.ref.synthetic ? 'OPTIMAL' : 'L' + m.ref.lapNo}</b> · <b>${fmtLap(m.ref.time)}</b></span>
        </div>
        <div class="kv">
          <span>Net lap Δ <b class="${cf.netLap > 0 ? 'lose' : 'gain'}">${sgn(cf.netLap, 3)}</b></span>
          <span>Corners losing <b>${cf.losingN} / ${cf.total}</b></span>
        </div>
      </div>

      <h2>Priorities</h2>
      <table><thead><tr><th class="num" style="text-align:center">#</th><th>Corner</th><th>Why</th><th class="num">Time</th></tr></thead><tbody>${priorities}</tbody></table>

      <h2>Sector Comparison</h2>
      <table><thead><tr><th>Sector</th><th class="num">Main</th><th class="num">Ref</th><th class="num">Δ</th></tr></thead><tbody>${sectors}</tbody></table>

      <h2>Driver KPIs</h2>
      <table><thead><tr><th>Metric</th><th class="num">Main</th><th class="num">Ref</th></tr></thead><tbody>${kpis}</tbody></table>

      <h2>Corner by Corner</h2>
      <table><thead><tr><th>Corner</th><th>Type</th><th class="num">Min</th><th class="num">Ref</th><th class="num">Δ km/h</th><th class="num">Δ time</th></tr></thead><tbody>${corners}</tbody></table>

      <div class="foot"><span>Pitwall i2 · post-session analysis</span><span>Generated ${new Date().toLocaleString()}</span></div>
    </body></html>`;

    try {
      const w = window.open('', '_blank');
      if (w) { w.document.open(); w.document.write(html); w.document.close(); }
      else { const url = URL.createObjectURL(new Blob([html], { type: 'text/html' })); window.open(url, '_blank'); }
      flash('Debrief opened — use Print / Save PDF');
    } catch (e) { flash('Pop-up blocked — allow pop-ups to export'); }
  }

  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);
})(window);
