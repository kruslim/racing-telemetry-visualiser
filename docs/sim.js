/* ============================================================
   sim.js — coherent GT3 endurance telemetry simulation
   A racing-line speed solver drives every channel from one
   track geometry, so all readouts stay physically correlated.
   ============================================================ */
(function (global) {
  'use strict';

  // ---- track waypoints (stylised circuit, normalised box ~1000x600) ----
  const WAYPOINTS = [
    [130, 470],[128, 250],[165, 150],[250, 105],[360, 120],[430, 205],
    [520, 240],[600, 170],[700, 120],[820, 140],[895, 235],[862, 350],
    [742, 372],[690, 300],[612, 320],[640, 430],[760, 470],[800, 540],
    [610, 560],[420, 548],[300, 560],[185, 545]
  ];
  const SECTOR_BOUNDS = [0.34, 0.68]; // lap% sector splits

  // Catmull-Rom closed spline -> dense sample
  function buildTrack(pts, N) {
    const n = pts.length;
    const cr = (p0, p1, p2, p3, t) => {
      const t2 = t * t, t3 = t2 * t;
      return [
        0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t + (2*p0[0]-5*p1[0]+4*p2[0]-p3[0]) * t2 + (-p0[0]+3*p1[0]-3*p2[0]+p3[0]) * t3),
        0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t + (2*p0[1]-5*p1[1]+4*p2[1]-p3[1]) * t2 + (-p0[1]+3*p1[1]-3*p2[1]+p3[1]) * t3)
      ];
    };
    const samplesPerSeg = Math.ceil(N / n);
    const raw = [];
    for (let i = 0; i < n; i++) {
      const p0 = pts[(i - 1 + n) % n], p1 = pts[i], p2 = pts[(i + 1) % n], p3 = pts[(i + 2) % n];
      for (let s = 0; s < samplesPerSeg; s++) raw.push(cr(p0, p1, p2, p3, s / samplesPerSeg));
    }
    // arc length + curvature
    const M = raw.length;
    const seg = new Array(M), cum = new Array(M);
    let total = 0;
    for (let i = 0; i < M; i++) {
      const a = raw[i], b = raw[(i + 1) % M];
      const d = Math.hypot(b[0] - a[0], b[1] - a[1]);
      seg[i] = d; cum[i] = total; total += d;
    }
    const curv = new Array(M);
    for (let i = 0; i < M; i++) {
      const a = raw[(i - 2 + M) % M], b = raw[i], c = raw[(i + 2) % M];
      const a1 = Math.atan2(b[1] - a[1], b[0] - a[0]);
      const a2 = Math.atan2(c[1] - b[1], c[0] - b[0]);
      let da = a2 - a1;
      while (da > Math.PI) da -= 2 * Math.PI;
      while (da < -Math.PI) da += 2 * Math.PI;
      const dist = (seg[(i - 1 + M) % M] + seg[i]) || 1;
      curv[i] = Math.abs(da) / dist;
    }
    return { raw, seg, cum, curv, total, M };
  }

  const TRACK = buildTrack(WAYPOINTS, 720);
  // scale geometry to "real" metres: assume ~4.1 km lap
  const LAP_LENGTH_M = 4100;
  const M2PX = TRACK.total / LAP_LENGTH_M; // px per metre

  // ---- speed profile via forward/backward passes ----
  const V_MAX = 79;       // m/s ~ 284 km/h
  const V_MIN = 19;       // m/s ~ 68 km/h (slowest hairpin)
  const A_LAT = 17.5;     // m/s^2 lateral grip
  const A_BRAKE = 26;     // m/s^2
  const A_ACCEL = 11;     // m/s^2
  function buildSpeed() {
    const M = TRACK.M, v = new Array(M);
    for (let i = 0; i < M; i++) {
      const k = TRACK.curv[i] / M2PX; // curvature per metre
      v[i] = k > 1e-5 ? Math.min(V_MAX, Math.sqrt(A_LAT / k)) : V_MAX;
      v[i] = Math.max(V_MIN, v[i]);
    }
    const dsM = i => TRACK.seg[i] / M2PX;
    for (let p = 0; p < 2; p++) {
      for (let i = M - 1; i >= 0; i--) { // backward (braking)
        const j = (i + 1) % M, ds = dsM(i);
        v[i] = Math.min(v[i], Math.sqrt(v[j] * v[j] + 2 * A_BRAKE * ds));
      }
      for (let i = 0; i < M; i++) { // forward (accel)
        const j = (i - 1 + M) % M, ds = dsM(j);
        v[i] = Math.min(v[i], Math.sqrt(v[j] * v[j] + 2 * A_ACCEL * ds));
      }
    }
    // smooth
    const out = v.slice();
    for (let i = 0; i < M; i++) {
      out[i] = (v[(i-1+M)%M] + 2*v[i] + v[(i+1)%M]) / 4;
    }
    return out;
  }
  const VPROF = buildSpeed();

  // ---- helpers ----
  const lerp = (a, b, t) => a + (b - a) * t;
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  // sample by arc length (metres) -> index fraction
  function atDist(distM) {
    const distPx = (distM % LAP_LENGTH_M) * M2PX;
    const M = TRACK.M;
    let lo = 0, hi = M - 1;
    // linear-ish search on cum (monotonic)
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (TRACK.cum[mid] < distPx) lo = mid + 1; else hi = mid;
    }
    const i = (lo - 1 + M) % M;
    const segStart = TRACK.cum[i], segLen = TRACK.seg[i] || 1;
    const f = clamp((distPx - segStart) / segLen, 0, 1);
    return { i, f };
  }
  function speedAt(distM) {
    const { i, f } = atDist(distM);
    return lerp(VPROF[i], VPROF[(i + 1) % TRACK.M], f);
  }
  function posAt(distM) {
    const { i, f } = atDist(distM);
    const a = TRACK.raw[i], b = TRACK.raw[(i + 1) % TRACK.M];
    return [lerp(a[0], b[0], f), lerp(a[1], b[1], f)];
  }
  function headingAt(distM) {
    const a = posAt(distM), b = posAt(distM + 6);
    return Math.atan2(b[1] - a[1], b[0] - a[0]);
  }
  function curvAt(distM) {
    const { i, f } = atDist(distM);
    return lerp(TRACK.curv[i], TRACK.curv[(i + 1) % TRACK.M], f) / M2PX;
  }

  // ---- gearbox (6-speed sequential) ----
  const GEAR_TOP = [0, 13.8, 19.6, 25.8, 32.4, 39.0, 79.5]; // m/s top of each gear
  const RPM_IDLE = 3200, RPM_MAX = 7900, RPM_SHIFT = 7450;
  function gearFor(v) {
    for (let g = 1; g <= 6; g++) if (v <= GEAR_TOP[g] + 0.01) return g;
    return 6;
  }
  function rpmFor(v, g) {
    const lo = GEAR_TOP[g - 1], hi = GEAR_TOP[g];
    const t = clamp((v - lo) / (hi - lo), 0, 1);
    return RPM_IDLE + (RPM_MAX - RPM_IDLE) * (0.30 + 0.70 * t);
  }

  // ---- standings field ----
  const DRIVERS = [
    'M. HOFFMANN','L. CHEN','D. ROSSI','K. NAKAMURA','R. LIM','S. ANDERSSON',
    'P. DUBOIS','T. OKAFOR','V. PETROV','E. SANTOS','J. MÜLLER','A. KOWALSKI',
    'F. BIANCHI','C. MENDOZA','N. HAAKONSEN','B. TANAKA','G. SILVA','H. KIM',
    'O. BERGSTRÖM','W. ASHFORD','Y. MORALES','Z. NOVAK','I. KOVAC','Q. RIVERA'
  ];
  const TEAM_CARS = [3,77,9,22,7,44,18,5,55,11,27,8,99,14,2,33,21,6,88,4,16,71,12,50];

  function buildField() {
    const field = DRIVERS.map((nm, i) => ({
      idx: i, name: nm, car: TEAM_CARS[i], me: i === 4,
      pos: i + 1,
      pace: 106.8 + i * 0.16 + (Math.random() * 0.5 - 0.25), // base laptime s
      gapToLeader: null,
      dist: 0,   // TOTAL race distance (metres)
      last: 0
    }));
    // grid: order by pace, assign cumulative time-gaps to the car ahead
    const avgV = LAP_LENGTH_M / 107;
    const order = field.slice().sort((a, b) => a.pace - b.pace);
    let cum = 0;
    order.forEach((c, rank) => { if (rank > 0) cum += 1.0 + Math.random() * 1.4; c._gap0 = cum; });
    // anchor everyone around the player's starting total distance (lap 14 + 1640 m)
    const player = field[4];
    const playerRD = 14 * LAP_LENGTH_M + 1640;
    const leaderRD = playerRD + player._gap0 * avgV;
    field.forEach(c => { c.dist = leaderRD - c._gap0 * avgV; c.last = c.pace + (Math.random() * 0.4 - 0.2); });
    return field;
  }

  // ============================================================
  //  STATE MACHINE
  // ============================================================
  function createSim() {
    const F = buildField();
    const me = F[4];

    const st = {
      t: 0,                 // sim seconds elapsed
      dist: 1640,           // metres along current lap (start mid-track)
      lap: 14, totalLaps: 58,
      sessionRemain: 102 * 60, // s
      // dynamic channels
      speed: 60, gear: 4, rpm: 6000, throttle: 1, brake: 0, clutch: 0, steer: 0,
      latG: 0, longG: 0, peakG: 1.4,
      fuel: 71.5, fuelPerLap: 3.41, fuelCap: 105,
      tankPct: 0.68,
      waterTemp: 87, oilTemp: 110, oilPress: 4.7, manifold: 1.0, voltage: 13.8,
      fuelPress: 4.4, fuelRate: 64,
      airTemp: 24.6, trackTemp: 37.5, humidity: 52, windVel: 3.1, windDir: 0.8,
      // tyres: [temp L,M,R], pressure, wear[L,M,R], brakeTemp
      tyres: makeTyres(),
      brakeBias: 54.0,
      // lap timing
      curLapTime: 0, lastLapTime: 107.9, bestLapTime: 107.61, optimalTime: 107.02,
      delta: -0.31, sector: 0, sectorTimes: [0, 0, 0],
      bestSectors: [33.9, 41.2, 32.5], curSectors: [0, 0, 0],
      stintLap: 6, tyreAge: 6, lastStop: 8,
      lapHistory: seedHistory(),
      field: F, me,
      flag: 'green',
      warnings: { abs: false, tc: false, pit: false, limit: false, eng: false },
      // trace ring buffer
      trace: [], traceMax: 60 * 14,
      flashShift: 0,
      gridReady: true
    };
    seedTyrePos(st);
    recomputeStandings(st);

    function makeTyres() {
      const mk = (base) => ({
        temp: [base - 4, base, base - 6], // L M R carcass
        surf: [base + 6, base + 10, base + 3],
        press: 0, wear: [1, 1, 1], brake: 280, base
      });
      return {
        LF: mk(86), RF: mk(89), LR: mk(83), RR: mk(85)
      };
    }
    function seedTyrePos(s) {
      s.tyres.LF.press = 199; s.tyres.RF.press = 203;
      s.tyres.LR.press = 196; s.tyres.RR.press = 200;
      const w = { LF: .86, RF: .82, LR: .88, RR: .84 };
      for (const k in w) s.tyres[k].wear = [w[k] + .02, w[k], w[k] - .03];
    }
    function seedHistory() {
      const h = [];
      const base = 108.6;
      for (let i = 0; i < 7; i++) {
        const lp = 7 + i;
        const t = base - i * 0.16 + (Math.random() * 0.5 - 0.2);
        h.push({
          lap: lp, time: t,
          s: [t * 0.315, t * 0.382, t * 0.303],
          fuel: 3.3 + Math.random() * 0.3,
          best: false
        });
      }
      return h;
    }

    // -------- per-frame update --------
    function update(dt) {
      if (dt <= 0) return st;
      st.t += dt;
      st.sessionRemain = Math.max(0, st.sessionRemain - dt);

      // advance player along racing line
      const v = speedAt(st.dist);
      // small driver noise so it's not robotic
      const vEff = v * (0.985 + 0.015 * Math.sin(st.t * 0.7));
      const prevDist = st.dist;
      st.dist += vEff * dt;
      st.curLapTime += dt;

      // lap rollover
      if (st.dist >= LAP_LENGTH_M) {
        st.dist -= LAP_LENGTH_M;
        onLapComplete();
      }

      // ---- derive channels ----
      const vAhead = speedAt(st.dist + 14);
      const prevSpeed = st.speed;
      st.speed = lerp(st.speed, vEff, clamp(dt * 8, 0, 1));
      // real longitudinal acceleration = change in speed over elapsed time
      const accelLong = (st.speed - prevSpeed) / Math.max(dt, 0.004);

      st.gear = gearFor(st.speed);
      const targetRpm = rpmFor(st.speed, st.gear);
      st.rpm = lerp(st.rpm, targetRpm, clamp(dt * 9, 0, 1));

      // throttle / brake from whether next section demands accel or decel
      const demand = (vAhead - vEff); // m/s difference over lookahead
      let thr, brk;
      if (demand >= -0.4) { thr = clamp(0.45 + demand * 0.5, 0, 1); brk = 0; }
      else { brk = clamp(-demand * 0.16, 0, 1); thr = clamp(0.12 - brk * 0.2, 0, 0.12); }
      // full throttle on straights
      if (curvAt(st.dist) < 0.0006 && demand > -0.1) thr = 1;
      st.throttle = lerp(st.throttle, thr, clamp(dt * 12, 0, 1));
      st.brake = lerp(st.brake, brk, clamp(dt * 14, 0, 1));
      st.clutch = st.brake > 0.55 && st.speed < 28 ? clamp(st.clutch + dt * 6, 0, 1) : clamp(st.clutch - dt * 6, 0, 1);

      // steering from curvature (signed by turn direction)
      const h1 = headingAt(st.dist), h2 = headingAt(st.dist + 10);
      let dh = h2 - h1;
      while (dh > Math.PI) dh -= 2 * Math.PI;
      while (dh < -Math.PI) dh += 2 * Math.PI;
      const k = curvAt(st.dist);
      const steerTgt = clamp(Math.sign(dh) * Math.min(1, k * 950), -1, 1);
      st.steer = lerp(st.steer, steerTgt, clamp(dt * 8, 0, 1));

      // G-forces
      st.latG = lerp(st.latG, (st.speed * st.speed * k) / 9.81 * Math.sign(dh || 1), clamp(dt * 9, 0, 1));
      st.longG = lerp(st.longG, clamp(accelLong / 9.81, -2.4, 1.7), clamp(dt * 6, 0, 1));
      const gMag = Math.hypot(st.latG, st.longG);
      if (gMag > st.peakG) st.peakG = gMag;

      // ---- tyres respond to load ----
      const load = Math.abs(st.latG) * 0.6 + st.brake * 0.5 + st.throttle * 0.25;
      updateTyres(dt, load);

      // ---- engine vitals drift ----
      const rpmFrac = (st.rpm - RPM_IDLE) / (RPM_MAX - RPM_IDLE);
      st.waterTemp = approach(st.waterTemp, 84 + rpmFrac * 10 + load * 3, dt, 0.4);
      st.oilTemp = approach(st.oilTemp, 106 + rpmFrac * 14 + load * 4, dt, 0.3);
      st.oilPress = approach(st.oilPress, 3.6 + rpmFrac * 2.4, dt, 2);
      st.manifold = approach(st.manifold, 0.35 + st.throttle * 1.0, dt, 6);
      st.voltage = approach(st.voltage, 13.6 + rpmFrac * 0.6, dt, 1.5);
      st.fuelRate = approach(st.fuelRate, 18 + st.throttle * rpmFrac * 90, dt, 4);
      st.fuelPress = approach(st.fuelPress, 4.2 + st.throttle * 0.5, dt, 3);

      // fuel burn (proportional to distance covered this frame)
      st.fuel = Math.max(0, st.fuel - st.fuelPerLap * (vEff * dt / LAP_LENGTH_M));
      st.tankPct = st.fuel / st.fuelCap;

      // delta to best: a believable rolling gain/loss vs best lap, bounded
      const lapFrac = st.dist / LAP_LENGTH_M;
      const wobble = Math.sin(st.dist * 0.0021) * 0.34 + Math.sin(st.dist * 0.00072 + 1.3) * 0.16;
      st.delta = approach(st.delta, wobble, dt, 0.9);

      // sectors
      const sec = lapFrac < SECTOR_BOUNDS[0] ? 0 : lapFrac < SECTOR_BOUNDS[1] ? 1 : 2;
      if (sec !== st.sector) { st.sector = sec; }
      st.curSectors[st.sector] = st.curLapTime - (st.sector > 0 ? sectorStartTime(st) : 0);

      // warnings
      st.warnings.abs = st.brake > 0.82;
      st.warnings.tc = st.throttle > 0.7 && st.speed < 35 && Math.abs(st.latG) > 1.0;
      st.warnings.limit = st.rpm > RPM_SHIFT;
      st.warnings.eng = st.oilTemp > 128 || st.waterTemp > 105;
      st.warnings.pit = false;

      // shift flash
      st.flashShift = clamp((st.rpm - 6600) / (RPM_SHIFT - 6600), 0, 1);

      // standings drift
      stepField(dt);

      // ---- trace buffer ----
      st.trace.push({ thr: st.throttle, brk: st.brake, spd: st.speed / V_MAX, steer: st.steer });
      if (st.trace.length > st.traceMax) st.trace.shift();

      // clock
      return st;
    }

    function sectorStartTime(s) {
      // approximate sector start as fraction of best
      if (s.sector === 1) return s.bestSectors[0] * (s.curLapTime / Math.max(s.bestLapTime, 1)) + 0;
      if (s.sector === 2) return (s.bestSectors[0] + s.bestSectors[1]);
      return 0;
    }

    function updateTyres(dt, load) {
      for (const k in st.tyres) {
        const ty = st.tyres[k];
        const right = k[1] === 'F'; // front bears more brake; outer loads vary
        const targetBase = 84 + load * 22 + (right ? 3 : 0);
        // each band L/M/R reacts slightly differently w/ camber
        for (let b = 0; b < 3; b++) {
          const bandBias = [b === 0 ? 1.5 : 0, 4, b === 2 ? -1 : 0][b] || 0;
          const tgt = targetBase + bandBias + (b === 1 ? 4 : 0);
          ty.temp[b] = approach(ty.temp[b], tgt, dt, 0.25);
          ty.surf[b] = approach(ty.surf[b], tgt + 8 + load * 6, dt, 1.2);
        }
        ty.press = approach(ty.press, 196 + (ty.temp[1] - 84) * 0.9, dt, 0.8);
        ty.brake = approach(ty.brake, 220 + st.brake * 560 + load * 80, dt, 1.5);
        // slow wear
        const wr = (load * 0.0000026 + 0.0000004);
        ty.wear[0] = Math.max(0, ty.wear[0] - wr * 1.1);
        ty.wear[1] = Math.max(0, ty.wear[1] - wr);
        ty.wear[2] = Math.max(0, ty.wear[2] - wr * 0.9);
      }
    }

    function onLapComplete() {
      st.lastLapTime = st.curLapTime;
      const lapFuel = st.fuelPerLap * (0.95 + Math.random() * 0.1);
      const entry = {
        lap: st.lap,
        time: st.curLapTime,
        s: st.curSectors.slice(),
        fuel: lapFuel,
        best: st.curLapTime < st.bestLapTime
      };
      if (st.curLapTime < st.bestLapTime) {
        st.bestLapTime = st.curLapTime;
        for (let i = 0; i < 3; i++) st.bestSectors[i] = Math.min(st.bestSectors[i], st.curSectors[i] || st.bestSectors[i]);
        st.optimalTime = st.bestSectors[0] + st.bestSectors[1] + st.bestSectors[2];
      }
      st.lapHistory.push(entry);
      if (st.lapHistory.length > 8) st.lapHistory.shift();
      st.lap++;
      st.stintLap++;
      st.tyreAge++;
      st.curLapTime = 0;
      st.curSectors = [0, 0, 0];
      st.fuelPerLap = lerp(st.fuelPerLap, lapFuel, 0.3);
      st.peakG = Math.hypot(st.latG, st.longG); // reset peak to this lap
      me.lap = st.lap;
      me.last = st.lastLapTime;
    }

    // -------- field simulation --------
    function stepField(dt) {
      for (const c of st.field) {
        if (c.me) { c.dist = st.dist + (st.lap) * LAP_LENGTH_M; c.last = st.lastLapTime; continue; }
        const v = LAP_LENGTH_M / c.pace; // avg m/s
        c.dist += v * dt * (0.98 + 0.04 * Math.sin(st.t * 0.5 + c.idx));
      }
      recomputeStandings(st);
    }

    return { state: st, update };
  }

  function recomputeStandings(st) {
    const arr = st.field.slice().sort((a, b) => b.dist - a.dist);
    const leadDist = arr[0].dist;
    arr.forEach((c, i) => {
      c.pos = i + 1;
      const dm = leadDist - c.dist;
      const avgV = LAP_LENGTH_M / c.pace;
      c.gapToLeader = dm / avgV; // seconds
    });
    st.standings = arr;
    const meRow = arr.find(c => c.me);
    st.me.pos = meRow ? meRow.pos : 5;
  }

  function approach(cur, tgt, dt, rate) {
    return lerp(cur, tgt, clamp(dt * rate, 0, 1));
  }

  // expose geometry for the map panel
  createSim.TRACK = TRACK;
  createSim.SECTOR_BOUNDS = SECTOR_BOUNDS;
  createSim.LAP_LENGTH_M = LAP_LENGTH_M;
  createSim.posAt = posAt;
  createSim.speedAt = speedAt;
  createSim.curvAt = curvAt;
  createSim.headingAt = headingAt;
  createSim.gearFor = gearFor;
  createSim.rpmFor = rpmFor;
  createSim.GEAR_TOP = GEAR_TOP;
  createSim.VPROF = VPROF;
  createSim.V_MAX = V_MAX;
  createSim.RPM_MAX = RPM_MAX;
  createSim.RPM_SHIFT = RPM_SHIFT;
  createSim.RPM_IDLE = RPM_IDLE;

  global.PitSim = createSim;
})(window);
