/**
 * Headless runner for the post-race review tests.
 *
 *     node frontend/js/run-review-tests.mjs
 *
 * Same deal as run-pitwall-tests.mjs: node plus ES modules, no dependencies.
 * pytest runs it when node is installed (tests/test_review_frontend.py).
 */

import { runReviewTests } from './review/review-model.test.js';

const report = runReviewTests();
for (const result of report.results) {
  console.log(`${result.ok ? 'PASS' : 'FAIL'}  ${result.name}`);
  if (!result.ok) console.log(`      ${result.error}`);
}
console.log(`\n${report.passed}/${report.total} passed.`);
process.exit(report.ok ? 0 : 1);
