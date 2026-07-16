/* ============================================================
   i2app.js — boot, scaling, lap selection, cursor scrubbing,
   zoom overview, readout + status wiring.
   ============================================================ */
(function () {
  'use strict';
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  const L = window.Laps, G = window.I2Graphs, PS = window.PitSim;
  const C = G.C;
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;

  // ---------- state ----------
  const st = {
    mainIdx: L.mainIdx,
    refIdx: L.refIdx,
    cursorD: 0,
    ws: 'timedist',
    view: { d0: 0, d1: L.LAP }
  };
  // open on the steepest time-loss point (the braking zone of the mistake)
  (function () {
    const V = G.variance(st);
    let bi = 1, bm = -1e9;
    for (let i = 6; i < V.arr.length - 1; i++) {
      const slope = V.arr[i + 1] - V.arr[i - 1];
      if (slope > bm) { bm = slope; bi = i; }
    }
    st.cursorD = clamp(bi * L.STEP - 30, 0, L.LAP);
  })();

  // ---------- readout channel set ----------
  // `dir` drives Δ coloring: 'hi' = more is better (green), 'lo' = less is better,
  // 'neutral' = no value judgement (signed/contextual channels stay ink-coloured).
  // Brake is context-dependent (more brake into a corner isn't inherently worse),
  // steering/G/gear/rpm carry no universal "good" direction — so none are colored
  // green/red purely by arithmetic sign anymore.
  const RO = [
    { key:'speed', label:'Ground Speed', unit:'km/h', conv:v=>v*3.6, dec:0, color:C.speed, dir:'hi' },
    { key:'thr',   label:'Throttle',     unit:'%',    conv:v=>v*100, dec:0, color:C.thr, dir:'hi' },
    { key:'brk',   label:'Brake',        unit:'%',    conv:v=>v*100, dec:0, color:C.brk, dir:'neutral' },
    { key:'gear',  label:'Gear',         unit:'',     conv:v=>v,     dec:0, color:C.gear, dir:'neutral' },
    { key:'rpm',   label:'Engine RPM',   unit:'rpm',  conv:v=>v,     dec:0, color:C.rpm, dir:'neutral' },
    { key:'steer', label:'Steering',     unit:'°',    conv:v=>v*220, dec:0, color:C.steer, dir:'neutral' },
    { key:'latG',  label:'Lateral G',    unit:'g',    conv:v=>v,     dec:2, color:C.lat, dir:'neutral' },
    { key:'lonG',  label:'Longitudinal G', unit:'g',  conv:v=>v,     dec:2, color:C.lon, dir:'neutral' },
  ];

  // ---------- formatters ----------
  function fmtLap(t) {
    if (!isFinite(t)) return '—';
    const m = Math.floor(t / 60), s = t - m * 60;
    return m + ':' + s.toFixed(3).padStart(6, '0');
  }
  function fmtNum(v, dec) { return dec ? v.toFixed(dec) : Math.round(v).toString(); }
  function lapTag(l) { return l.synthetic ? 'OPT' : 'L' + l.lapNo; }
  function di() { return clamp(Math.round(st.cursorD / L.STEP), 0, L.N - 1); }

  // ============================================================
  //  SIDEBAR BUILD
  // ============================================================
  function buildSession() {
    const best = Math.min(...L.realLaps().map(l => l.time));
    const venue = L.trackName || 'Autodrome Nazionale';
    const km = (L.LAP / 1000).toFixed(2);
    const src = L.source === 'api' ? 'Backend telemetry' : 'Offline simulation';
    $('#session').innerHTML = `
      <span class="k">Venue</span><span class="v">${venue}</span>
      <span class="k">Layout</span><span class="v">Grand Prix · ${km} km</span>
      <span class="k">Vehicle</span><span class="v">GT3 · #07</span>
      <span class="k">Driver</span><span class="v">R. LIM</span>
      <span class="k">Source</span><span class="v">${src}</span>
      <span class="k">Date</span><span class="v">04 Jun 2026</span>
      <span class="k">Logged</span><span class="v">${L.realLaps().length} laps · 60 Hz</span>
      <span class="k">Best</span><span class="v hl">${fmtLap(best)}</span>`;
  }

  function buildLapList() {
    const real = L.realLaps();
    const fastest = Math.min(...real.map(l => l.time));
    const bestSec = [0, 1, 2].map(si => Math.min(...real.map(l => l.sectors[si])));
    const opt = L.stint[L.optIdx];

    const secCells = (lap) => [0, 1, 2].map(si => {
      const v = lap.sectors[si];
      const isBest = !lap.synthetic && Math.abs(v - bestSec[si]) < 0.0005;
      return `<span class="sc ${isBest ? 'best' : ''}">${v.toFixed(2)}</span>`;
    }).join('');

    const row = (l, i, synthetic) => {
      const gap = l.time - fastest;
      const isMain = i === st.mainIdx, isRef = i === st.refIdx;
      const cls = [synthetic ? 'is-opt' : '', isMain ? 'is-main' : '', isRef ? 'is-ref' : '',
        (!synthetic && l.time === fastest) ? 'is-fastest' : ''].join(' ');
      const name = synthetic ? 'OPT' : 'L' + l.lapNo;
      const gapTxt = synthetic ? '<span class="best-tag">best bits</span>' : (gap < 0.0005 ? 'fastest' : '+' + gap.toFixed(2));
      return `<div class="ll-row ${cls}" data-i="${i}">
        <div class="ll-line">
          <span class="ll-pick">
            <button class="pk pk-m ${isMain ? 'on' : ''}" data-pick="main" data-i="${i}" ${synthetic ? 'disabled title="Theoretical lap can only be a reference"' : 'title="Set as Main lap"'}>M</button>
            <button class="pk pk-r ${isRef ? 'on' : ''}" data-pick="ref" data-i="${i}" title="Set as Reference lap">R</button>
          </span>
          <span class="lapn">${name}</span>
          <span class="lt">${fmtLap(l.time)}</span>
          <span class="gap">${gapTxt}</span>
        </div>
        <div class="ll-sectors">${secCells(l)}</div>
      </div>`;
    };

    const rows = real.map((l, i) => row(l, i, false)).join('');
    const optRow = `<div class="ll-opt-sep">Theoretical best · assembled from fastest mini-sectors</div>` + row(opt, L.optIdx, true);
    $('#ll-rows').innerHTML = rows + optRow;

    $$('#ll-rows .pk').forEach(b => {
      if (b.disabled) return;
      b.addEventListener('click', e => {
        e.stopPropagation();
        const i = +b.dataset.i;
        b.dataset.pick === 'main' ? setMain(i) : setRef(i);
      });
    });
    $$('#ll-rows .ll-row').forEach(r => {
      r.addEventListener('click', () => { const i = +r.dataset.i; if (!L.stint[i].synthetic) setMain(i); });
      r.addEventListener('contextmenu', e => { e.preventDefault(); setRef(+r.dataset.i); });
    });
  }

  function buildReadout() {
    let html = '';
    for (const c of RO) {
      html += `<div class="ro-row" data-k="${c.key}">
        <span class="ch"><i style="background:${c.color}"></i>${c.label}</span>
        <span class="mn">—</span><span class="rf">—</span><span class="df">—</span>
      </div>`;
    }
    html += `<div class="ro-row sep big" data-k="_dist">
        <span class="ch">Distance / Lap %</span><span class="mn">—</span><span class="rf">—</span><span class="df"></span></div>`;
    html += `<div class="ro-row big" data-k="_time">
        <span class="ch">Elapsed Time</span><span class="mn">—</span><span class="rf">—</span><span class="df">—</span></div>`;
    html += `<div class="ro-row big" data-k="_var">
        <span class="ch">Time Variance</span><span class="mn">—</span><span class="rf"></span><span class="df">—</span></div>`;
    $('#readout').innerHTML = html;
  }

  function updateReadout() {
    const i = di();
    const main = L.stint[st.mainIdx], ref = L.stint[st.refIdx];
    for (const c of RO) {
      const row = $(`#readout .ro-row[data-k="${c.key}"]`);
      const mv = c.conv(main.data[c.key][i]), rv = c.conv(ref.data[c.key][i]), df = mv - rv;
      row.querySelector('.mn').textContent = fmtNum(mv, c.dec) + (c.unit ? '' : '');
      row.querySelector('.rf').textContent = fmtNum(rv, c.dec);
      const dfEl = row.querySelector('.df');
      const eps = c.dec ? 0.005 : 0.5;
      dfEl.textContent = (df >= 0 ? '+' : '−') + fmtNum(Math.abs(df), c.dec);
      let dcls = '';
      if (Math.abs(df) >= eps && c.dir !== 'neutral')
        dcls = ((c.dir === 'hi') ? df > 0 : df < 0) ? ' pos' : ' neg';
      dfEl.className = 'df' + dcls;
    }
    // distance
    const dRow = $('#readout .ro-row[data-k="_dist"]');
    dRow.querySelector('.mn').textContent = Math.round(st.cursorD) + ' m';
    dRow.querySelector('.rf').textContent = (st.cursorD / L.LAP * 100).toFixed(1) + '%';
    // elapsed time
    const tRow = $('#readout .ro-row[data-k="_time"]');
    const tm = L.timeAtDist(main, st.cursorD), tr = L.timeAtDist(ref, st.cursorD);
    tRow.querySelector('.mn').textContent = fmtLap(tm);
    tRow.querySelector('.rf').textContent = fmtLap(tr);
    const tdf = tm - tr;
    const tdfEl = tRow.querySelector('.df');
    tdfEl.textContent = (tdf >= 0 ? '+' : '−') + Math.abs(tdf).toFixed(3);
    tdfEl.className = 'df' + (tdf > 0.001 ? ' neg' : tdf < -0.001 ? ' pos' : '');
    // variance
    const V = G.variance(st), v = V.arr[i];
    const vRow = $('#readout .ro-row[data-k="_var"]');
    vRow.querySelector('.mn').textContent = (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(3) + ' s';
    vRow.querySelector('.mn').style.color = v > 0.001 ? '#ff463a' : v < -0.001 ? '#39d46a' : '#fff';
    const fin = V.arr[L.N - 1];
    const fEl = vRow.querySelector('.df');
    fEl.textContent = 'Σ ' + (fin >= 0 ? '+' : '−') + Math.abs(fin).toFixed(3);
    fEl.className = 'df' + (fin > 0 ? ' neg' : ' pos');
  }

  function updateStatus() {
    const main = L.stint[st.mainIdx], ref = L.stint[st.refIdx];
    const V = G.variance(st), v = V.arr[di()];
    $('#sb-cursor b').textContent = Math.round(st.cursorD) + ' m';
    $('#sb-lappct b').textContent = (st.cursorD / L.LAP * 100).toFixed(1) + '%';
    $('#sb-main b').textContent = lapTag(main) + '  ' + fmtLap(main.time);
    $('#sb-ref b').textContent = lapTag(ref) + '  ' + fmtLap(ref.time);
    const dv = $('#sb-var b');
    dv.textContent = (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(3) + ' s';
    dv.style.color = v > 0.001 ? '#ff463a' : v < -0.001 ? '#39d46a' : '#fff';
    // sector at cursor
    const f = st.cursorD / L.LAP;
    const sec = f < L.SECTORS[0] ? 1 : f < L.SECTORS[1] ? 2 : 3;
    $('#sb-sector b').textContent = 'S' + sec;
  }

  // ---------- lap selection ----------
  function setMain(i) { if (i === st.refIdx) return; st.mainIdx = i; buildLapList(); renderAll(); }
  function setRef(i) { if (i === st.mainIdx) return; st.refIdx = i; buildLapList(); buildSession(); renderAll(); }

  // ============================================================
  //  RENDER
  // ============================================================
  function renderAll() {
    if (st.ws === 'timedist') {
      G.drawGraph($('#graphcanvas'), st);
      G.drawMap($('#mapcanvas'), st);
      G.drawOverview($('#overviewcanvas'), st);
      if (window.Coach) Coach.overlayTimedist(st);
    } else if (st.ws === 'coach') {
      if (window.Coach) Coach.render(st);
    } else if (window.I2Work) {
      I2Work.render(st.ws, st);
      if (window.Coach && st.ws === 'track') Coach.overlayTrack(st);
    }
    updateReadout();
    updateStatus();
    $('#map-dist').textContent = Math.round(st.cursorD) + ' m';
    $('#map-pct').textContent = (st.cursorD / L.LAP * 100).toFixed(1) + '%';
    // Re-anchor the LLM's graph annotations after every render (zoom/pan/tab).
    if (window.ChatAnnotations) window.ChatAnnotations.reposition();
  }

  // ---------- worksheet (tab) switching ----------
  function setWorksheet(name) {
    if (!name) return;
    st.ws = name;
    $$('.tabs .tab').forEach(t => t.classList.toggle('on', t.dataset.ws === name));
    $$('.wsarea .ws').forEach(w => w.classList.toggle('on', w.id === 'ws-' + name));
    renderAll();
    // second pass once the newly-shown canvases have real layout size
    requestAnimationFrame(renderAll);
  }
  $$('.tabs .tab').forEach(t => t.addEventListener('click', () => setWorksheet(t.dataset.ws)));

  // ---------- coaching layer bridge ----------
  // jump the cursor to a distance, optionally zooming the view window around it
  function jumpTo(d, span) {
    st.cursorD = clamp(d, 0, L.LAP);
    if (span) {
      const s = clamp(span, 300, L.LAP);
      let d0 = clamp(d - s / 2, 0, L.LAP - s);
      st.view.d0 = d0; st.view.d1 = d0 + s;
    }
    renderAll();
  }
  window.I2App = { st, jumpTo, renderAll, setWorksheet, fmtLap, setMain, setRef, buildLapList };

  // ============================================================
  //  INTERACTION
  // ============================================================
  function graphMouse(e) {
    const cv = $('#graphcanvas'), r = cv.getBoundingClientRect();
    const x = (e.clientX - r.left) * (cv._w / r.width);
    const lay = G.getLayout();
    if (x < lay.px0) return;
    st.cursorD = clamp(lay.dOfX(x), 0, L.LAP);
    renderAll();
  }
  $('#graphcanvas').addEventListener('mousemove', e => { if (e.buttons === 0 || e.buttons === 1) graphMouse(e); });
  $('#graphcanvas').addEventListener('mousedown', graphMouse);

  // wheel zoom around cursor
  $('#graphcanvas').addEventListener('wheel', e => {
    e.preventDefault();
    const lay = G.getLayout(); const span = st.view.d1 - st.view.d0;
    const factor = e.deltaY > 0 ? 1.18 : 0.85;
    let ns = clamp(span * factor, 300, L.LAP);
    const center = st.cursorD;
    let d0 = center - (center - st.view.d0) * (ns / span);
    let d1 = d0 + ns;
    if (d0 < 0) { d0 = 0; d1 = ns; }
    if (d1 > L.LAP) { d1 = L.LAP; d0 = L.LAP - ns; }
    st.view.d0 = d0; st.view.d1 = clamp(d1, 0, L.LAP);
    renderAll();
  }, { passive: false });

  // overview drag (pan / resize)
  let ovDrag = null;
  function ovDistAtX(clientX) {
    const cv = $('#overviewcanvas'), r = cv.getBoundingClientRect();
    const x = (clientX - r.left) * (cv._w / r.width);
    const ov = G.getOvLayout();
    return clamp((x - ov.px0) / ov.pw * L.LAP, 0, L.LAP);
  }
  $('#overviewcanvas').addEventListener('mousedown', e => {
    const d = ovDistAtX(e.clientX), span = st.view.d1 - st.view.d0;
    const edge = span * 0.06;
    if (Math.abs(d - st.view.d0) < edge) ovDrag = { mode: 'l' };
    else if (Math.abs(d - st.view.d1) < edge) ovDrag = { mode: 'r' };
    else if (d > st.view.d0 && d < st.view.d1) ovDrag = { mode: 'pan', off: d - st.view.d0 };
    else { // jump: center window here
      const half = span / 2; let d0 = clamp(d - half, 0, L.LAP - span);
      st.view.d0 = d0; st.view.d1 = d0 + span; renderAll(); ovDrag = { mode: 'pan', off: half };
    }
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!ovDrag) return;
    const d = ovDistAtX(e.clientX);
    if (ovDrag.mode === 'pan') {
      const span = st.view.d1 - st.view.d0;
      let d0 = clamp(d - ovDrag.off, 0, L.LAP - span);
      st.view.d0 = d0; st.view.d1 = d0 + span;
    } else if (ovDrag.mode === 'l') {
      st.view.d0 = clamp(d, 0, st.view.d1 - 300);
    } else if (ovDrag.mode === 'r') {
      st.view.d1 = clamp(d, st.view.d0 + 300, L.LAP);
    }
    renderAll();
  });
  window.addEventListener('mouseup', () => { ovDrag = null; });
  $('#overviewcanvas').addEventListener('dblclick', () => { st.view.d0 = 0; st.view.d1 = L.LAP; renderAll(); });

  // keyboard cursor step
  window.addEventListener('keydown', e => {
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const step = (e.shiftKey ? 20 : 4) * L.STEP * (e.key === 'ArrowLeft' ? -1 : 1);
      st.cursorD = clamp(st.cursorD + step, 0, L.LAP);
      renderAll();
    }
  });

  // toolbar
  $('#tb-reset').addEventListener('click', () => { st.view.d0 = 0; st.view.d1 = L.LAP; renderAll(); });
  let showRef = true;
  // (reference overlay always on in this build; button reflects state)

  // ---------- scaling ----------
  function fit() {
    // Scale to the #viewport region (the right side, beside the chat pane), not
    // the whole window — the chat lives outside the scaled stage at native size.
    const vp = document.getElementById('viewport');
    const w = vp && vp.clientWidth ? vp.clientWidth : window.innerWidth;
    const h = vp && vp.clientHeight ? vp.clientHeight : window.innerHeight;
    const sc = Math.min(w / 1920, h / 1080);
    $('#app').style.transform = `scale(${sc})`;
  }
  window.addEventListener('resize', () => { fit(); requestAnimationFrame(renderAll); });

  // ---------- boot ----------
  buildSession();
  buildLapList();
  buildReadout();

  // Reveal only after the first real paint, so the user never sees the
  // unscaled stage or a black (pre-fit) frame. fit() runs on a post-layout
  // rAF so the viewport has measured size before we compute the scale.
  function reveal() { const a = document.getElementById('app'); if (a) a.classList.add('booted'); }
  function firstPaint() {
    try { fit(); renderAll(); } catch (e) { console.error(e); }
    requestAnimationFrame(() => { try { renderAll(); } catch (e) { console.error(e); } reveal(); });
  }
  requestAnimationFrame(firstPaint);
  setTimeout(() => { try { fit(); renderAll(); } catch (e) {} reveal(); }, 150); // safety net

  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => requestAnimationFrame(renderAll));
    ['.graphwrap', '.mapwrap', '.overview', '.wsarea'].forEach(sel => {
      const el = document.querySelector(sel); if (el) ro.observe(el);
    });
  }
  window.addEventListener('load', () => requestAnimationFrame(renderAll));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => renderAll());
})();
