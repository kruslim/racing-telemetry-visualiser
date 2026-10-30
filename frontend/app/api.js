/* ============================================================
   api.js — the real-backend data layer.

   Builds window.Laps (and an API-derived window.PitSim track) from the RTV REST
   API so the i2 graphs, coach worksheet and LLM chat all run on real telemetry.
   Falls back to the offline simulation (window.LapsSim / the sim.js PitSim) when
   the backend is unreachable, then boots the render modules in dependency order.

   Contract consumed downstream (see i2graphs.js / i2app.js):
     Laps = { LAP, STEP, N, SECTORS, CORNERS, stint:[{lapNo,time,sectors,data:{
              speed(m/s), t(s), thr, brk, gear, rpm, steer(-1..1), latG(g),
              lonG(g), dist(m)}}], refIdx, mainIdx, optIdx, realLaps(),
              timeAtDist(), sampleAtDist(), V_MAX, sessionId }
   ============================================================ */
(function (global) {
  'use strict';

  const API = '/api/v1';
  const G_ACCEL = 9.80665;
  const STEER_FULL = 4.5;   // rad of wheel angle mapped to normalised ±1
  const CHANNELS = ['Speed', 'Throttle', 'Brake', 'Gear', 'RPM',
    'SteeringWheelAngle', 'LatAccel', 'LongAccel'];

  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const lerp = (a, b, t) => a + (b - a) * t;

  // Keep a handle on the offline sim geometry so the fallback can restore it.
  const SIM_PITSIM = global.PitSim;

  async function getJSON(path) {
    const r = await fetch(API + path, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    return r.json();
  }

  // ---- resample a (possibly non-uniform, null-holed) series onto j/N grid ----
  function resample(xv, yv, N) {
    // clean: keep pairs with finite y, x ascending
    const xs = [], ys = [];
    for (let i = 0; i < xv.length; i++) {
      const y = yv[i];
      if (y === null || y === undefined || Number.isNaN(y)) continue;
      xs.push(xv[i]); ys.push(y);
    }
    const out = new Float32Array(N);
    if (!xs.length) return out;
    let k = 0;
    for (let j = 0; j < N; j++) {
      const px = j / N;
      while (k < xs.length - 1 && xs[k + 1] < px) k++;
      if (px <= xs[0]) { out[j] = ys[0]; continue; }
      if (px >= xs[xs.length - 1]) { out[j] = ys[ys.length - 1]; continue; }
      const x0 = xs[k], x1 = xs[k + 1];
      const t = x1 > x0 ? (px - x0) / (x1 - x0) : 0;
      out[j] = lerp(ys[k], ys[k + 1], t);
    }
    return out;
  }

  function smooth(arr, passes) {
    for (let p = 0; p < passes; p++) {
      let prev = arr[0];
      for (let i = 1; i < arr.length - 1; i++) {
        const cur = arr[i];
        arr[i] = (prev + 2 * cur + arr[i + 1]) * 0.25;
        prev = cur;
      }
    }
  }

  function sectorTimes(t, N, STEP, LAP, SECTORS) {
    const tAt = d => {
      const x = clamp(d / STEP, 0, N - 1), i = Math.floor(x);
      return lerp(t[i], t[Math.min(N - 1, i + 1)], x - i);
    };
    const a = tAt(SECTORS[0] * LAP), b = tAt(SECTORS[1] * LAP), c = t[N - 1];
    return [a, b - a, c - b];
  }

  // ---- build one lap's per-distance channel arrays from an API /channels doc ----
  function buildLapData(doc, lapNo, lapTime, N, STEP, LAP, SECTORS) {
    const xv = doc.x_values;
    const ch = doc.channels;
    const raw = name => (ch[name] ? ch[name].y : []);

    const speed = resample(xv, raw('Speed'), N);
    for (let i = 0; i < N; i++) if (speed[i] < 3) speed[i] = 3; // avoid divide-by-zero in t
    smooth(speed, 1);
    const thr = resample(xv, raw('Throttle'), N);
    const brk = resample(xv, raw('Brake'), N);
    const gear = resample(xv, raw('Gear'), N);
    const rpm = resample(xv, raw('RPM'), N);
    const steerRad = resample(xv, raw('SteeringWheelAngle'), N);
    const latA = resample(xv, raw('LatAccel'), N);
    const lonA = resample(xv, raw('LongAccel'), N);

    const dist = new Float32Array(N);
    const t = new Float32Array(N);
    const steer = new Float32Array(N);
    const latG = new Float32Array(N);
    const lonG = new Float32Array(N);
    t[0] = 0;
    for (let i = 0; i < N; i++) {
      dist[i] = i * STEP;
      if (i > 0) {
        const vAvg = (speed[i - 1] + speed[i]) * 0.5 || 1;
        t[i] = t[i - 1] + STEP / vAvg;
      }
      steer[i] = clamp(steerRad[i] / STEER_FULL, -1, 1);
      latG[i] = clamp(latA[i] / G_ACCEL, -3, 3);
      lonG[i] = clamp(lonA[i] / G_ACCEL, -2.6, 1.9);
    }
    smooth(thr, 1); smooth(brk, 1);

    // Rescale integrated time so the lap sums to the store's recorded lap_time.
    if (lapTime && t[N - 1] > 0.1) {
      const k = lapTime / t[N - 1];
      for (let i = 0; i < N; i++) t[i] *= k;
    }
    const time = lapTime || t[N - 1];
    const sectors = sectorTimes(t, N, STEP, LAP, SECTORS);
    return { lapNo, time, sectors, data: { dist, speed, t, thr, brk, gear, rpm, steer, latG, lonG } };
  }

  function findCorners(speed, N, STEP, VMAX) {
    const out = [];
    const win = Math.max(4, Math.round(N * 0.006));
    for (let i = win; i < N - win; i++) {
      let isMin = true;
      for (let j = i - win; j <= i + win; j++) if (speed[j] < speed[i] - 0.01) { isMin = false; break; }
      if (isMin && speed[i] < VMAX * 0.82) {
        const d = i * STEP;
        if (!out.length || d - out[out.length - 1].d > 160) out.push({ d, v: speed[i] });
      }
    }
    out.forEach((c, i) => c.label = 'T' + (i + 1));
    return out;
  }

  function buildOptimalLap(stint, N, STEP, LAP, SECTORS) {
    const SEG = Math.max(120, LAP / 20);
    const bounds = [];
    for (let d = 0; d < LAP; d += SEG) bounds.push(d);
    bounds.push(LAP);
    const keys = ['speed', 'thr', 'brk', 'gear', 'rpm', 'steer', 'latG', 'lonG'];
    const out = { dist: new Float32Array(N), t: new Float32Array(N) };
    keys.forEach(k => out[k] = new Float32Array(N));
    for (let i = 0; i < N; i++) out.dist[i] = i * STEP;
    const choice = [];
    let tAccum = 0;
    for (let s = 0; s < bounds.length - 1; s++) {
      const i0 = clamp(Math.round(bounds[s] / STEP), 0, N - 1);
      const i1 = clamp(Math.round(bounds[s + 1] / STEP), 0, N - 1);
      let best = 0, bestDt = Infinity;
      stint.forEach((lap, li) => {
        const dt = lap.data.t[i1] - lap.data.t[i0];
        if (dt < bestDt) { bestDt = dt; best = li; }
      });
      choice.push(best);
      const src = stint[best].data, t0 = src.t[i0];
      for (let i = i0; i < i1; i++) {
        for (const k of keys) out[k][i] = src[k][i];
        out.t[i] = tAccum + (src.t[i] - t0);
      }
      tAccum += bestDt;
    }
    const last = stint[choice[choice.length - 1]].data;
    for (const k of keys) out[k][N - 1] = last[k][N - 1];
    out.t[N - 1] = tAccum;
    return { lapNo: 'OPT', synthetic: true, time: out.t[N - 1], sectors: sectorTimes(out.t, N, STEP, LAP, SECTORS), data: out };
  }

  // ---- API-derived track geometry (a PitSim-compatible object for the map) ----
  function buildTrackGeometry(points, N, STEP, LAP, VMAX) {
    // points: [{lat, lon, lap_dist_pct}] ordered by tick. Resample onto the
    // distance grid so posAt(d) and the index→distance mapping are linear.
    const pts = points.filter(p => p.lat != null && p.lon != null && p.lap_dist_pct != null)
      .sort((a, b) => a.lap_dist_pct - b.lap_dist_pct);
    if (pts.length < 8) return null;
    const lat0 = pts[0].lat;
    const coslat = Math.cos(lat0 * Math.PI / 180);
    const xs = pts.map(p => p.lap_dist_pct);
    const lonM = pts.map(p => (p.lon) * coslat);
    const latM = pts.map(p => -(p.lat));            // flip so north is up on canvas
    const rx = resample(xs, lonM, N);
    const ry = resample(xs, latM, N);
    const raw = [];
    for (let i = 0; i < N; i++) raw.push([rx[i], ry[i]]);
    const posAt = d => raw[clamp(Math.round(d / STEP), 0, N - 1)];
    return {
      TRACK: { raw },
      V_MAX: VMAX,
      LAP_LENGTH_M: LAP,
      SECTOR_BOUNDS: [1 / 3, 2 / 3],
      posAt,
      // sim.js extras some code paths reference — safe no-op-ish stand-ins
      speedAt: () => VMAX,
      curvAt: () => 0,
      headingAt: () => 0,
    };
  }

  // ============================================================
  //  BUILD FROM BACKEND
  // ============================================================
  async function buildFromApi() {
    const sessionsDoc = await getJSON('/sessions?limit=20');
    const sessions = sessionsDoc.sessions || [];
    if (!sessions.length) throw new Error('no sessions');
    // Prefer the demo session, else the most recent.
    const chosen = sessions.find(s => s.session_id === 'demo-sunset-ridge') || sessions[0];
    const sid = chosen.session_id;

    const lapsDoc = await getJSON(`/sessions/${encodeURIComponent(sid)}/laps`);
    let laps = (lapsDoc.laps || []).filter(l => l.lap_time && l.lap_time > 0);
    if (!laps.length) throw new Error('no laps');
    laps.sort((a, b) => a.lap - b.lap);

    // ref = fastest; main = the biggest genuine loss vs ref (skip in/out laps).
    let ref = laps[0];
    for (const l of laps) if (l.lap_time < ref.lap_time) ref = l;
    const candidates = laps.filter(l => l.lap !== ref.lap && !l.is_out_lap && !l.is_in_lap);
    let main = candidates.length ? candidates[0] : laps.find(l => l.lap !== ref.lap) || ref;
    for (const l of candidates) if (l.lap_time > main.lap_time) main = l;

    // Lap length + canonical corners from the deterministic findings.
    let LAP = 0, corners = null;
    try {
      const f = await getJSON(
        `/coaching/lap-findings?session_id=${encodeURIComponent(sid)}&main_lap=${main.lap}&ref_lap=${ref.lap}`);
      LAP = f.lap_length_m || 0;
      corners = (f.corners || []).map(c => ({ d: c.distance, v: (c.min_ref || c.min_main || 0) / 3.6, label: c.label }));
    } catch (e) { /* fall back to detection below */ }

    if (!LAP) {
      // derive lap length from a channel doc's LapDist if findings unavailable
      const probe = await getJSON(
        `/sessions/${encodeURIComponent(sid)}/channels?names=Speed&lap=${ref.lap}&x=lap_dist_pct&max_points=50`);
      LAP = 4000; void probe;
    }

    const N = Math.max(600, Math.min(1600, Math.round(LAP / 4)));
    const STEP = LAP / N;
    const SECTORS = [1 / 3, 2 / 3];

    // Fetch every lap's channels in parallel and build per-distance arrays.
    const names = CHANNELS.join(',');
    const docs = await Promise.all(laps.map(l => getJSON(
      `/sessions/${encodeURIComponent(sid)}/channels?names=${names}&lap=${l.lap}` +
      `&x=lap_dist_pct&mode=minmax&max_points=2000`)));
    const stint = docs.map((doc, i) =>
      buildLapData(doc, laps[i].lap, laps[i].lap_time, N, STEP, LAP, SECTORS));

    let VMAX = 1;
    for (const lap of stint) for (let i = 0; i < N; i++) VMAX = Math.max(VMAX, lap.data.speed[i]);
    VMAX = VMAX * 1.02;

    const refIdx = laps.findIndex(l => l.lap === ref.lap);
    const mainIdx = laps.findIndex(l => l.lap === main.lap);

    if (!corners || !corners.length) {
      corners = findCorners(stint[refIdx].data.speed, N, STEP, VMAX);
    }

    const optimal = buildOptimalLap(stint, N, STEP, LAP, SECTORS);
    stint.push(optimal);
    const optIdx = stint.length - 1;

    // API-derived track for the map (keep sim geometry if trackmap is missing).
    let geom = null;
    try {
      const tm = await getJSON(`/sessions/${encodeURIComponent(sid)}/trackmap?lap=${ref.lap}&max_points=1500`);
      geom = buildTrackGeometry(tm.points || [], N, STEP, LAP, VMAX);
    } catch (e) { /* keep sim geometry */ }
    if (geom) global.PitSim = geom;

    function sampleAtDist(lap, key, d) {
      const arr = lap.data[key];
      const x = clamp(d / STEP, 0, N - 1), i = Math.floor(x);
      return lerp(arr[i], arr[Math.min(N - 1, i + 1)], x - i);
    }

    return {
      LAP, STEP, N, SECTORS, CORNERS: corners,
      stint,
      refIdx: refIdx < 0 ? 0 : refIdx,
      mainIdx: mainIdx < 0 ? Math.min(1, stint.length - 1) : mainIdx,
      optIdx,
      realLaps: () => stint.filter(l => !l.synthetic),
      sampleAtDist,
      timeAtDist: (lap, d) => sampleAtDist(lap, 't', d),
      V_MAX: VMAX,
      sessionId: sid,
      trackName: chosen.track_name || chosen.label || 'Session',
      source: 'api'
    };
  }

  // ---- boot: load the render modules in dependency order ----
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src; s.async = false;
      s.onload = resolve; s.onerror = () => reject(new Error('load ' + src));
      document.body.appendChild(s);
    });
  }

  async function bootModules() {
    for (const src of ['i2graphs.js', 'worksheets.js', 'coach.js', 'i2app.js', 'i2menus.js', 'chat.js']) {
      await loadScript(src);
    }
  }

  async function boot() {
    try {
      global.Laps = await buildFromApi();
      global.RTV = { source: 'api', sessionId: global.Laps.sessionId };
    } catch (e) {
      console.warn('RTV: backend unavailable, using offline simulation.', e);
      global.PitSim = SIM_PITSIM;
      global.Laps = global.LapsSim;
      global.RTV = { source: 'sim', sessionId: null };
    }
    await bootModules();
    window.dispatchEvent(new CustomEvent('rtv:ready', { detail: global.RTV }));
  }

  boot();
})(window);
