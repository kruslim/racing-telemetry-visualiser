/* ============================================================
   i2graphs.js — MoTeC i2-style stacked distance graphs,
   track map, and zoom overview. Pure rendering; the app owns
   state and interaction.
   ============================================================ */
(function (global) {
  'use strict';
  const L = global.Laps;
  const PS = global.PitSim;
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const lerp = (a, b, t) => a + (b - a) * t;

  const C = {
    speed:'#ffd23b', thr:'#39d46a', brk:'#ff463a', gear:'#4db5ff',
    rpm:'#b388ff', steer:'#ff9234', lat:'#34d2e3', lon:'#ffae3b',
    varLine:'#e7ebee', loseR:255, loseG:70, loseB:58, gainR:57, gainG:212, gainB:106,
    grid:'#171b1e', grid2:'#0f1316', axis:'#5b6266', axisDim:'#3a4044',
    accent:'#ff9234', bg:'#070809'
  };

  // lane config (top -> bottom). w = relative height weight.
  const LANES = [
    { id:'var',  title:'Time Variance', unit:'s',    w:0.95, kind:'variance' },
    { id:'speed',title:'Ground Speed',  unit:'km/h', w:1.75, min:0,   max:300,
      ch:[{key:'speed',color:C.speed,label:'Speed', conv:v=>v*3.6, dec:0}] },
    { id:'ped',  title:'Throttle / Brake',unit:'%',  w:1.40, min:0,   max:100,
      ch:[{key:'thr',color:C.thr,label:'Throttle',conv:v=>v*100,dec:0},
          {key:'brk',color:C.brk,label:'Brake',   conv:v=>v*100,dec:0}] },
    { id:'gear', title:'Gear',          unit:'',     w:0.78, min:0,   max:7, step:true,
      ch:[{key:'gear',color:C.gear,label:'Gear',conv:v=>v,dec:0}] },
    { id:'rpm',  title:'Engine RPM',    unit:'rpm',  w:1.25, min:2800,max:8200,
      ch:[{key:'rpm',color:C.rpm,label:'RPM',conv:v=>v,dec:0}] },
    { id:'steer',title:'Steering Angle',unit:'°',    w:1.05, min:-240,max:240, center:true,
      ch:[{key:'steer',color:C.steer,label:'Steering',conv:v=>v*220,dec:0}] },
    { id:'g',    title:'G Force',       unit:'g',    w:1.25, min:-2.6,max:2.6, center:true,
      ch:[{key:'latG',color:C.lat,label:'Lateral',conv:v=>v,dec:2},
          {key:'lonG',color:C.lon,label:'Longitud.',conv:v=>v,dec:2}] },
  ];

  const GUT = 170, RPAD = 16, TPAD = 8, BAX = 30;

  // ---- dynamic lane state: Channels menu hides lanes, Maths menu adds derived ones ----
  let hidden = new Set();          // lane ids the user has hidden
  let extraLanes = [];             // math channels added at runtime
  function activeLanes() { return LANES.concat(extraLanes).filter(l => !hidden.has(l.id)); }
  function allLanes() { return LANES.concat(extraLanes); }
  function setLaneHidden(id, h) { if (id === 'var') return; h ? hidden.add(id) : hidden.delete(id); }
  function isHidden(id) { return hidden.has(id); }
  // compute a derived per-sample channel for every lap and cache on lap.data[key]
  function ensureMathData(key, fn) {
    for (const lap of L.stint) {
      if (lap.data[key]) continue;
      const d = lap.data, out = new Float32Array(L.N);
      for (let i = 0; i < L.N; i++) out[i] = fn(d, i);
      lap.data[key] = out;
    }
  }
  function addMathChannel(def) {
    if (extraLanes.some(l => l.id === def.id)) return false;
    ensureMathData(def.ch[0].key, def._fn);
    extraLanes.push(def);
    return true;
  }
  function removeMathChannel(id) { extraLanes = extraLanes.filter(l => l.id !== id); }
  function hasMath(id) { return extraLanes.some(l => l.id === id); }

  // ---------- canvas dpi ----------
  // Measure with offsetWidth/Height: these are LAYOUT pixels, unaffected by the
  // #app `transform: scale(...)` used to fit the workbench to the viewport.
  // getBoundingClientRect() returns the *scaled* box, which during the initial
  // fit (or any time the stage is small) caches a tiny/zero backing store and
  // leaves the graph blank. offsetWidth never lies about layout size.
  function setup(cv) {
    const dpr = Math.min(2, global.devicePixelRatio || 1);
    const w = cv.offsetWidth || cv.clientWidth;
    const h = cv.offsetHeight || cv.clientHeight;
    if (!w || !h) return null;            // not laid out yet — caller bails, retries next frame
    if (cv._w !== w || cv._h !== h || cv._dpr !== dpr) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
      cv._w = w; cv._h = h; cv._dpr = dpr;
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  // precompute variance array (main - ref), recomputed when laps change
  let varCache = { mi:-1, ri:-1, arr:null, vmax:1 };
  function variance(mainLap, refLap, mi, ri) {
    if (varCache.mi === mi && varCache.ri === ri) return varCache;
    const tm = mainLap.data.t, tr = refLap.data.t, n = tm.length;
    const arr = new Float32Array(n);
    let mx = 0.05;
    for (let i = 0; i < n; i++) { arr[i] = tm[i] - tr[i]; mx = Math.max(mx, Math.abs(arr[i])); }
    const vmax = niceStep(mx);
    varCache = { mi, ri, arr, vmax };
    return varCache;
  }
  function niceStep(x){ const p=Math.pow(10,Math.floor(Math.log10(x))); const f=x/p; const n=f<=1?1:f<=2?2:f<=5?5:10; return n*p; }

  // ---------- main graph ----------
  let layout = { lanes:[], px0:0, pw:0, ya0:0, ya1:0 };

  function drawGraph(cv, st) {
    const ctx = setup(cv); if (!ctx) return;
    const W = cv._w, H = cv._h;
    const mainLap = L.stint[st.mainIdx], refLap = L.stint[st.refIdx];
    const d0 = st.view.d0, d1 = st.view.d1, span = d1 - d0;
    const PX0 = GUT, PW = W - GUT - RPAD;
    const xOf = d => PX0 + (d - d0) / span * PW;
    const dOfX = x => d0 + (x - PX0) / PW * span;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);

    // lane vertical layout
    const areaH = H - BAX;
    const ACTIVE = activeLanes();
    const totW = ACTIVE.reduce((s, l) => s + l.w, 0);
    let y = 0; const lanes = [];
    for (const ln of ACTIVE) {
      const h = (ln.w / totW) * areaH;
      lanes.push({ ln, y0: y, y1: y + h });
      y += h;
    }
    layout = { lanes, px0: PX0, pw: PW, ya0: 0, ya1: areaH, d0, d1, xOf, dOfX };

    const V = variance(mainLap, refLap, st.mainIdx, st.refIdx);

    // sample index range to draw
    const i0 = Math.max(0, Math.floor(d0 / L.STEP) - 1);
    const i1 = Math.min(L.N - 1, Math.ceil(d1 / L.STEP) + 1);

    // gutter background
    ctx.fillStyle = '#0d1013'; ctx.fillRect(0, 0, GUT, H);
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(GUT + .5, 0); ctx.lineTo(GUT + .5, areaH); ctx.stroke();

    for (const lo of lanes) {
      const { ln, y0, y1 } = lo;
      const top = y0 + TPAD, bot = y1 - 6, ph = bot - top;
      // lane separator
      ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, y1 + .5); ctx.lineTo(W, y1 + .5); ctx.stroke();

      // plot clip
      ctx.save();
      ctx.beginPath(); ctx.rect(PX0, y0, PW, y1 - y0); ctx.clip();

      if (ln.kind === 'variance') {
        drawVariance(ctx, V, xOf, top, bot, i0, i1);
      } else {
        const yOf = v => bot - (v - ln.min) / (ln.max - ln.min) * ph;
        // gridlines
        drawLaneGrid(ctx, ln, PX0, PW, top, bot, yOf);
        // traces: ref first (dim), then main (bright)
        for (const c of ln.ch) {
          drawTrace(ctx, refLap.data[c.key], c, ln, xOf, yOf, i0, i1, true);
        }
        for (const c of ln.ch) {
          drawTrace(ctx, mainLap.data[c.key], c, ln, xOf, yOf, i0, i1, false);
        }
        lo.yOf = yOf; lo.top = top; lo.bot = bot;
      }
      ctx.restore();

      // gutter text
      drawGutter(ctx, lo, st, mainLap, refLap, V);
    }

    // corner + sector verticals across stack
    drawVerticals(ctx, xOf, areaH, d0, d1);

    // x axis (distance)
    drawXAxis(ctx, xOf, areaH, BAX, d0, d1, PX0, PW);

    // cursor
    drawCursor(ctx, st, lanes, xOf, areaH, mainLap, V);
  }

  function drawLaneGrid(ctx, ln, x0, w, top, bot, yOf) {
    ctx.lineWidth = 1;
    const lines = ln.center ? [ln.min, 0, ln.max] : [ln.min, (ln.min + ln.max) / 2, ln.max];
    for (const lv of lines) {
      const yy = yOf(lv);
      ctx.strokeStyle = (lv === 0 && ln.center) ? '#222a2e' : C.grid2;
      ctx.beginPath(); ctx.moveTo(x0, yy + .5); ctx.lineTo(x0 + w, yy + .5); ctx.stroke();
    }
    // scale labels (max top, min bottom)
    ctx.fillStyle = C.axisDim; ctx.font = '10.5px "JetBrains Mono"'; ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText(fmtScale(ln.max), x0 + 4, top + 1);
    ctx.textBaseline = 'bottom';
    ctx.fillText(fmtScale(ln.min), x0 + 4, bot - 1);
  }
  function fmtScale(v){ return Math.abs(v) >= 1000 ? (v/1000).toFixed(1)+'k' : (Number.isInteger(v)?v:v.toFixed(1)); }

  function drawTrace(ctx, arr, c, ln, xOf, yOf, i0, i1, dim) {
    if (!arr) return;
    const cv = c.conv || (x => x);
    ctx.strokeStyle = c.color;
    ctx.globalAlpha = dim ? 0.34 : 1;
    ctx.lineWidth = dim ? 1 : 1.7;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    let started = false;
    for (let i = i0; i <= i1; i++) {
      const x = xOf(i * L.STEP);
      const v = cv(arr[i]);
      if (ln.step) { // sample-and-hold for gear
        const y = yOf(v);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else { ctx.lineTo(x, y); }
        const xn = xOf(Math.min(i1, i + 1) * L.STEP);
        ctx.lineTo(xn, y);
      } else {
        const y = yOf(v);
        started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true);
      }
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  function drawVariance(ctx, V, xOf, top, bot, i0, i1) {
    const ph = bot - top, mid = (top + bot) / 2, half = ph / 2;
    const yOf = v => mid - (v / V.vmax) * half;
    // grid + zero
    ctx.strokeStyle = C.grid2; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(layout.px0, yOf(V.vmax) + .5); ctx.lineTo(layout.px0 + layout.pw, yOf(V.vmax) + .5); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(layout.px0, yOf(-V.vmax) + .5); ctx.lineTo(layout.px0 + layout.pw, yOf(-V.vmax) + .5); ctx.stroke();
    // fill area to zero
    const z = yOf(0);
    for (let pass = 0; pass < 2; pass++) {
      // pass 0 = losing (>0, red), pass1 = gaining (<0, green)
      ctx.beginPath();
      let open = false;
      for (let i = i0; i <= i1; i++) {
        const x = xOf(i * L.STEP), v = V.arr[i];
        const yv = yOf(v);
        if (!open) { ctx.moveTo(x, z); open = true; }
        ctx.lineTo(x, yv);
      }
      // close back along zero
      for (let i = i1; i >= i0; i--) ctx.lineTo(xOf(i * L.STEP), z);
      ctx.closePath();
      ctx.save(); ctx.clip();
      ctx.fillStyle = pass === 0 ? `rgba(${C.loseR},${C.loseG},${C.loseB},.30)` : `rgba(${C.gainR},${C.gainG},${C.gainB},.30)`;
      if (pass === 0) ctx.fillRect(layout.px0, top, layout.pw, z - top);
      else ctx.fillRect(layout.px0, z, layout.pw, bot - z);
      ctx.restore();
    }
    // zero line
    ctx.strokeStyle = '#2a3236'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(layout.px0, z + .5); ctx.lineTo(layout.px0 + layout.pw, z + .5); ctx.stroke();
    // line
    ctx.strokeStyle = C.varLine; ctx.lineWidth = 1.6; ctx.beginPath();
    let started = false;
    for (let i = i0; i <= i1; i++) {
      const x = xOf(i * L.STEP), y = yOf(V.arr[i]);
      started ? ctx.lineTo(x, y) : (ctx.moveTo(x, y), started = true);
    }
    ctx.stroke();
    // scale labels
    ctx.fillStyle = C.axisDim; ctx.font = '10.5px "JetBrains Mono"'; ctx.textAlign = 'left';
    ctx.textBaseline = 'top'; ctx.fillText('+' + V.vmax.toFixed(2), layout.px0 + 4, top + 1);
    ctx.textBaseline = 'bottom'; ctx.fillText('-' + V.vmax.toFixed(2), layout.px0 + 4, bot - 1);
  }

  // channel "good direction" for Δ coloring — keep in sync with i2app RO set.
  // Only speed & throttle have a universal better direction; everything else is
  // contextual/signed and is shown neutral rather than colored by raw sign.
  const CHAN_DIR = { speed:'hi', thr:'hi' };

  function drawGutter(ctx, lo, st, mainLap, refLap, V) {
    const { ln, y0, y1 } = lo;
    const tall = (y1 - y0) > 66;
    ctx.textBaseline = 'alphabetic';
    // title
    ctx.fillStyle = '#9aa1a5'; ctx.font = '600 10px "Segoe UI",sans-serif'; ctx.textAlign = 'left';
    ctx.fillText(ln.title.toUpperCase(), 10, y0 + 14);
    if (ln.unit) { ctx.fillStyle = C.axisDim; ctx.font = '10.5px "JetBrains Mono"';
      ctx.textAlign = 'right'; ctx.fillText(ln.unit, GUT - 8, y0 + 14); }

    const di = clamp(Math.round(st.cursorD / L.STEP), 0, L.N - 1);
    let ry = y0 + (tall ? 32 : 30);

    if (ln.kind === 'variance') {
      const v = V.arr[di];
      ctx.textAlign = 'left'; ctx.fillStyle = '#cfd4d7'; ctx.font = '12px "Segoe UI",sans-serif';
      ctx.fillText('Δt @ cursor', 10, ry);
      ctx.textAlign = 'right'; ctx.font = '700 17px "JetBrains Mono"';
      ctx.fillStyle = v > 0.001 ? `rgb(${C.loseR},${C.loseG},${C.loseB})` : v < -0.001 ? `rgb(${C.gainR},${C.gainG},${C.gainB})` : '#cfd4d7';
      ctx.fillText((v >= 0 ? '+' : '−') + Math.abs(v).toFixed(3), GUT - 10, ry + 4);
      // final
      const fin = V.arr[L.N - 1];
      ctx.textAlign = 'left'; ctx.fillStyle = C.axis; ctx.font = '10.5px "JetBrains Mono"';
      ctx.fillText('LAP Δ ' + (fin >= 0 ? '+' : '−') + Math.abs(fin).toFixed(3) + ' s', 10, y1 - 8);
      return;
    }

    for (const c of ln.ch) {
      const mv = c.conv(mainLap.data[c.key][di]);
      const rv = c.conv(refLap.data[c.key][di]);
      const df = mv - rv;
      // swatch + label
      ctx.fillStyle = c.color; ctx.fillRect(10, ry - 9, 9, 9);
      ctx.fillStyle = '#aeb4b7'; ctx.font = '11px "Segoe UI",sans-serif'; ctx.textAlign = 'left';
      ctx.fillText(c.label, 24, ry);
      // main value (big, channel color)
      ctx.textAlign = 'right'; ctx.fillStyle = c.color; ctx.font = '700 16px "JetBrains Mono"';
      ctx.fillText(fmtNum(mv, c.dec), GUT - 10, ry + 3);
      if (tall) {
        ry += 16;
        ctx.fillStyle = C.axis; ctx.font = '11px "JetBrains Mono"'; ctx.textAlign = 'left';
        ctx.fillText('ref ' + fmtNum(rv, c.dec), 24, ry);
        ctx.textAlign = 'right';
        const gdir = CHAN_DIR[c.key];
        ctx.fillStyle = Math.abs(df) < (c.dec ? 0.01 : 0.5) || !gdir ? C.axis
          : (gdir === 'hi' ? df > 0 : df < 0) ? `rgb(${C.gainR},${C.gainG},${C.gainB})` : `rgb(${C.loseR},${C.loseG},${C.loseB})`;
        ctx.fillText((df >= 0 ? '+' : '−') + fmtNum(Math.abs(df), c.dec), GUT - 10, ry);
        ry += 20;
      } else { ry += 22; }
    }
  }
  function fmtNum(v, dec){ if (!isFinite(v)) return '—'; return dec ? v.toFixed(dec) : Math.round(v).toString(); }

  function drawVerticals(ctx, xOf, areaH, d0, d1) {
    // sectors
    for (const f of L.SECTORS) {
      const d = f * L.LAP; if (d < d0 || d > d1) continue;
      const x = xOf(d);
      ctx.strokeStyle = 'rgba(120,150,170,.16)'; ctx.lineWidth = 1; ctx.setLineDash([3, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, areaH); ctx.stroke();
      ctx.setLineDash([]);
    }
    // corners
    ctx.font = '10.5px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (const c of L.CORNERS) {
      if (c.d < d0 || c.d > d1) continue;
      const x = xOf(c.d);
      ctx.strokeStyle = 'rgba(255,255,255,.05)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, areaH); ctx.stroke();
      ctx.fillStyle = '#5b6266'; ctx.fillText(c.label, x, 3);
    }
  }

  function drawXAxis(ctx, xOf, areaH, bax, d0, d1, px0, pw) {
    ctx.fillStyle = '#0d1013'; ctx.fillRect(px0, areaH, pw + RPAD, bax);
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px0, areaH + .5); ctx.lineTo(px0 + pw, areaH + .5); ctx.stroke();
    // ticks every 250/500 m depending on span
    const span = d1 - d0;
    const stepM = span > 3000 ? 500 : span > 1200 ? 250 : 100;
    ctx.fillStyle = C.axis; ctx.font = '11px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const first = Math.ceil(d0 / stepM) * stepM;
    for (let d = first; d <= d1; d += stepM) {
      const x = xOf(d);
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(x, areaH); ctx.lineTo(x, areaH + 4); ctx.stroke();
      ctx.fillText(d.toString(), x, areaH + bax / 2 + 1);
    }
    ctx.textAlign = 'left';
    ctx.fillStyle = C.axisDim; ctx.fillText('DISTANCE  m', px0 + 4, areaH + bax / 2 + 1);
  }

  function drawCursor(ctx, st, lanes, xOf, areaH, mainLap, V) {
    const x = xOf(st.cursorD);
    if (x < layout.px0 - 1 || x > layout.px0 + layout.pw + 1) return;
    ctx.strokeStyle = C.accent; ctx.lineWidth = 1; ctx.globalAlpha = .85;
    ctx.beginPath(); ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, areaH); ctx.stroke();
    ctx.globalAlpha = 1;
    // dots where main trace crosses
    const di = clamp(Math.round(st.cursorD / L.STEP), 0, L.N - 1);
    for (const lo of lanes) {
      const ln = lo.ln; if (ln.kind === 'variance' || !lo.yOf) continue;
      for (const c of ln.ch) {
        const y = lo.yOf(c.conv(mainLap.data[c.key][di]));
        ctx.fillStyle = c.color;
        ctx.beginPath(); ctx.arc(x, y, 2.6, 0, 7); ctx.fill();
        ctx.strokeStyle = '#070809'; ctx.lineWidth = 1; ctx.stroke();
      }
    }
    // top flag with distance
    ctx.fillStyle = C.accent;
    const lbl = Math.round(st.cursorD) + ' m';
    ctx.font = '700 10px "JetBrains Mono"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const tw = ctx.measureText(lbl).width + 12;
    const fx = clamp(x, layout.px0 + tw / 2, layout.px0 + layout.pw - tw / 2);
    ctx.fillRect(fx - tw / 2, 0, tw, 14);
    ctx.fillStyle = '#161208'; ctx.fillText(lbl, fx, 7);
  }

  // ---------- track map ----------
  function drawMap(cv, st) {
    const ctx = setup(cv); if (!ctx) return;
    const W = cv._w, H = cv._h;
    const T = PS.TRACK, mainLap = L.stint[st.mainIdx];
    ctx.clearRect(0, 0, W, H); ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
    let minx=1e9,miny=1e9,maxx=-1e9,maxy=-1e9;
    for (const p of T.raw){minx=Math.min(minx,p[0]);miny=Math.min(miny,p[1]);maxx=Math.max(maxx,p[0]);maxy=Math.max(maxy,p[1]);}
    const pad=30, sc=Math.min((W-pad*2)/(maxx-minx),(H-pad*2)/(maxy-miny));
    const ox=pad+(W-pad*2-(maxx-minx)*sc)/2, oy=pad+(H-pad*2-(maxy-miny)*sc)/2;
    const tx=p=>[ox+(p[0]-minx)*sc, oy+(p[1]-miny)*sc];

    ctx.lineJoin='round'; ctx.lineCap='round';
    ctx.strokeStyle='#1c2125'; ctx.lineWidth=14;
    ctx.beginPath(); T.raw.forEach((p,i)=>{const q=tx(p); i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]);}); ctx.closePath(); ctx.stroke();
    ctx.strokeStyle='#0a0c0e'; ctx.lineWidth=10; ctx.stroke();

    // speed-colored centerline from MAIN lap
    const spd = mainLap.data.speed, vmax = PS.V_MAX;
    ctx.lineWidth=3.2;
    const M = T.raw.length;
    for (let i=0;i<M;i++){
      const a=tx(T.raw[i]), b=tx(T.raw[(i+1)%M]);
      // map track index -> distance fraction
      const d = (i/M)*L.LAP, di=Math.min(L.N-1,Math.round(d/L.STEP));
      const vf=spd[di]/vmax;
      const col=vf>0.72?'57,212,106':vf>0.46?'255,210,59':'255,70,58';
      ctx.strokeStyle=`rgba(${col},.9)`;
      ctx.beginPath(); ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); ctx.stroke();
    }
    // sector splits
    for (const f of L.SECTORS){ const p=tx(PS.posAt(f*L.LAP));
      ctx.fillStyle='rgba(120,150,170,.6)'; ctx.beginPath(); ctx.arc(p[0],p[1],3,0,7); ctx.fill(); }
    // corner labels
    ctx.font='10.5px "JetBrains Mono"'; ctx.fillStyle='#6b7276'; ctx.textAlign='center'; ctx.textBaseline='middle';
    for (const c of L.CORNERS){ const p=tx(PS.posAt(c.d));
      // offset label outward a touch
      ctx.fillText(c.label, p[0], p[1]-9);
    }
    // start/finish
    const sf=tx(T.raw[0]); ctx.save(); ctx.translate(sf[0],sf[1]);
    ctx.fillStyle='#fff'; ctx.fillRect(-1,-8,2,16); ctx.restore();
    // cursor dot
    const mp=tx(PS.posAt(st.cursorD));
    ctx.shadowColor='rgba(255,146,52,.9)'; ctx.shadowBlur=12; ctx.fillStyle=C.accent;
    ctx.beginPath(); ctx.arc(mp[0],mp[1],6,0,7); ctx.fill(); ctx.shadowBlur=0;
    ctx.strokeStyle='#0a0c0e'; ctx.lineWidth=2; ctx.stroke();
  }

  // ---------- overview / zoom strip ----------
  let ovLayout = { px0:0, pw:0 };
  function drawOverview(cv, st) {
    const ctx = setup(cv); if (!ctx) return;
    const W = cv._w, H = cv._h;
    const mainLap = L.stint[st.mainIdx];
    ctx.clearRect(0,0,W,H); ctx.fillStyle=C.bg; ctx.fillRect(0,0,W,H);
    const px0=GUT, pw=W-GUT-RPAD, top=20, bot=H-8;
    ovLayout={px0,pw,top,bot};
    const xOf=d=>px0+d/L.LAP*pw;
    // mini speed
    const spd=mainLap.data.speed, vmax=PS.V_MAX;
    ctx.strokeStyle=C.speed; ctx.lineWidth=1.2; ctx.globalAlpha=.8; ctx.beginPath();
    for (let i=0;i<L.N;i++){ const x=xOf(i*L.STEP), y=bot-(spd[i]/vmax)*(bot-top); i?ctx.lineTo(x,y):ctx.moveTo(x,y); }
    ctx.stroke(); ctx.globalAlpha=1;
    // corner + sector ticks
    ctx.strokeStyle='rgba(255,255,255,.06)';
    for (const c of L.CORNERS){ const x=xOf(c.d); ctx.beginPath(); ctx.moveTo(x,top); ctx.lineTo(x,bot); ctx.stroke(); }
    // window rectangle
    const wx0=xOf(st.view.d0), wx1=xOf(st.view.d1);
    ctx.fillStyle='rgba(255,146,52,.12)'; ctx.fillRect(wx0,top-4,wx1-wx0,bot-top+8);
    ctx.strokeStyle=C.accent; ctx.lineWidth=1; ctx.strokeRect(wx0+.5,top-4.5,wx1-wx0,bot-top+8);
    // handles
    ctx.fillStyle=C.accent; ctx.fillRect(wx0-1,top-4,3,bot-top+8); ctx.fillRect(wx1-2,top-4,3,bot-top+8);
    // cursor
    const cx=xOf(st.cursorD);
    ctx.strokeStyle='rgba(255,146,52,.7)'; ctx.beginPath(); ctx.moveTo(cx,top-4); ctx.lineTo(cx,bot+4); ctx.stroke();
    // gutter label
    ctx.fillStyle='#0d1013'; ctx.fillRect(0,0,GUT,H);
    ctx.fillStyle='#9aa1a5'; ctx.font='600 10px "Segoe UI",sans-serif'; ctx.textAlign='left'; ctx.textBaseline='alphabetic';
    ctx.fillText('LAP OVERVIEW',10,18);
    ctx.fillStyle=C.axis; ctx.font='10.5px "JetBrains Mono"';
    ctx.fillText('drag window',10,34); ctx.fillText('to zoom',10,46);
    ctx.strokeStyle=C.grid; ctx.beginPath(); ctx.moveTo(GUT+.5,0); ctx.lineTo(GUT+.5,H); ctx.stroke();
  }

  global.I2Graphs = {
    LANES, C, GUT, RPAD,
    drawGraph, drawMap, drawOverview,
    getLayout: () => layout,
    getOvLayout: () => ovLayout,
    variance: (st) => variance(L.stint[st.mainIdx], L.stint[st.refIdx], st.mainIdx, st.refIdx),
    // lane management (Channels + Maths menus)
    allLanes, activeLanes, setLaneHidden, isHidden,
    addMathChannel, removeMathChannel, hasMath
  };
})(window);
