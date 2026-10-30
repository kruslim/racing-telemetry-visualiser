/**
 * Headless runner for the shared live/review component tests.
 *
 *     node frontend/js/run-shared-tests.mjs
 *
 * Same deal as run-pitwall-tests.mjs: node plus ES modules, no dependencies.
 * pytest runs it when node is installed (tests/test_live_view_frontend.py).
 */

import { runSharedTests } from './shared/shared.test.js';

const report = runSharedTests();
for (const result of report.results) {
  console.log(`${result.ok ? 'PASS' : 'FAIL'}  ${result.name}`);
  if (!result.ok) console.log(`      ${result.error}`);
}
console.log(`\n${report.passed}/${report.total} passed.`);
process.exit(report.ok ? 0 : 1);
