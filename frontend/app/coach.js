/* ============================================================
   coach.js — Agentic coaching layer for Pitwall i2.

   A roster of specialised agents (Brake, Throttle, Speed/Line,
   Gearing, Grip, Sector, Consistency) each read the SAME
   main-vs-reference selection the worksheets use and emit
   findings pinned to a distance. A Turn-by-Turn agent threads
   them into a corner-by-corner walkthrough; a Chief Engineer
   synthesises the top priorities and total time available.

   All findings are clickable -> jump the i2 cursor + zoom.
   Markers also overlay the Time/Distance graph and track maps.

   Pure analysis + DOM; i2app owns state, the cursor and zoom.
   ============================================================ */
(function (global) {
  'use strict';
  const L = global.Laps, PS = global.PitSim, G = global.I2Graphs;
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const di = d => clamp(Math.round(d / L.STEP), 0, L.N - 1);
  const $ = s => document.querySelector(s);

  // ---------- agent registry (clinical identity: colour + 3-letter code) ----------
  const AGENTS = {
    txt: { name: 'Turn-by-Turn', code: 'TXT', color: '#cfd4d7', watch: 'Corner-by-corner walkthrough' },
    sector: { name: 'Sector', code: 'SEC', color: '#34d2e3', watch: 'S1 · S2 · S3 split times' },
    brake: { name: 'Brake', code: 'BRK', color: '#ff463a', watch: 'Brake point · trail · lock-up' },
    speed: { name: 'Speed / Line', code: 'SPD', color: '#ffd23b', watch: 'Min corner speed · apex' },
    throttle: { name: 'Throttle', code: 'THR', color: '#39d46a', watch: 'Application · full-throttle %' },
    gear: { name: 'Gearing', code: 'GER', color: '#4db5ff', watch: 'Gear selection · shift point' },
    grip: { name: 'Grip / Tyres', code: 'GRP', color: '#b388ff', watch: 'Lateral grip · slip' },
    consistency: { name: 'Consistency', code: 'CNS', color: '#ffae3b', watch: 'Lap-to-lap spread' },
  };
  const ORDER = ['txt', 'sector', 'brake', 'speed', 'throttle', 'gear', 'grip', 'consistency'];

  // ---------- channel helpers ----------
  function localMin(arr, c, win) {
    const i0 = Math.max(0, di(c - win)), i1 = Math.min(L.N - 1, di(c + win));
    let m = Infinity, mi = i0;
    for (let i = i0; i <= i1; i++) if (arr[i] < m) { m = arr[i]; mi = i; }
    return { v: m, i: mi, d: mi * L.STEP };
  }
  function peakAbs(arr, c, win) {
    const i0 = Math.max(0, di(c - win)), i1 = Math.min(L.N - 1, di(c + win));
    let m = 0; for (let i = i0; i <= i1; i++) { const a = Math.abs(arr[i]); if (a > m) m = a; }
    return m;
  }
  function brakePoint(lap, c) {            // first distance before apex where brake bites
    const i0 = di(c - 270), i1 = di(c);
    for (let i = i0; i <= i1; i++) if (lap.data.brk[i] > 0.09) return i * L.STEP;
    return null;
  }
  function throttleOn(lap, c) {            // first distance after apex at full throttle
    const i0 = di(c), i1 = di(c + 360);
    for (let i = i0; i <= i1; i++) if (lap.data.thr[i] > 0.9) return i * L.STEP;
    return null;
  }
  const gearAt = (lap, d) => Math.round(lap.data.gear[di(d)]);
  const cornerType = k => k < 95 ? 'Hairpin' : k < 140 ? 'Slow corner' : k < 200 ? 'Medium corner' : 'Fast corner';
  const sectorOf = d => { const f = d / L.LAP; return f < L.SECTORS[0] ? 1 : f < L.SECTORS[1] ? 2 : 3; };

  // ---------- coaching KPIs: turn raw channels into chase-able driver scores ----------
  function lapKPIs(lap) {
    const d = lap.data;
    let brkN = 0, trail = 0, coast = 0, fullT = 0, tv = 0, reversals = 0, lastSign = 0;
    for (let i = 0; i < L.N; i++) {
      const br = d.brk[i], th = d.thr[i], steer = d.steer[i];
      if (br > 0.05) { brkN++; if (Math.abs(steer) > 0.16) trail++; }   // braking while turning
      if (th < 0.05 && br < 0.05) coast++;                              // neither pedal
      fullT += th;                                                     // mean throttle application
      if (i > 0) tv += Math.abs(th - d.thr[i - 1]);                     // throttle total variation
      const sgn = steer > 0.05 ? 1 : steer < -0.05 ? -1 : 0;            // steering reversals = corrections
      if (sgn !== 0) { if (lastSign !== 0 && sgn !== lastSign) reversals++; lastSign = sgn; }
    }
    return {
      trail: brkN ? trail / brkN * 100 : 0,
      coast: coast / L.N * 100,
      full: fullT / L.N * 100,
      smooth: clamp(100 - tv * 0.95, 0, 100),
      corr: reversals
    };
  }
  // label, unit, key, dir ('hi'|'lo'|'neutral'), dec
  const KPI_DEFS = [
    { key: 'trail', label: 'Trail-brake', unit: '%', dir: 'neutral', dec: 0, hint: 'time braking while turning' },
    { key: 'full', label: 'Avg throttle', unit: '%', dir: 'hi', dec: 0, hint: 'mean throttle application' },
    { key: 'smooth', label: 'Throttle smooth', unit: '', dir: 'hi', dec: 0, hint: 'inverse of pedal jitter' },
    { key: 'corr', label: 'Steer corrections', unit: '', dir: 'lo', dec: 0, hint: 'mid-corner reversals' },
    { key: 'coast', label: 'Coasting', unit: '%', dir: 'lo', dec: 1, hint: 'neither pedal applied' }
  ];

  // ============================================================
  //  AGENT ANALYSIS  (build the full model from main vs ref)
  // ============================================================
  function build(st) {
    const main = L.stint[st.mainIdx], ref = L.stint[st.refIdx];
    const V = G.variance(st);

    const corners = L.CORNERS.map((c, idx) => {
      const entry = di(c.d - 150), exit = Math.min(L.N - 1, di(c.d + 170));
      const netDt = V.arr[exit] - V.arr[entry];                 // + = main lost time here
      const mm = localMin(main.data.speed, c.d, 90), rm = localMin(ref.data.speed, c.d, 90);
      const minMain = mm.v * 3.6, minRef = rm.v * 3.6, apexD = mm.d;
      const diags = [];

      // SPEED / LINE — minimum corner speed
      const dk = minMain - minRef;
      if (Math.abs(dk) >= 1.5) diags.push({ agent: 'speed', d: apexD, mag: Math.abs(dk), good: dk > 0,
        text: `Min speed ${dk >= 0 ? '+' : '\u2212'}${Math.abs(dk).toFixed(0)} km/h vs reference` });

      // BRAKE — brake point + lock-up
      const bpM = brakePoint(main, c.d), bpR = brakePoint(ref, c.d);
      if (bpM != null && bpR != null) { const bd = bpM - bpR;
        if (Math.abs(bd) >= 7) diags.push({ agent: 'brake', d: bpM, mag: Math.abs(bd) / 3, good: false,
          text: `Brake point ${bd < 0 ? Math.abs(bd).toFixed(0) + ' m early' : bd.toFixed(0) + ' m late'}` }); }
      const lockDip = minRef - minMain;
      if (lockDip >= 5 && peakAbs(main.data.brk, c.d, 80) > 0.5)
        diags.push({ agent: 'brake', d: apexD, mag: lockDip / 1.6, good: false, lockup: true,
          text: `Lock-up \u2014 ${lockDip.toFixed(0)} km/h scrubbed at entry` });

      // THROTTLE — application point
      const toM = throttleOn(main, c.d), toR = throttleOn(ref, c.d);
      if (toM != null && toR != null) { const td = toM - toR;
        if (Math.abs(td) >= 10) diags.push({ agent: 'throttle', d: toM, mag: Math.abs(td) / 3, good: td < 0,
          text: `Full throttle ${td > 0 ? Math.abs(td).toFixed(0) + ' m later' : Math.abs(td).toFixed(0) + ' m earlier'} than ref` }); }

      // GEARING — gear used at apex
      const gM = gearAt(main, apexD), gR = gearAt(ref, apexD);
      if (gM !== gR) diags.push({ agent: 'gear', d: apexD, mag: 1.3, good: false,
        text: `Apex in gear ${gM} \u2014 reference used ${gR}` });

      // GRIP / TYRES — lateral G held
      const lgM = peakAbs(main.data.latG, c.d, 90), lgR = peakAbs(ref.data.latG, c.d, 90);
      if (lgR - lgM >= 0.12) diags.push({ agent: 'grip', d: apexD, mag: (lgR - lgM) * 3, good: false,
        text: `Peak ${lgM.toFixed(2)}g lateral \u2014 ref held ${lgR.toFixed(2)}g` });

      // apportion the corner's lost time across the loss diagnostics
      const lost = Math.max(0, netDt);
      const totMag = diags.filter(x => !x.good).reduce((s, x) => s + x.mag, 0) || 1;
      diags.forEach(x => { x.t = x.good ? 0 : lost * (x.mag / totMag); });

      return { idx, label: c.label, d: c.d, apexD, entryD: c.d - 150, exitD: c.d + 170,
        netDt, minMain, minRef, type: cornerType(minMain), sector: sectorOf(c.d), diags };
    });

    // CONSISTENCY — min-speed spread across the whole stint, pin to the 2 worst corners
    const realLaps = L.realLaps();
    const spread = L.CORNERS.map(c => {
      let lo = Infinity, hi = -Infinity;
      for (const lap of realLaps) { const v = localMin(lap.data.speed, c.d, 90).v * 3.6; if (v < lo) lo = v; if (v > hi) hi = v; }
      return hi - lo;
    });
    spread.map((v, i) => [v, i]).sort((a, b) => b[0] - a[0]).forEach(([sp, ci], k) => {
      if (k < 2 && sp >= 6) corners[ci].diags.push({ agent: 'consistency', d: corners[ci].apexD, mag: sp / 3,
        good: false, t: 0, variance: true, text: `\u00b1${sp.toFixed(0)} km/h min-speed spread over stint` });
    });

    // SECTOR — split deltas
    const secMid = [L.SECTORS[0] / 2, (L.SECTORS[0] + L.SECTORS[1]) / 2, (L.SECTORS[1] + 1) / 2];
    const sectors = [0, 1, 2].map(i => ({ i, name: 'S' + (i + 1), main: main.sectors[i], ref: ref.sectors[i],
      delta: main.sectors[i] - ref.sectors[i], d: secMid[i] * L.LAP }));

    // agent roster stats
    const stats = {}; ORDER.forEach(id => stats[id] = { count: 0, time: 0 });
    corners.forEach(c => c.diags.forEach(x => { const s = stats[x.agent]; if (!s) return; s.count++; if (x.t > 0) s.time += x.t; }));
    const losing = corners.filter(c => c.netDt > 0.02);
    stats.txt = { count: losing.length, time: losing.reduce((s, c) => s + c.netDt, 0) };
    stats.sector = { count: 3, time: sectors.reduce((s, x) => s + Math.max(0, x.delta), 0) };

    // CHIEF synthesis
    const lost = corners.reduce((s, c) => s + Math.max(0, c.netDt), 0);
    const netLap = V.arr[L.N - 1];
    const top3 = losing.slice().sort((a, b) => b.netDt - a.netDt).slice(0, 3)
      .map(c => ({ idx: c.idx, label: c.label, gain: c.netDt, why: topReason(c) }));
    const times = realLaps.map(l => l.time);
    const tSpread = Math.max(...times) - Math.min(...times);

    return { main, ref, V, corners, sectors, stats,
      kpis: { main: lapKPIs(main), ref: lapKPIs(ref) },
      chief: { lost, netLap, losingN: losing.length, total: corners.length, top3, tSpread } };
  }

  function topReason(c) {
    const d = c.diags.filter(x => !x.variance).sort((a, b) => b.t - a.t)[0];
    return d ? d.text : (c.minMain < c.minRef ? 'Low minimum speed' : 'Lost on line');
  }

  // ---------- model cache (rebuild only when lap selection changes) ----------
  let cache = { key: '', m: null }, lastSt = null;
  function model(st) {
    st = st || lastSt; if (!st) return null;
    const key = st.mainIdx + '_' + st.refIdx;
    if (cache.key !== key) cache = { key, m: build(st) };
    return cache.m;
  }

  // ---------- selection / filter ----------
  let SEL = null, FILT = null;
  const fmtT = t => (t >= 0 ? '+' : '\u2212') + Math.abs(t).toFixed(3);
  const fmtT2 = t => (t >= 0 ? '+' : '\u2212') + Math.abs(t).toFixed(2);
  function fmtLap(t) { if (!isFinite(t)) return '\u2014'; const m = Math.floor(t / 60); return m + ':' + (t - m * 60).toFixed(2).padStart(5, '0'); }
  const matches = (c, f) => !f || f === 'txt' || f === 'sector' || c.diags.some(d => d.agent === f);

  function selectCorner(idx) { SEL = idx; const c = model().corners[idx]; global.I2App.jumpTo(c.apexD, 820); }
  function selectFinding(d, idx) { SEL = idx; global.I2App.jumpTo(d, 480); }
  function openInGraph(idx) { SEL = idx; const c = model().corners[idx]; global.I2App.setWorksheet('timedist'); global.I2App.jumpTo(c.apexD, 820); }
  function setFilter(id) { FILT = (FILT === id) ? null : id; global.I2App.renderAll(); }

  // ============================================================
  //  RENDER — Coach worksheet
  // ============================================================
  function render(st) {
    lastSt = st;
    const m = model(st);
    renderChief(m);
    renderWalk(m);
    renderAgents(m);
    drawMap($('#cx-map'), st, m);
    updateMapFoot(st, m);
  }

  function renderKPICards(m) {
    return KPI_DEFS.map(k => {
      const mv = m.kpis.main[k.key], rv = m.kpis.ref[k.key];
      const d = mv - rv;
      const eps = k.dec ? 0.05 : 0.5;
      let cls = '';
      if (Math.abs(d) >= eps && k.dir !== 'neutral') cls = ((k.dir === 'hi') ? d > 0 : d < 0) ? 'good' : 'bad';
      const dTxt = Math.abs(d) < eps ? '±0' : (d >= 0 ? '+' : '−') + Math.abs(d).toFixed(k.dec);
      return `<div class="kpi" title="${k.hint}">
        <div class="kpi-lab">${k.label}</div>
        <div class="kpi-val ${cls}">${mv.toFixed(k.dec)}<span class="u">${k.unit}</span></div>
        <div class="kpi-cmp"><span class="r">ref ${rv.toFixed(k.dec)}</span><span class="d ${cls}">${dTxt}</span></div>
      </div>`;
    }).join('');
  }

  function renderChief(m) {
    const cf = m.chief;
    const tag = l => l.synthetic ? 'OPT' : 'L' + l.lapNo;
    $('#cx-meta').textContent = `main ${tag(m.main)} vs ref ${tag(m.ref)}`;
    const acts = cf.top3.map((t, i) => `
      <button class="cf-act" data-corner="${t.idx}">
        <span class="rank">${i + 1}</span>
        <span class="cn">${t.label}</span>
        <span class="why">${t.why}</span>
        <span class="gain">+${t.gain.toFixed(3)}s</span>
      </button>`).join('');
    $('#cx-chief').innerHTML = `
      <div class="cf-head">
        <div class="cf-big">
          <div class="cf-num">+${cf.lost.toFixed(2)}<span class="s">s</span></div>
          <div class="cf-cap">time available vs reference</div>
        </div>
        <div class="cf-sub">
          <div class="cf-kv"><span class="k">Net lap \u0394</span><span class="v ${cf.netLap > 0 ? 'lose' : 'gain'}">${fmtT(cf.netLap)}</span></div>
          <div class="cf-kv"><span class="k">Corners losing</span><span class="v">${cf.losingN} / ${cf.total}</span></div>
          <div class="cf-kv"><span class="k">Main lap</span><span class="v">${tag(m.main)} · ${fmtLap(m.main.time)}</span></div>
          <div class="cf-kv"><span class="k">Reference</span><span class="v">${tag(m.ref)} · ${fmtLap(m.ref.time)}</span></div>
        </div>
      </div>
      <div class="cf-actions">
        <span class="cf-lbl">Priorities</span>
        ${acts || '<span class="wt-empty" style="margin:0">Clean lap \u2014 no significant losses vs reference.</span>'}
      </div>
      <div class="cf-kpis">
        <span class="cf-lbl">Driver KPIs <em>main vs ref</em></span>
        <div class="kpi-strip">${renderKPICards(m)}</div>
      </div>`;
    $('#cx-chief').querySelectorAll('.cf-act').forEach(b =>
      b.addEventListener('click', () => selectCorner(+b.dataset.corner)));
  }

  function renderWalk(m) {
    const SPDMAX = 300;
    $('#cx-wt-meta').textContent = FILT ? `filtered \u00b7 ${AGENTS[FILT].name}` : `${m.corners.length} corners \u00b7 by distance`;
    const rows = m.corners.map(c => {
      const sign = c.netDt > 0.02 ? 'lose' : c.netDt < -0.02 ? 'gain' : 'flat';
      const dk = c.minMain - c.minRef;
      const dCls = Math.abs(dk) < 0.5 ? '' : dk > 0 ? 'gain' : 'lose';
      const dim = !matches(c, FILT) ? 'dim' : '';
      const chips = c.diags.length ? c.diags.map(x => {
        const a = AGENTS[x.agent];
        const cdim = (FILT && FILT !== 'txt' && FILT !== 'sector' && x.agent !== FILT) ? 'dim' : '';
        const tt = x.variance ? `<span class="t var">var</span>` : (x.t > 0.004 ? `<span class="t">+${x.t.toFixed(2)}</span>` : '');
        return `<button class="wt-chip ${x.lockup ? 'lockup' : ''} ${cdim}" data-d="${x.d.toFixed(0)}" data-corner="${c.idx}">
          <span class="ag" style="background:${a.color}">${a.code}</span>
          <span class="tx">${x.text}</span>${tt}
        </button>`;
      }).join('') : `<div class="wt-empty">No deficit vs reference \u2014 on the pace here.</div>`;
      return `
      <div class="wt-row ${sign} ${SEL === c.idx ? 'sel' : ''} ${dim}" data-corner="${c.idx}">
        <div class="wt-node"><span class="wt-dot"></span></div>
        <div class="wt-card" data-corner="${c.idx}">
          <div class="wt-top">
            <div class="wt-id">
              <span class="wt-label">${c.label}</span>
              <span class="wt-type">${c.type}</span>
              <span class="wt-sec">S${c.sector}</span>
              <span class="wt-dist">${Math.round(c.d)} m</span>
            </div>
            <button class="wt-graph" data-graph="${c.idx}" title="Open this corner in the Time/Distance graph">\u21d2 graph</button>
            <span class="wt-delta ${sign}">${c.netDt >= 0 ? '+' : '\u2212'}${Math.abs(c.netDt).toFixed(3)}<span class="u">s</span></span>
          </div>
          <div class="wt-speed">
            <span class="lab">Min speed</span>
            <span class="wt-bar">
              <span class="ref" style="width:${clamp(c.minRef / SPDMAX * 100, 0, 100).toFixed(1)}%"></span>
              <span class="main" style="width:${clamp(c.minMain / SPDMAX * 100, 0, 100).toFixed(1)}%"></span>
            </span>
            <span class="wt-vals"><b>${Math.round(c.minMain)}</b> / <span class="r">${Math.round(c.minRef)}</span> km/h
              ${dCls ? `<span class="d ${dCls}">${dk >= 0 ? '+' : '\u2212'}${Math.abs(dk).toFixed(0)}</span>` : ''}</span>
          </div>
          <div class="wt-chips">${chips}</div>
        </div>
      </div>`;
    }).join('');
    const host = $('#cx-walk'); host.innerHTML = rows;
    host.querySelectorAll('.wt-card').forEach(card =>
      card.addEventListener('click', () => selectCorner(+card.dataset.corner)));
    host.querySelectorAll('.wt-graph').forEach(b =>
      b.addEventListener('click', e => { e.stopPropagation(); openInGraph(+b.dataset.graph); }));
    host.querySelectorAll('.wt-chip').forEach(b =>
      b.addEventListener('click', e => { e.stopPropagation(); selectFinding(+b.dataset.d, +b.dataset.corner); }));
  }

  function renderAgents(m) {
    $('#cx-ag-meta').textContent = `${ORDER.length} active`;
    const all = `<button class="ag-all ${FILT ? '' : 'on'}" data-filter=""><span>All agents</span><span class="cnt">show every finding</span></button>`;
    const rows = ORDER.map(id => {
      const a = AGENTS[id], s = m.stats[id] || { count: 0, time: 0 };
      const on = FILT === id ? 'on' : '';
      const muted = FILT && FILT !== id ? 'muted' : '';
      const off = (s.count === 0) ? 'off' : '';
      return `
      <button class="ag-row ${on} ${muted} ${off}" data-filter="${id}" ${s.count === 0 ? 'data-empty="1"' : ''}>
        <span class="ag-code" style="background:${a.color}">${a.code}</span>
        <span class="ag-body"><span class="ag-name">${a.name}</span><span class="ag-watch">${a.watch}</span></span>
        <span class="ag-stat">
          <span class="n ${s.count ? '' : 'zero'}">${s.count}</span>
          <span class="tm ${s.time > 0.004 ? 'has' : ''}">${s.time > 0.004 ? '+' + s.time.toFixed(2) + 's' : '\u2014'}</span>
        </span>
      </button>`;
    }).join('');
    const host = $('#cx-agents'); host.innerHTML = all + rows;
    host.querySelectorAll('[data-filter]').forEach(b => {
      if (b.dataset.empty) return;            // zero-finding agents are not filterable
      b.addEventListener('click', () => setFilter(b.dataset.filter || null));
    });
  }

  // ============================================================
  //  FINDINGS MAP  (coach worksheet)
  // ============================================================
  function setupCanvas(cv) {
    const dpr = Math.min(2, global.devicePixelRatio || 1);
    const w = cv.offsetWidth || cv.clientWidth, h = cv.offsetHeight || cv.clientHeight;
    if (!w || !h) return null;
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cv._w = w; cv._h = h;
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }
  function projector(W, H, pad) {
    const T = PS.TRACK; let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    for (const p of T.raw) { mnx = Math.min(mnx, p[0]); mny = Math.min(mny, p[1]); mxx = Math.max(mxx, p[0]); mxy = Math.max(mxy, p[1]); }
    const sc = Math.min((W - pad * 2) / (mxx - mnx), (H - pad * 2) / (mxy - mny));
    const ox = pad + (W - pad * 2 - (mxx - mnx) * sc) / 2, oy = pad + (H - pad * 2 - (mxy - mny) * sc) / 2;
    return p => [ox + (p[0] - mnx) * sc, oy + (p[1] - mny) * sc];
  }

  let mapHits = [];
  function drawMap(cv, st, m) {
    if (!cv) return;
    const ctx = setupCanvas(cv); if (!ctx) return;
    const W = cv._w, H = cv._h; if (W < 2) return;
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = '#070809'; ctx.fillRect(0, 0, W, H);
    const T = PS.TRACK, tx = projector(W, H, 34);
    // base ribbon
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.strokeStyle = '#1c2125'; ctx.lineWidth = 13;
    ctx.beginPath(); T.raw.forEach((p, i) => { const q = tx(p); i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]); }); ctx.closePath(); ctx.stroke();
    ctx.strokeStyle = '#0a0c0e'; ctx.lineWidth = 9; ctx.stroke();
    // sector splits
    for (const f of L.SECTORS) { const p = tx(PS.posAt(f * L.LAP)); ctx.fillStyle = 'rgba(120,150,170,.5)'; ctx.beginPath(); ctx.arc(p[0], p[1], 3, 0, 7); ctx.fill(); }
    const sf = tx(T.raw[0]); ctx.fillStyle = '#fff'; ctx.fillRect(sf[0] - 1, sf[1] - 7, 2, 14);

    // corner markers (size = magnitude, colour = sign)
    mapHits = [];
    ctx.font = '700 10px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const c of m.corners) {
      const p = tx(PS.posAt(c.apexD));
      const r = 5 + clamp(Math.abs(c.netDt) * 26, 0, 9);
      const on = matches(c, FILT);
      const sign = c.netDt > 0.02 ? '255,70,58' : c.netDt < -0.02 ? '57,212,106' : '107,114,118';
      ctx.globalAlpha = on ? 1 : 0.25;
      ctx.fillStyle = `rgba(${sign},${SEL === c.idx ? 1 : .85})`;
      ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, 7); ctx.fill();
      ctx.strokeStyle = '#0a0c0e'; ctx.lineWidth = 1.5; ctx.stroke();
      if (SEL === c.idx) { ctx.strokeStyle = '#ff9234'; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(p[0], p[1], r + 3, 0, 7); ctx.stroke(); }
      ctx.globalAlpha = 1;
      ctx.fillStyle = on ? '#cfd4d7' : '#5b6266';
      ctx.fillText(c.label, p[0], p[1] - r - 8);
      mapHits.push({ x: p[0], y: p[1], r: r + 4, idx: c.idx });
    }
    // cursor
    const mp = tx(PS.posAt(st.cursorD));
    ctx.shadowColor = 'rgba(255,146,52,.9)'; ctx.shadowBlur = 10; ctx.fillStyle = '#ff9234';
    ctx.beginPath(); ctx.arc(mp[0], mp[1], 5, 0, 7); ctx.fill(); ctx.shadowBlur = 0;
    ctx.strokeStyle = '#0a0c0e'; ctx.lineWidth = 1.5; ctx.stroke();
  }
  function updateMapFoot(st, m) {
    const foot = $('#cx-mapfoot'); if (!foot) return;
    if (SEL != null) {
      const c = m.corners[SEL];
      foot.innerHTML = `<span>${c.label} \u00b7 ${c.type}</span><span class="r">${c.netDt >= 0 ? '+' : '\u2212'}${Math.abs(c.netDt).toFixed(3)}s \u00b7 <b>${Math.round(st.cursorD)} m</b></span>`;
    } else {
      foot.innerHTML = `<span>${m.corners.length} corners \u00b7 click a marker</span><span class="r">cursor <b>${Math.round(st.cursorD)} m</b></span>`;
    }
  }

  // bind map click once
  let mapBound = false;
  function bindMap() {
    if (mapBound) return; const cv = $('#cx-map'); if (!cv) return; mapBound = true;
    cv.addEventListener('click', e => {
      const r = cv.getBoundingClientRect();
      const x = (e.clientX - r.left) * (cv._w / r.width), y = (e.clientY - r.top) * (cv._h / r.height);
      let best = null, bd = 1e9;
      for (const h of mapHits) { const d = Math.hypot(h.x - x, h.y - y); if (d < h.r + 6 && d < bd) { bd = d; best = h; } }
      if (best) selectCorner(best.idx);
    });
  }

  // ============================================================
  //  OVERLAY MARKERS  (other worksheets)
  // ============================================================
  function overlayTimedist(st) {
    const m = model(st); if (!m) return;
    // graph flags
    const host = $('#cx-gmarkers');
    const lay = G.getLayout();
    if (host && lay && lay.xOf) {
      const d0 = lay.d0, d1 = lay.d1;
      host.innerHTML = m.corners.map(c => {
        if (c.d < d0 || c.d > d1) return '';
        const x = lay.xOf(c.d);
        const sign = c.netDt > 0.02 ? 'lose' : c.netDt < -0.02 ? 'gain' : 'flat';
        const on = matches(c, FILT) ? '' : 'dim';
        const sel = SEL === c.idx ? 'sel' : '';
        return `<button class="cx-flag ${sign} ${sel}" style="left:${x.toFixed(1)}px;${on ? 'opacity:.3' : ''}" data-corner="${c.idx}"
          title="${c.label} \u00b7 ${c.netDt >= 0 ? '+' : '\u2212'}${Math.abs(c.netDt).toFixed(3)}s">${c.label}</button>`;
      }).join('');
      host.querySelectorAll('.cx-flag').forEach(b => b.addEventListener('click', () => selectCorner(+b.dataset.corner)));
    }
    placeMapDots($('#cx-mapmarks'), '.mapwrap', 30, st, m, $('#mapcanvas'));
  }
  function overlayTrack(st) {
    const m = model(st);
    placeMapDots($('#cx-trmarks'), '#ws-track .mapwrap', 46, st, m, $('#tr-map'));
  }
  function placeMapDots(host, wrapSel, pad, st, m, cv) {
    if (!host || !m || !cv) return;
    const W = cv._w || cv.clientWidth, H = cv._h || cv.clientHeight; if (!W || W < 2) { host.innerHTML = ''; return; }
    const tx = projector(W, H, pad);
    host.innerHTML = m.corners.map(c => {
      const p = tx(PS.posAt(c.apexD));
      const sign = c.netDt > 0.02 ? 'lose' : c.netDt < -0.02 ? 'gain' : 'flat';
      const sz = (10 + clamp(Math.abs(c.netDt) * 30, 0, 12)).toFixed(0);
      const muted = matches(c, FILT) ? '' : 'muted';
      const sel = SEL === c.idx ? 'sel' : '';
      return `<button class="cx-dot ${sign} ${sel} ${muted}" data-corner="${c.idx}"
        style="left:${p[0].toFixed(1)}px;top:${p[1].toFixed(1)}px;width:${sz}px;height:${sz}px"
        title="${c.label} \u00b7 ${c.netDt >= 0 ? '+' : '\u2212'}${Math.abs(c.netDt).toFixed(3)}s">${SEL === c.idx ? `<span class="lbl">${c.label}</span>` : ''}</button>`;
    }).join('');
    host.querySelectorAll('.cx-dot').forEach(b => b.addEventListener('click', () => selectCorner(+b.dataset.corner)));
  }

  // ---------- boot hook ----------
  function init() { bindMap(); }
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);

  global.Coach = { render, overlayTimedist, overlayTrack, selectCorner, selectFinding, setFilter, openInGraph,
    getModel: (st) => model(st),
    get sel() { return SEL; }, get filter() { return FILT; } };
})(window);
