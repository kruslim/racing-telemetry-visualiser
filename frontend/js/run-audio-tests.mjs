/**
 * Headless runner for the audio-discipline tests.
 *
 *     node frontend/js/run-audio-tests.mjs
 *
 * The repo has no JS toolchain and does not want one, so this is deliberately
 * just node plus ES modules -- no package.json, no dependencies, no config. The
 * same test module is rendered in-page by frontend/audio-test.html, and pytest
 * runs this file when node happens to be installed (see
 * tests/test_pitwall_audio_frontend.py). Exit code is the result.
 */

import { runAudioTests } from './audio-manager.test.js';

const report = runAudioTests();
for (const result of report.results) {
  console.log(`${result.ok ? 'PASS' : 'FAIL'}  ${result.name}`);
  if (!result.ok) console.log(`      ${result.error}`);
}
console.log(`\n${report.passed}/${report.total} passed.`);
process.exit(report.ok ? 0 : 1);
