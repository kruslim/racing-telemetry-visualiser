/* ============================================================
   worksheets.js — the non-default i2 worksheets:
   Track Report, Histogram, Suspension. Each reads the same
   stint + main/ref selection the Time/Distance sheet uses.
   Pure rendering; i2app owns state, selection and the cursor.
   ============================================================ */
(function (global) {
  'use strict';
  const L = global.Laps, PS = global.PitSim, G = global.I2Graphs;
  const C = G.C;
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const lerp = (a, b, t) => a + (b - a) * t;
  const di = d => clamp(Math.round(d / L.STEP), 0, L.N - 1);
  const $ = s => document.querySelector(s);

  // ---------- canvas dpi setup ----------
  // offsetWidth/Height are layout px, immune to the #app transform — see i2graphs.js.
  function setup(cv) {
    const dpr = Math.min(2, global.devicePixelRatio || 1);
    const w = cv.offsetWidth || cv.clientWidth, h = cv.offsetHeight || cv.clientHeight;
    if (!w || !h) return null;
    if (cv._w !== w || cv._h !== h || cv._dpr !== dpr) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
      cv._w = w; cv._h = h; cv._dpr = dpr;
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }
  function fmt(v, dec) { return dec ? v.toFixed(dec) : Math.round(v).toString(); }
  function rgb(c) { return c; }

  // shared track->canvas projection (matches the sidebar map)
  function project(W, H, pad) {
    const T = PS.TRACK; let mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    for (const p of T.raw) { mnx = Math.min(mnx, p[0]); mny = Math.min(mny, p[1]); mxx = Math.max(mxx, p[0]); mxy = Math.max(mxy, p[1]); }
    const sc = Math.min((W - pad * 2) / (mxx - mnx), (H - pad * 2) / (mxy - mny));
    const ox = pad + (W - pad * 2 - (mxx - mnx) * sc) / 2;
    const oy = pad + (H - pad * 2 - (mxy - mny) * sc) / 2;
    return p => [ox + (p[0] - mnx) * sc, oy + (p[1] - mny) * sc];
  }

  // ============================================================
  //  TRACK REPORT
  // ============================================================
  function renderTrack(st) {
    drawVarianceMap($('#tr-map'), st);
    buildCornerTable(st);
    buildSectors(st);
  }

  function drawVarianceMap(cv, st) {
    if (!cv) return;
    const ctx = setup(cv); if (!ctx) return;
    const W = cv._w, H = cv._h;
    if (W < 2) return;
    const T = PS.TRACK, M = T.raw.length;
    const V = G.variance(st);
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
    const tx = project(W, H, 46);

    // base ribbon
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.strokeStyle = '#161b1f'; ctx.lineWidth = 22;
    ctx.beginPath(); T.raw.forEach((p, i) => { const q = tx(p); i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]); }); ctx.closePath(); ctx.stroke();
    ctx.strokeStyle = '#0a0d0f'; ctx.lineWidth = 17; ctx.stroke();

    // colour by LOCAL time-loss rate (slope of variance) along the lap
    ctx.lineWidth = 9;
    for (let i = 0; i < M; i++) {
      const a = tx(T.raw[i]), b = tx(T.raw[(i + 1) % M]);
      const d = (i / M) * L.LAP, k = di(d);
      const slope = (V.arr[Math.min(L.N - 1, k + 2)] - V.arr[Math.max(0, k - 2)]); // s over ~16 m
      const mag = clamp(Math.abs(slope) / 0.020, 0, 1);
      let col;
      if (mag < 0.12) col = 'rgba(110,120,126,.8)';
      else if (slope > 0) col = `rgba(255,70,58,${0.45 + mag * 0.55})`;   // losing
      else col = `rgba(57,212,106,${0.45 + mag * 0.55})`;                  // gaining
      ctx.strokeStyle = col;
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    }

    // corner markers + labels
    ctx.font = '600 11px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const c of L.CORNERS) {
      const p = tx(PS.posAt(c.d));
      ctx.fillStyle = '#0a0d0f'; ctx.beginPath(); ctx.arc(p[0], p[1], 9, 0, 7); ctx.fill();
      ctx.strokeStyle = 'rgba(255,255,255,.22)'; ctx.lineWidth = 1; ctx.stroke();
      ctx.fillStyle = '#c8cdd0'; ctx.fillText(c.label, p[0], p[1]);
    }
    // sector splits
    for (const f of L.SECTORS) { const p = tx(PS.posAt(f * L.LAP));
      ctx.fillStyle = '#cfd4d7'; ctx.fillRect(p[0] - 1.5, p[1] - 8, 3, 16); }
    // start/finish
    const sf = tx(T.raw[0]);
    ctx.fillStyle = '#fff'; ctx.fillRect(sf[0] - 1.5, sf[1] - 11, 3, 22);

    // cursor dot
    const mp = tx(PS.posAt(st.cursorD));
    ctx.shadowColor = 'rgba(255,146,52,.9)'; ctx.shadowBlur = 12; ctx.fillStyle = C.accent;
    ctx.beginPath(); ctx.arc(mp[0], mp[1], 7, 0, 7); ctx.fill(); ctx.shadowBlur = 0;
    ctx.strokeStyle = '#0a0d0f'; ctx.lineWidth = 2; ctx.stroke();

    // net lap delta caption
    const fin = V.arr[L.N - 1];
    ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
    ctx.fillStyle = '#9aa1a5'; ctx.font = '12px "Segoe UI",sans-serif';
    ctx.fillText('NET LAP \u0394', 14, H - 18);
    ctx.font = '700 19px "JetBrains Mono"';
    ctx.fillStyle = fin > 0 ? `rgb(${C.loseR},${C.loseG},${C.loseB})` : `rgb(${C.gainR},${C.gainG},${C.gainB})`;
    ctx.fillText((fin >= 0 ? '+' : '\u2212') + Math.abs(fin).toFixed(3) + ' s', 92, H - 18);
  }

  function localMin(arr, c, win) {
    const i0 = Math.max(0, di(c - win)), i1 = Math.min(L.N - 1, di(c + win));
    let m = Infinity, mi = i0;
    for (let i = i0; i <= i1; i++) if (arr[i] < m) { m = arr[i]; mi = i; }
    return { v: m, i: mi };
  }
  function cornerType(kmh) {
    return kmh < 95 ? 'Hairpin' : kmh < 140 ? 'Slow' : kmh < 200 ? 'Medium' : 'Fast';
  }

  function buildCornerTable(st) {
    const host = $('#tr-corners'); if (!host) return;
    const main = L.stint[st.mainIdx], ref = L.stint[st.refIdx];
    const V = G.variance(st);
    let html = `<div class="ct-head"><span>Corner</span><span>Type</span><span>Min km/h</span><span>Ref</span><span>\u0394</span><span>\u0394 time</span></div>`;
    for (const c of L.CORNERS) {
      const mm = localMin(main.data.speed, c.d, 90), rm = localMin(ref.data.speed, c.d, 90);
      const mk = mm.v * 3.6, rk = rm.v * 3.6, dk = mk - rk;
      const entry = di(c.d - 140), exit = di(c.d + 150);
      const dt = V.arr[exit] - V.arr[entry];
      const dCls = Math.abs(dk) < 0.5 ? '' : dk > 0 ? 'pos' : 'neg';
      const tCls = Math.abs(dt) < 0.003 ? '' : dt > 0 ? 'neg' : 'pos';
      html += `<div class="ct-row">
        <span class="cn"><i></i>${c.label}</span>
        <span class="ctype">${cornerType(mk)}</span>
        <span class="mn">${Math.round(mk)}</span>
        <span class="rf">${Math.round(rk)}</span>
        <span class="dv ${dCls}">${dk >= 0 ? '+' : '\u2212'}${Math.abs(dk).toFixed(1)}</span>
        <span class="tv ${tCls}">${dt >= 0 ? '+' : '\u2212'}${Math.abs(dt).toFixed(3)}</span>
      </div>`;
    }
    host.innerHTML = html;
  }

  function buildSectors(st) {
    const host = $('#tr-sectors'); if (!host) return;
    const main = L.stint[st.mainIdx], ref = L.stint[st.refIdx];
    const names = ['Sector 1', 'Sector 2', 'Sector 3'];
    const maxT = Math.max(...main.sectors, ...ref.sectors);
    let html = '';
    for (let i = 0; i < 3; i++) {
      const m = main.sectors[i], r = ref.sectors[i], d = m - r;
      const dCls = Math.abs(d) < 0.005 ? '' : d > 0 ? 'neg' : 'pos';
      html += `<div class="sbar">
        <div class="sbar-top">
          <span class="sname">${names[i]}</span>
          <span class="sval"><span class="d ${dCls}">${d >= 0 ? '+' : '−'}${Math.abs(d).toFixed(2)}</span></span>
        </div>
        <div class="srow">
          <span class="sk main">MAIN</span>
          <span class="strack"><i class="bar smain" style="width:${(m / maxT * 100).toFixed(1)}%"></i></span>
          <span class="snum"><b>${m.toFixed(2)}</b></span>
        </div>
        <div class="srow">
          <span class="sk ref">REF</span>
          <span class="strack"><i class="bar sref" style="width:${(r / maxT * 100).toFixed(1)}%"></i></span>
          <span class="snum">${r.toFixed(2)}</span>
        </div>
      </div>`;
    }
    host.innerHTML = html;
  }

  // ============================================================
  //  HISTOGRAM
  // ============================================================
  // count fraction of lap (by distance, uniform sampling) per bin
  function histify(arr, conv, lo, hi, nb) {
    const out = new Float32Array(nb); const span = hi - lo;
    for (let i = 0; i < L.N; i++) {
      const v = conv(arr[i]);
      let b = Math.floor((v - lo) / span * nb);
      b = clamp(b, 0, nb - 1); out[b]++;
    }
    for (let b = 0; b < nb; b++) out[b] = out[b] / L.N;
    return out;
  }

  const HCFG = [
    { cv: 'hg-speed', key: 'speed', conv: v => v * 3.6, lo: 60, hi: 290, nb: 23, color: C.speed, unit: '', tick: 50 },
    { cv: 'hg-rpm', key: 'rpm', conv: v => v, lo: 2800, hi: 8000, nb: 26, color: C.rpm, unit: '', tick: 1000, kfmt: true },
    { cv: 'hg-thr', key: 'thr', conv: v => v * 100, lo: 0, hi: 100, nb: 20, color: C.thr, unit: '', tick: 25 },
    { cv: 'hg-lat', key: 'latG', conv: v => v, lo: -3, hi: 3, nb: 24, color: C.lat, unit: '', tick: 1, dec: 1 },
  ];

  function renderHistogram(st) {
    const main = L.stint[st.mainIdx], ref = L.stint[st.refIdx];
    for (const cf of HCFG) drawHist($('#' + cf.cv), cf, main, ref);
    drawGearUsage($('#hg-gear'), main, ref);
    drawGG($('#hg-gg'), st, main);
    buildHistStats(st);
  }

  function drawHist(cv, cf, main, ref) {
    if (!cv) return;
    const ctx = setup(cv); if (!ctx) return; const W = cv._w, H = cv._h; if (W < 2) return;
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
    const PADL = 8, PADR = 8, PADT = 12, PADB = 22;
    const pw = W - PADL - PADR, ph = H - PADT - PADB;
    const mh = histify(main.data[cf.key], cf.conv, cf.lo, cf.hi, cf.nb);
    const rh = histify(ref.data[cf.key], cf.conv, cf.lo, cf.hi, cf.nb);
    let mx = 0.0001; for (let b = 0; b < cf.nb; b++) { mx = Math.max(mx, mh[b], rh[b]); }
    const bw = pw / cf.nb;
    const yOf = f => PADT + ph - (f / mx) * ph;
    // baseline
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PADL, PADT + ph + .5); ctx.lineTo(PADL + pw, PADT + ph + .5); ctx.stroke();
    // ref bars (dim outline)
    for (let b = 0; b < cf.nb; b++) {
      const x = PADL + b * bw, y = yOf(rh[b]);
      ctx.strokeStyle = 'rgba(150,160,166,.35)'; ctx.lineWidth = 1;
      ctx.strokeRect(x + 1, y, bw - 2, PADT + ph - y);
    }
    // main bars (filled)
    for (let b = 0; b < cf.nb; b++) {
      const x = PADL + b * bw, y = yOf(mh[b]);
      ctx.fillStyle = cf.color; ctx.globalAlpha = .82;
      ctx.fillRect(x + 1, y, bw - 2, PADT + ph - y);
    }
    ctx.globalAlpha = 1;
    // x ticks
    ctx.fillStyle = C.axisDim; ctx.font = '10.5px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let v = Math.ceil(cf.lo / cf.tick) * cf.tick; v <= cf.hi; v += cf.tick) {
      const x = PADL + (v - cf.lo) / (cf.hi - cf.lo) * pw;
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(x, PADT + ph); ctx.lineTo(x, PADT + ph + 3); ctx.stroke();
      const lbl = cf.kfmt ? (v / 1000) + 'k' : cf.dec ? v.toFixed(cf.dec) : v;
      ctx.fillText(lbl, x, PADT + ph + 5);
    }
  }

  function drawGearUsage(cv, main, ref) {
    if (!cv) return;
    const ctx = setup(cv); if (!ctx) return; const W = cv._w, H = cv._h; if (W < 2) return;
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
    const ng = 6;
    const usage = lap => { const u = new Float32Array(ng + 1); for (let i = 0; i < L.N; i++) u[clamp(Math.round(lap.data.gear[i]), 1, ng)]++; for (let g = 1; g <= ng; g++) u[g] /= L.N; return u; };
    const mu = usage(main), ru = usage(ref);
    const PADL = 14, PADR = 14, PADT = 14, PADB = 24;
    const pw = W - PADL - PADR, ph = H - PADT - PADB;
    let mx = 0.0001; for (let g = 1; g <= ng; g++) mx = Math.max(mx, mu[g], ru[g]);
    const slot = pw / ng;
    ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(PADL, PADT + ph + .5); ctx.lineTo(PADL + pw, PADT + ph + .5); ctx.stroke();
    for (let g = 1; g <= ng; g++) {
      const cx = PADL + (g - 1) * slot + slot / 2;
      const bw = slot * 0.34;
      // main
      const mhh = (mu[g] / mx) * ph, rhh = (ru[g] / mx) * ph;
      ctx.fillStyle = 'rgba(150,160,166,.32)';
      ctx.fillRect(cx - bw - 1, PADT + ph - rhh, bw, rhh);
      ctx.fillStyle = C.gear;
      ctx.fillRect(cx + 1, PADT + ph - mhh, bw, mhh);
      // value
      ctx.fillStyle = '#cfd4d7'; ctx.font = '600 11px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      ctx.fillText(Math.round(mu[g] * 100) + '%', cx + 1 + bw / 2, PADT + ph - mhh - 3);
      // gear label
      ctx.fillStyle = C.axis; ctx.font = '11px "JetBrains Mono"'; ctx.textBaseline = 'top';
      ctx.fillText('G' + g, cx, PADT + ph + 6);
    }
  }

  function drawGG(cv, st, main) {
    if (!cv) return;
    const ctx = setup(cv); if (!ctx) return; const W = cv._w, H = cv._h; if (W < 2) return;
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
    const cx = W / 2, cy = H / 2 + 4, R = Math.min(W, H) / 2 - 20;
    const G_MAX = 2.6;
    const sx = g => cx + (g / G_MAX) * R, sy = g => cy - (g / G_MAX) * R;
    // grid rings
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    for (let g = 1; g <= 2; g++) { ctx.beginPath(); ctx.arc(cx, cy, (g / G_MAX) * R, 0, 7); ctx.stroke(); }
    ctx.strokeStyle = '#222a2e';
    ctx.beginPath(); ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy); ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R); ctx.stroke();
    // labels
    ctx.fillStyle = C.axisDim; ctx.font = '10.5px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('1g', sx(1) + 8, cy - 8); ctx.fillText('2g', sx(2) + 8, cy - 8);
    ctx.textAlign = 'left'; ctx.fillText('ACCEL', cx + 4, cy - R + 6);
    ctx.fillText('BRAKE', cx + 4, cy + R - 6);
    // scatter
    for (let i = 0; i < L.N; i += 1) {
      const x = sx(main.data.latG[i]), y = sy(main.data.lonG[i]);
      ctx.fillStyle = 'rgba(52,210,227,.5)';
      ctx.fillRect(x - 1, y - 1, 2, 2);
    }
    // cursor point
    const k = di(st.cursorD);
    const px = sx(main.data.latG[k]), py = sy(main.data.lonG[k]);
    ctx.fillStyle = C.accent; ctx.shadowColor = 'rgba(255,146,52,.9)'; ctx.shadowBlur = 8;
    ctx.beginPath(); ctx.arc(px, py, 4, 0, 7); ctx.fill(); ctx.shadowBlur = 0;
    ctx.strokeStyle = '#0a0d0f'; ctx.lineWidth = 1.5; ctx.stroke();
  }

  function buildHistStats(st) {
    const host = $('#hg-stats'); if (!host) return;
    const main = L.stint[st.mainIdx], ref = L.stint[st.refIdx];
    const max = (lap, key, conv) => { let m = -Infinity; for (let i = 0; i < L.N; i++) m = Math.max(m, conv(lap.data[key][i])); return m; };
    const min = (lap, key, conv) => { let m = Infinity; for (let i = 0; i < L.N; i++) m = Math.min(m, conv(lap.data[key][i])); return m; };
    const avg = (lap, key, conv) => { let s = 0; for (let i = 0; i < L.N; i++) s += conv(lap.data[key][i]); return s / L.N; };
    const pct = (lap, test) => { let c = 0; for (let i = 0; i < L.N; i++) if (test(lap.data, i)) c++; return c / L.N * 100; };
    const rows = [
      { sec: 'Speed' },
      { lab: 'Top speed', unit: 'km/h', m: max(main, 'speed', v => v * 3.6), r: max(ref, 'speed', v => v * 3.6), dec: 0, hi: true },
      { lab: 'Average speed', unit: 'km/h', m: avg(main, 'speed', v => v * 3.6), r: avg(ref, 'speed', v => v * 3.6), dec: 0, hi: true },
      { lab: 'Min speed', unit: 'km/h', m: min(main, 'speed', v => v * 3.6), r: min(ref, 'speed', v => v * 3.6), dec: 0, hi: true },
      { sec: 'Pedals' },
      { lab: 'Full throttle', unit: '%', m: pct(main, (d, i) => d.thr[i] > 0.97), r: pct(ref, (d, i) => d.thr[i] > 0.97), dec: 0, hi: true },
      { lab: 'On brake', unit: '%', m: pct(main, (d, i) => d.brk[i] > 0.06), r: pct(ref, (d, i) => d.brk[i] > 0.06), dec: 0, hi: false },
      { lab: 'Coasting', unit: '%', m: pct(main, (d, i) => d.thr[i] < 0.05 && d.brk[i] < 0.06), r: pct(ref, (d, i) => d.thr[i] < 0.05 && d.brk[i] < 0.06), dec: 0, hi: false },
      { sec: 'Grip' },
      { lab: 'Peak lateral G', unit: 'g', m: max(main, 'latG', v => Math.abs(v)), r: max(ref, 'latG', v => Math.abs(v)), dec: 2, hi: true },
      { lab: 'Peak brake G', unit: 'g', m: -min(main, 'lonG', v => v), r: -min(ref, 'lonG', v => v), dec: 2, hi: true },
      { lab: 'Peak accel G', unit: 'g', m: max(main, 'lonG', v => v), r: max(ref, 'lonG', v => v), dec: 2, hi: true },
      { sec: 'Engine' },
      { lab: 'Max RPM', unit: '', m: max(main, 'rpm', v => v), r: max(ref, 'rpm', v => v), dec: 0, hi: true },
    ];
    let html = `<div class="hs-head"><span>Metric</span><span>Main</span><span>Ref</span><span>\u0394</span></div>`;
    for (const r of rows) {
      if (r.sec) { html += `<div class="hs-row sec"><span class="lab">${r.sec}</span><span></span><span></span><span></span></div>`; continue; }
      const d = r.m - r.r;
      const eps = r.dec ? 0.005 : 0.5;
      // "good" direction: hi=true => higher is better (green), else lower is better
      let cls = '';
      if (Math.abs(d) >= eps) cls = (r.hi ? d > 0 : d < 0) ? 'pos' : 'neg';
      html += `<div class="hs-row">
        <span class="lab">${r.lab}<span style="color:var(--ink-ghost);font-size:10px">${r.unit ? ' ' + r.unit : ''}</span></span>
        <span class="mn">${fmt(r.m, r.dec)}</span>
        <span class="rf">${fmt(r.r, r.dec)}</span>
        <span class="dv ${cls}">${d >= 0 ? '+' : '\u2212'}${fmt(Math.abs(d), r.dec)}</span>
      </div>`;
    }
    host.innerHTML = html;
  }

  // ============================================================
  //  SUSPENSION  (damper travel synthesised from load transfer)
  // ============================================================
  // cache derived corner-travel arrays per main lap
  let suspCache = { idx: -1, data: null };
  function suspData(lap, idx) {
    if (suspCache.idx === idx) return suspCache.data;
    const N = L.N;
    const LF = new Float32Array(N), RF = new Float32Array(N), LR = new Float32Array(N), RR = new Float32Array(N);
    const BASE = 30, KP = 9.5, KR = 8.0; // mm
    const lon = lap.data.lonG, lat = lap.data.latG;
    for (let i = 0; i < N; i++) {
      const pitch = -lon[i];        // braking (lon<0) -> front compresses
      const roll = lat[i];          // sign loads one side
      const bump = 1.6 * Math.sin(i * 0.21) + 1.1 * Math.sin(i * 0.07 + 1.3);
      LF[i] = clamp(BASE + pitch * KP + roll * KR + bump, 2, 58);
      RF[i] = clamp(BASE + pitch * KP - roll * KR + bump * 0.8, 2, 58);
      LR[i] = clamp(BASE - pitch * KP * 0.8 + roll * KR + bump * 0.6, 2, 58);
      RR[i] = clamp(BASE - pitch * KP * 0.8 - roll * KR + bump * 0.9, 2, 58);
    }
    suspCache = { idx, data: { LF, RF, LR, RR } };
    return suspCache.data;
  }

  function renderSuspension(st) {
    const main = L.stint[st.mainIdx];
    const sd = suspData(main, st.mainIdx);
    drawSuspGraph($('#sp-graph'), st, sd);
    drawSuspHist($('#sp-hist'), sd);
    buildCornerLoads(st, sd);
  }

  const SP_COL = { LF: '#34d2e3', RF: '#ffd23b', LR: '#b388ff', RR: '#ff9234' };

  function drawSuspGraph(cv, st, sd) {
    if (!cv) return;
    const ctx = setup(cv); if (!ctx) return; const W = cv._w, H = cv._h; if (W < 2) return;
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
    const GUT = 150, RPAD = 16, BAX = 28;
    const d0 = st.view.d0, d1 = st.view.d1, span = d1 - d0;
    const PX0 = GUT, PW = W - GUT - RPAD, areaH = H - BAX;
    const xOf = d => PX0 + (d - d0) / span * PW;
    const lanes = [{ t: 'FRONT AXLE', keys: ['LF', 'RF'] }, { t: 'REAR AXLE', keys: ['LR', 'RR'] }];
    const laneH = areaH / 2;
    const i0 = Math.max(0, Math.floor(d0 / L.STEP) - 1), i1 = Math.min(L.N - 1, Math.ceil(d1 / L.STEP) + 1);
    // gutter
    ctx.fillStyle = '#0d1013'; ctx.fillRect(0, 0, GUT, H);
    const dCur = di(st.cursorD);
    lanes.forEach((ln, li) => {
      const y0 = li * laneH, top = y0 + 16, bot = y0 + laneH - 12, ph = bot - top;
      const yOf = mm => bot - (mm / 60) * ph;
      // grid
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y0 + laneH + .5); ctx.lineTo(W, y0 + laneH + .5); ctx.stroke();
      ctx.strokeStyle = C.grid2;
      [0, 30, 60].forEach(mm => { const y = yOf(mm); ctx.beginPath(); ctx.moveTo(PX0, y + .5); ctx.lineTo(PX0 + PW, y + .5); ctx.stroke(); });
      // traces
      ctx.save(); ctx.beginPath(); ctx.rect(PX0, y0, PW, laneH); ctx.clip();
      for (const k of ln.keys) {
        ctx.strokeStyle = SP_COL[k]; ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.beginPath();
        let started = false;
        for (let i = i0; i <= i1; i++) { const x = xOf(i * L.STEP), y = yOf(sd[k][i]); started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true); }
        ctx.stroke();
      }
      ctx.restore();
      // gutter label + values at cursor
      ctx.fillStyle = '#9aa1a5'; ctx.font = '600 10px "Segoe UI",sans-serif'; ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
      ctx.fillText(ln.t, 12, y0 + 18);
      ctx.font = '10.5px "JetBrains Mono"'; ctx.fillStyle = C.axisDim; ctx.textAlign = 'right';
      ctx.fillText('60', GUT - 8, yOf(60) + 9); ctx.fillText('0 mm', GUT - 8, yOf(0));
      let ry = y0 + 38;
      for (const k of ln.keys) {
        ctx.fillStyle = SP_COL[k]; ctx.fillRect(12, ry - 9, 9, 9);
        ctx.fillStyle = '#aeb4b7'; ctx.font = '11px "Segoe UI",sans-serif'; ctx.textAlign = 'left'; ctx.fillText(k, 26, ry);
        ctx.textAlign = 'right'; ctx.fillStyle = SP_COL[k]; ctx.font = '700 14px "JetBrains Mono"';
        ctx.fillText(sd[k][dCur].toFixed(1), GUT - 10, ry + 2); ry += 22;
      }
    });
    // sector + corner verticals
    ctx.setLineDash([3, 4]); ctx.strokeStyle = 'rgba(120,150,170,.16)';
    for (const f of L.SECTORS) { const d = f * L.LAP; if (d < d0 || d > d1) continue; const x = xOf(d); ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, areaH); ctx.stroke(); }
    ctx.setLineDash([]);
    // x axis
    ctx.fillStyle = '#0d1013'; ctx.fillRect(PX0, areaH, PW + RPAD, BAX);
    ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(PX0, areaH + .5); ctx.lineTo(PX0 + PW, areaH + .5); ctx.stroke();
    const stepM = span > 3000 ? 500 : span > 1200 ? 250 : 100;
    ctx.fillStyle = C.axis; ctx.font = '11px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (let d = Math.ceil(d0 / stepM) * stepM; d <= d1; d += stepM) { const x = xOf(d); ctx.fillText(d.toString(), x, areaH + BAX / 2); }
    // cursor
    const cxp = xOf(st.cursorD);
    if (cxp >= PX0 && cxp <= PX0 + PW) { ctx.strokeStyle = C.accent; ctx.globalAlpha = .85; ctx.beginPath(); ctx.moveTo(cxp + .5, 0); ctx.lineTo(cxp + .5, areaH); ctx.stroke(); ctx.globalAlpha = 1; }
  }

  function drawSuspHist(cv, sd) {
    if (!cv) return;
    const ctx = setup(cv); if (!ctx) return; const W = cv._w, H = cv._h; if (W < 2) return;
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
    // damper velocity = d(travel)/dt ; dt = STEP / speed
    const vel = key => {
      const arr = sd[key], lap = curMain; const out = [];
      for (let i = 1; i < L.N; i++) {
        const dt = L.STEP / Math.max(8, lap.data.speed[i]);
        out.push((arr[i] - arr[i - 1]) / dt); // mm/s
      }
      return out;
    };
    const VR = 220, nb = 31;
    const hist = vals => { const h = new Float32Array(nb); for (const v of vals) { let b = Math.floor((v + VR) / (2 * VR) * nb); b = clamp(b, 0, nb - 1); h[b]++; } let s = 0; for (const x of h) s += x; for (let i = 0; i < nb; i++) h[i] /= (s || 1); return h; };
    const front = hist([...vel('LF'), ...vel('RF')]);
    const rear = hist([...vel('LR'), ...vel('RR')]);
    const PADL = 30, PADR = 12, PADT = 26, PADB = 26;
    const pw = W - PADL - PADR, ph = H - PADT - PADB;
    let mx = 0.0001; for (let b = 0; b < nb; b++) mx = Math.max(mx, front[b], rear[b]);
    const xOf = b => PADL + (b / nb) * pw, yOf = f => PADT + ph - (f / mx) * ph;
    // zero line (rebound | bump)
    const zx = PADL + 0.5 * pw;
    ctx.strokeStyle = '#222a2e'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(zx, PADT - 4); ctx.lineTo(zx, PADT + ph); ctx.stroke();
    ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(PADL, PADT + ph + .5); ctx.lineTo(PADL + pw, PADT + ph + .5); ctx.stroke();
    // series as outlined steps
    const series = [{ h: rear, c: '#b388ff' }, { h: front, c: '#34d2e3' }];
    for (const s of series) {
      ctx.strokeStyle = s.c; ctx.lineWidth = 1.6; ctx.beginPath();
      for (let b = 0; b < nb; b++) { const x = xOf(b) + (pw / nb) / 2, y = yOf(s.h[b]); b ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
      ctx.stroke();
      ctx.globalAlpha = .12; ctx.fillStyle = s.c;
      ctx.lineTo(xOf(nb - 1) + (pw / nb) / 2, PADT + ph); ctx.lineTo(xOf(0) + (pw / nb) / 2, PADT + ph); ctx.closePath(); ctx.fill();
      ctx.globalAlpha = 1;
    }
    // labels
    ctx.fillStyle = C.axisDim; ctx.font = '10.5px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    ctx.fillText('\u2190 REBOUND', PADL + pw * 0.26, PADT + ph + 6);
    ctx.fillText('BUMP \u2192', PADL + pw * 0.74, PADT + ph + 6);
    ctx.fillText('0', zx, PADT + ph + 6);
    // legend
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillStyle = '#34d2e3'; ctx.fillRect(PADL, 12, 9, 9); ctx.fillStyle = '#aeb4b7'; ctx.fillText('Front', PADL + 13, 17);
    ctx.fillStyle = '#b388ff'; ctx.fillRect(PADL + 64, 12, 9, 9); ctx.fillStyle = '#aeb4b7'; ctx.fillText('Rear', PADL + 77, 17);
  }

  let curMain = null;
  function buildCornerLoads(st, sd) {
    const host = $('#sp-loads'); if (!host) return;
    const k = di(st.cursorD);
    const cells = [['LF', 'L-FRONT'], ['RF', 'R-FRONT'], ['LR', 'L-REAR'], ['RR', 'R-REAR']];
    let html = '';
    for (const [key, label] of cells) {
      const mm = sd[key][k];
      html += `<div class="cl-cell">
        <span class="clpos">${label}</span>
        <span class="clval">${mm.toFixed(1)}</span>
        <span class="clunit">mm travel</span>
        <span class="clbar"><i style="width:${(mm / 60 * 100).toFixed(0)}%;background:${SP_COL[key]}"></i></span>
      </div>`;
    }
    host.innerHTML = html;
  }

  // ============================================================
  //  DISPATCH
  // ============================================================
  function render(name, st) {
    curMain = L.stint[st.mainIdx];
    if (name === 'track') renderTrack(st);
    else if (name === 'histogram') renderHistogram(st);
    else if (name === 'susp') renderSuspension(st);
  }

  global.I2Work = { render };
})(window);
