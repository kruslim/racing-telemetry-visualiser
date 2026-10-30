/**
 * Headless runner for the pitwall view-model tests.
 *
 *     node frontend/js/run-pitwall-tests.mjs
 *
 * Same deal as run-audio-tests.mjs: node plus ES modules, no package.json, no
 * dependencies, no config. The same module is rendered in-page by
 * frontend/pitwall-test.html, and pytest runs this file when node happens to be
 * installed (see tests/test_pitwall_ui_frontend.py). Exit code is the result.
 */

import { runPitwallTests } from './pitwall/view-model.test.js';

const report = runPitwallTests();
for (const result of report.results) {
  console.log(`${result.ok ? 'PASS' : 'FAIL'}  ${result.name}`);
  if (!result.ok) console.log(`      ${result.error}`);
}
console.log(`\n${report.passed}/${report.total} passed.`);
process.exit(report.ok ? 0 : 1);
