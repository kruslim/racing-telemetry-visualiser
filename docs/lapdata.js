/* ============================================================
   lapdata.js — turn the shared track geometry + speed solver
   (sim.js) into a STINT of post-session laps.

   Every channel is sampled uniformly by distance so two laps can
   be overlaid and a time-variance channel computed between them.
   Output is deterministic (seeded) so reloads are stable.
   ============================================================ */
(function (global) {
  'use strict';
  const S = global.PitSim;
  const LAP = S.LAP_LENGTH_M;          // 4100 m
  const STEP = 4;                      // metres per sample
  const N = Math.floor(LAP / STEP) + 1;
  const SECTORS = S.SECTOR_BOUNDS;     // [0.34, 0.68]

  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const lerp = (a, b, t) => a + (b - a) * t;

  // tiny seeded RNG (mulberry32)
  function rng(seed) {
    return function () {
      seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // gaussian bump
  const bump = (p, at, w, amt) => amt * Math.exp(-((p - at) ** 2) / (2 * w * w));

  // ---- build one lap's channels from a per-distance "skill factor" ----
  // mistakes: list of {at(0..1), w, amt} corner-speed deficits
  function buildLap(lapNo, skill, mistakes, seed) {
    const r = rng(seed);
    const dist = new Float32Array(N);
    const speed = new Float32Array(N);   // m/s
    const t = new Float32Array(N);       // elapsed s at this distance
    const thr = new Float32Array(N);
    const brk = new Float32Array(N);
    const gear = new Float32Array(N);
    const rpm = new Float32Array(N);
    const steer = new Float32Array(N);   // -1..1
    const latG = new Float32Array(N);
    const lonG = new Float32Array(N);

    // pass 1: speed from reference profile * factor
    for (let i = 0; i < N; i++) {
      const d = i * STEP;
      dist[i] = d;
      const p = d / LAP;
      let f = skill + 0.004 * Math.sin(d * 0.0011 + seed);
      for (const m of mistakes) f -= bump(p, m.at, m.w, m.amt);
      f = clamp(f, 0.82, 1.02);
      speed[i] = Math.max(14, S.speedAt(d) * f);
    }
    // smooth speed a touch
    smooth(speed, 2);

    // pass 2: integrate time, derive channels from speed + geometry
    t[0] = 0;
    let g = S.gearFor(speed[0]);          // gear is stateful (hysteresis)
    const GT = S.GEAR_TOP;
    for (let i = 0; i < N; i++) {
      const d = dist[i];
      const v = speed[i];
      if (i > 0) {
        const vAvg = (speed[i - 1] + v) * 0.5 || 1;
        t[i] = t[i - 1] + STEP / vAvg;
      }
      // longitudinal accel a = v dv/ds
      const iN = Math.min(N - 1, i + 1), iP = Math.max(0, i - 1);
      const dvds = (speed[iN] - speed[iP]) / ((iN - iP) * STEP || 1);
      const aLong = v * dvds;                       // m/s^2
      lonG[i] = clamp(aLong / 9.81, -2.6, 1.9);

      const k = S.curvAt(d);                          // 1/m
      // signed by turn direction
      const h1 = S.headingAt(d), h2 = S.headingAt(d + 10);
      let dh = h2 - h1;
      while (dh > Math.PI) dh -= 2 * Math.PI;
      while (dh < -Math.PI) dh += 2 * Math.PI;
      const sgn = Math.sign(dh) || 1;
      latG[i] = clamp((v * v * k) / 9.81 * sgn, -3, 3);
      // steering normalised to circuit curvature (hairpin ~ full lock)
      steer[i] = clamp(sgn * (k / 0.020), -1, 1);

      // gearbox with hysteresis -> clean RPM sawtooth (no hunting)
      while (g < 6 && v > GT[g] + 3.0) g++;
      while (g > 1 && v < GT[g - 1] - 3.0) g--;
      gear[i] = g;
      rpm[i] = S.rpmFor(v, g);

      // throttle / brake: WOT on straights, ease in corners, hard brake spikes
      let th, bk;
      if (aLong >= 0) {
        th = clamp(0.6 + aLong * 0.14, 0, 1);
        const latLoad = Math.min(1, Math.abs(latG[i]) / 2.2);
        th = Math.min(th, 1 - latLoad * 0.32);
        if (k < 0.0007) th = 1;                       // straight = full throttle
        bk = 0;
      } else {
        bk = clamp(-aLong * 0.075, 0, 1);
        th = clamp(0.07 - bk * 0.1, 0, 0.07);
      }
      thr[i] = th; brk[i] = bk;
    }
    smooth(thr, 2); smooth(brk, 2); smooth(steer, 1);
    smooth(latG, 1); smooth(lonG, 1);

    const time = t[N - 1];
    const sectors = sectorTimes(t);
    return {
      lapNo, time, sectors,
      data: { dist, speed, t, thr, brk, gear, rpm, steer, latG, lonG }
    };
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

  function sectorTimes(t) {
    const d1 = SECTORS[0] * LAP, d2 = SECTORS[1] * LAP;
    const tAt = d => {
      const x = clamp(d / STEP, 0, N - 1), i = Math.floor(x);
      return lerp(t[i], t[Math.min(N - 1, i + 1)], x - i);
    };
    const a = tAt(d1), b = tAt(d2), c = t[N - 1];
    return [a, b - a, c - b];
  }

  // ---- detect corners from speed minima of the reference lap ----
  function findCorners(speed) {
    const out = [];
    const win = 6;
    for (let i = win; i < N - win; i++) {
      let isMin = true;
      for (let j = i - win; j <= i + win; j++) if (speed[j] < speed[i] - 0.01) { isMin = false; break; }
      if (isMin && speed[i] < S.V_MAX * 0.78) {
        const d = i * STEP;
        if (!out.length || d - out[out.length - 1].d > 180) out.push({ d, v: speed[i] });
      }
    }
    out.forEach((c, i) => c.label = 'T' + (i + 1));
    return out;
  }

  // ============================================================
  //  BUILD THE STINT
  // ============================================================
  // A 12-lap stint: driver warms up, finds the limit, then a flyer,
  // then tyres go off slightly. One lap has a clear mistake (T-late).
  const STINT = [];
  const lapDefs = [
    { no: 7,  skill: 0.948, mist: [{ at: 0.18, w: 0.05, amt: 0.05 }, { at: 0.74, w: 0.04, amt: 0.04 }] },
    { no: 8,  skill: 0.962, mist: [{ at: 0.46, w: 0.05, amt: 0.05 }] },
    { no: 9,  skill: 0.971, mist: [{ at: 0.74, w: 0.045, amt: 0.06 }] },
    { no: 10, skill: 0.980, mist: [{ at: 0.18, w: 0.045, amt: 0.035 }] },
    { no: 11, skill: 0.986, mist: [{ at: 0.60, w: 0.03, amt: 0.02 }] },
    { no: 12, skill: 0.992, mist: [] },                                    // the flyer (reference)
    { no: 13, skill: 0.988, mist: [{ at: 0.40, w: 0.035, amt: -0.024 },    // selected main:
                                   { at: 0.74, w: 0.038, amt: 0.105 },     // big lock-up at T-final
                                   { at: 0.20, w: 0.04,  amt: 0.022 }] },
    { no: 14, skill: 0.978, mist: [{ at: 0.46, w: 0.05, amt: 0.05 }] },
    { no: 15, skill: 0.970, mist: [{ at: 0.34, w: 0.05, amt: 0.045 }, { at: 0.82, w: 0.04, amt: 0.04 }] },
    { no: 16, skill: 0.961, mist: [{ at: 0.55, w: 0.06, amt: 0.06 }] },
  ];
  lapDefs.forEach((d, i) => STINT.push(buildLap(d.no, d.skill, d.mist, 1000 + i * 7)));

  // pick reference (fastest) + default main (the lap-13 mistake lap)
  let refIdx = 0;
  STINT.forEach((l, i) => { if (l.time < STINT[refIdx].time) refIdx = i; });
  let mainIdx = STINT.findIndex(l => l.lapNo === 13);
  if (mainIdx < 0) mainIdx = Math.min(STINT.length - 1, refIdx + 1);

  const CORNERS = findCorners(STINT[refIdx].data.speed);

  // ---- THEORETICAL BEST LAP ----------------------------------------------
  // Split the lap into ~200 m mini-sectors. For each, find which real lap was
  // quickest through that stretch and stitch its channels in, re-integrating
  // elapsed time as the sum of the best mini-sector times. The result is a
  // valid, selectable reference that is faster than any single lap actually
  // driven — the "perfect" lap assembled from the driver's own best bits.
  function buildOptimalLap(stint) {
    const SEG = 200;
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
    return { lapNo: 'OPT', synthetic: true, time: out.t[N - 1], sectors: sectorTimes(out.t), data: out };
  }
  const OPTIMAL = buildOptimalLap(STINT);
  STINT.push(OPTIMAL);          // appended last; real laps keep indices 0..n-1
  const OPT_IDX = STINT.length - 1;

  // ---- interpolation helpers ----
  function sampleAtDist(lap, key, d) {
    const data = lap.data, arr = data[key];
    const x = clamp(d / STEP, 0, N - 1), i = Math.floor(x);
    return lerp(arr[i], arr[Math.min(N - 1, i + 1)], x - i);
  }
  // elapsed time of a lap at a distance
  function timeAtDist(lap, d) { return sampleAtDist(lap, 't', d); }

  // Published as LapsSim — the offline simulated fallback. api.js sets window.Laps
  // from the real backend when it is reachable, and falls back to this otherwise.
  global.LapsSim = {
    LAP, STEP, N, SECTORS, CORNERS,
    stint: STINT,
    refIdx, mainIdx, optIdx: OPT_IDX,
    realLaps: () => STINT.filter(l => !l.synthetic),
    sampleAtDist, timeAtDist,
    V_MAX: S.V_MAX,
    sessionId: null,
    source: 'sim'
  };
})(window);
