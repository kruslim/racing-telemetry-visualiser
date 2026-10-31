/**
 * The shared view golden file, run against the JS view models.
 *
 *     node frontend/js/run-golden-tests.mjs            check every case
 *     node frontend/js/run-golden-tests.mjs --emit F   print raw outputs for cases file F
 *
 * `tests/fixtures/view_golden/*.json` holds `payload -> expected view fields`
 * cases built from the Spa example payloads (scripts/build_view_golden.py). The
 * Python ports in `src/rtv/desktop/model/` run the same file in
 * tests/test_view_golden.py, so a rule that drifts on one side fails there or
 * here. pytest runs this when node is installed.
 *
 * Outputs are compared in a neutral shape: keys snake_case, Maps as lists of
 * their values, undefined as null. An expected object lists the fields both
 * sides share; a side may carry extra fields of its own.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import * as format from './pitwall/format.js';
import * as viewModel from './pitwall/view-model.js';
import * as activity from './shared/activity-model.js';
import * as review from './review/review-model.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = resolve(HERE, '../../tests/fixtures/view_golden');
const MODULES = { format, view_model: viewModel, activity, review };

const camel = (key) => key.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase());
const snake = (key) => key.replace(/([a-z0-9])([A-Z])/g, (_, a, b) => `${a}_${b.toLowerCase()}`);

/** Turn the golden file's argument markers into real JS arguments. */
function resolveArg(arg, inputs) {
  if (Array.isArray(arg)) return arg.map((a) => resolveArg(a, inputs));
  if (!arg || typeof arg !== 'object') return arg;
  if ('$input' in arg) {
    const value = inputs[arg.$input];
    return structuredClone('index' in arg ? value[arg.index] : value);
  }
  if ('$auditRows' in arg) return review.auditRows(resolveArg(arg.$auditRows, inputs));
  if ('$opts' in arg) {
    const out = {};
    for (const [k, v] of Object.entries(arg.$opts)) out[camel(k)] = resolveArg(v, inputs);
    return out;
  }
  if ('$model' in arg) {
    return activity.reduce(resolveArg(arg.$model, inputs), arg.up_to ?? null);
  }
  if ('$trends' in arg) {
    const tracker = new viewModel.GapTrendTracker();
    for (const state of resolveArg(arg.$trends, inputs)) tracker.update(state);
    return tracker;
  }
  if ('$warnings' in arg) {
    const warnings = new viewModel.TowerWarnings();
    for (const event of resolveArg(arg.$warnings.events, inputs)) warnings.observe(event);
    return warnings.clock(arg.$warnings.clock);
  }
  const out = {};
  for (const [k, v] of Object.entries(arg)) out[k] = resolveArg(v, inputs);
  return out;
}

function call(testCase, inputs) {
  const mod = MODULES[testCase.module];
  const args = resolveArg(testCase.args || [], inputs);
  if (testCase.kwargs) args.push(resolveArg({ $opts: testCase.kwargs }, inputs));
  if (testCase.fn === 'FuelHistory.points') {
    const history = new viewModel.FuelHistory();
    for (const state of args[0]) history.update(state);
    return history.points();
  }
  const fn = mod[camel(testCase.fn)];
  if (typeof fn !== 'function') throw new Error(`${testCase.module}.${testCase.fn} is not exported`);
  return fn(...args);
}

/** The neutral shape both runners compare in. */
export function neutral(value) {
  if (value === undefined || value === null) return null;
  if (value instanceof Map) return [...value.values()].map(neutral);
  if (Array.isArray(value)) return value.map(neutral);
  if (typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (typeof v === 'function') continue;
      out[snake(k)] = neutral(v);
    }
    return out;
  }
  return value;
}

/** Where `actual` departs from `expected`, or null. Extra actual keys are allowed. */
export function mismatch(expected, actual, path = '$') {
  if (typeof expected === 'number' && typeof actual === 'number') {
    return Math.abs(expected - actual) <= 1e-9 * Math.max(1, Math.abs(expected))
      ? null
      : `${path}: expected ${expected}, got ${actual}`;
  }
  if (Array.isArray(expected)) {
    if (!Array.isArray(actual)) return `${path}: expected a list, got ${JSON.stringify(actual)}`;
    if (expected.length !== actual.length) {
      return `${path}: expected ${expected.length} items, got ${actual.length}`;
    }
    for (let i = 0; i < expected.length; i += 1) {
      const m = mismatch(expected[i], actual[i], `${path}[${i}]`);
      if (m) return m;
    }
    return null;
  }
  if (expected && typeof expected === 'object') {
    if (!actual || typeof actual !== 'object' || Array.isArray(actual)) {
      return `${path}: expected an object, got ${JSON.stringify(actual)}`;
    }
    for (const key of Object.keys(expected)) {
      if (!(key in actual)) return `${path}.${key}: missing`;
      const m = mismatch(expected[key], actual[key], `${path}.${key}`);
      if (m) return m;
    }
    return null;
  }
  return expected === actual
    ? null
    : `${path}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`;
}

function loadGolden(dir) {
  const inputs = JSON.parse(readFileSync(join(dir, 'inputs.json'), 'utf8'));
  const files = readdirSync(dir).filter((f) => f.endsWith('.json') && f !== 'inputs.json');
  return { inputs, files: files.sort().map((f) => [f, JSON.parse(readFileSync(join(dir, f), 'utf8'))]) };
}

function emit(casesPath) {
  const { inputs, cases } = JSON.parse(readFileSync(casesPath, 'utf8'));
  const out = {};
  for (const testCase of cases) {
    try {
      out[testCase.name] = { ok: true, value: neutral(call(testCase, inputs)) };
    } catch (err) {
      out[testCase.name] = { ok: false, error: String(err && err.stack ? err.stack : err) };
    }
  }
  process.stdout.write(JSON.stringify(out));
}

function check(dir) {
  const { inputs, files } = loadGolden(dir);
  let total = 0;
  let failed = 0;
  for (const [file, golden] of files) {
    for (const testCase of golden.cases) {
      total += 1;
      let problem;
      try {
        problem = mismatch(testCase.expect, neutral(call(testCase, inputs)));
      } catch (err) {
        problem = `threw ${err}`;
      }
      if (problem) failed += 1;
      console.log(`${problem ? 'FAIL' : 'PASS'}  ${file}: ${testCase.name}`);
      if (problem) console.log(`      ${problem}`);
    }
  }
  console.log(`\n${total - failed}/${total} passed.`);
  return failed === 0 && total > 0;
}

const argv = process.argv.slice(2);
if (argv[0] === '--emit') emit(argv[1]);
else process.exit(check(argv[0] ? resolve(argv[0]) : GOLDEN) ? 0 : 1);
