/**
 * The review's static mode: read an exported session from JSON files instead of the API.
 *
 *     /review/?static=../data/spa/
 *
 * scripts/export_session.py writes those files from a stored session by calling the
 * same REST endpoints this page calls, so a static page shows exactly what the live
 * API would. staticPlan() maps one API request to the file(s) that hold its answer;
 * the layout here and the exporter's must agree (tests/test_export_session.py and
 * review-model.test.js both pin it).
 *
 * Pure: no DOM, no fetch.
 */

/** The export folder named by `?static=`, or null. Only a relative path is accepted. */
export function staticBase(value) {
  if (!value) return null;
  const v = String(value).trim();
  // No scheme, no protocol-relative or absolute URL, no backslashes: the page only
  // ever reads files published next to it.
  if (!v || /^[a-z][a-z0-9+.-]*:/i.test(v) || v.startsWith('/') || v.includes('\\')) return null;
  return v.endsWith('/') ? v : `${v}/`;
}

const seg = (s) => encodeURIComponent(String(s));

/**
 * The files that answer one API request (path relative to /api/v1, with its query).
 *
 * Returns { file } for a one-to-one payload, { compare: { name, files, laps } } for a
 * lap comparison assembled from one file per lap, { state: { file, t } } for the
 * race state at a cursor (picked client-side from the run's snapshots), or null
 * when the export cannot answer it (the page then behaves as if the API failed).
 */
export function staticPlan(path) {
  const [route, query = ''] = String(path).split('?');
  const p = new URLSearchParams(query);
  const parts = route.split('/').filter(Boolean);

  if (route === '/sessions') return { file: 'sessions.json' };

  if (parts[0] === 'coaching' && (parts[1] === 'lap-findings' || parts[1] === 'delta')) {
    const sid = p.get('session_id');
    const main = p.get('main_lap');
    const ref = p.get('ref_lap');
    if (!sid || main == null || ref == null) return null;
    return { file: `sessions/${seg(sid)}/${parts[1]}/${seg(main)}-${seg(ref)}.json` };
  }

  if (parts[0] !== 'sessions' || parts.length < 3) return null;
  const sid = parts[1];
  const dir = `sessions/${seg(sid)}`;
  const tail = parts.slice(2).join('/');
  if (tail === 'laps') return { file: `${dir}/laps.json` };
  if (tail === 'pitwall-runs') return { file: `${dir}/pitwall-runs.json` };
  if (tail === 'summary') return { file: `${dir}/summary/${seg(p.get('run') || 'none')}.json` };
  if (tail === 'pitwall-log') {
    const run = p.get('run');
    return run ? { file: `${dir}/pitwall-log/${seg(run)}.json` } : null;
  }
  if (tail === 'pitwall-log/state') {
    const run = p.get('run');
    const t = Number(p.get('t'));
    if (!run || !Number.isFinite(t)) return null;
    return { state: { file: `${dir}/pitwall-log/${seg(run)}-state.json`, t } };
  }
  if (tail === 'trackmap') {
    const lap = p.get('lap');
    return lap == null ? null : { file: `${dir}/trackmap/${seg(lap)}.json` };
  }
  if (tail === 'compare') {
    const name = p.get('name');
    const laps = (p.get('laps') || '').split(',').map((s) => s.trim()).filter(Boolean);
    if (!name || !laps.length) return null;
    const unique = [...new Set(laps)];
    return {
      compare: {
        name,
        laps: unique,
        files: unique.map((lap) => `${dir}/compare/${seg(name)}/${seg(lap)}.json`),
      },
    };
  }
  return null;
}

/** One /compare payload from the per-lap payloads (same grid, so same x_values). */
export function mergeCompare(payloads) {
  const usable = payloads.filter((b) => b && b.laps);
  if (!usable.length) return null;
  const out = { ...usable[0], laps: {} };
  for (const body of usable) Object.assign(out.laps, body.laps);
  return out;
}

/**
 * The state snapshot at (or just before) session time t, else the first one:
 * the same rule as rtv.store.journal.state_at, which serves /pitwall-log/state.
 */
export function stateAt(records, t) {
  let best = null;
  let first = null;
  for (const rec of records || []) {
    if (!rec || rec.kind !== 'state') continue;
    if (first === null) first = rec;
    const st = rec.session_time;
    if (st != null && st <= t && (best === null || st >= (best.session_time || 0))) best = rec;
  }
  return best || first;
}
