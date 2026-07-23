/**
 * Formatting for a pit stand: pure, total, and honest about missing data.
 *
 * Every function here takes something that may legitimately be `null` -- because
 * `RaceState` says `null` whenever the session's catalog could not back a number
 * -- and returns `n/a` rather than a zero. That is the whole reason this module
 * exists as its own file: a `0.0` where the sim never told us anything is the
 * same class of lie as an agent quoting a gap it was never shown.
 *
 * No DOM, no timers, no browser. Tested by js/pitwall/view-model.test.js.
 */

/** What the UI prints when a channel did not exist. Never "0", never "--". */
export const NA = 'n/a';

export function isNum(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

/** A fixed-point number, or `n/a`. */
export function num(value, digits = 1, suffix = '') {
  if (!isNum(value)) return NA;
  return `${value.toFixed(digits)}${suffix}`;
}

/** A whole number, or `n/a`. */
export function integer(value, suffix = '') {
  if (!isNum(value)) return NA;
  return `${Math.round(value)}${suffix}`;
}

/** Lap time as a driver reads it: `1:32.418`, or `52.104` under a minute. */
export function lapTime(seconds) {
  if (!isNum(seconds) || seconds <= 0) return NA;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds - minutes * 60;
  if (minutes <= 0) return rest.toFixed(3);
  return `${minutes}:${rest.toFixed(3).padStart(6, '0')}`;
}

/** A countdown: `1:02:33` with hours, `12:33` without. */
export function clock(seconds) {
  if (!isNum(seconds) || seconds < 0) return NA;
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}:${pad2(minutes)}:${pad2(secs)}`;
  return `${minutes}:${pad2(secs)}`;
}

/** A signed gap in seconds: `+1.23`, `-0.40`, `n/a`. */
export function gap(seconds, digits = 2) {
  if (!isNum(seconds)) return NA;
  const sign = seconds > 0 ? '+' : seconds < 0 ? '−' : '';
  return `${sign}${Math.abs(seconds).toFixed(digits)}`;
}

/** A signed quantity that is not a gap (a trend, a margin). */
export function signed(value, digits = 1, suffix = '') {
  if (!isNum(value)) return NA;
  const sign = value > 0 ? '+' : value < 0 ? '−' : '';
  return `${sign}${Math.abs(value).toFixed(digits)}${suffix}`;
}

/**
 * An arrow for a rate of change. `deadband` is what counts as "not moving" --
 * without one, floating-point noise makes every tyre trend flicker.
 * Returns `''` for an unknown value, so the caller renders nothing rather than
 * a confident horizontal bar.
 */
export function trendArrow(value, deadband = 0) {
  if (!isNum(value)) return '';
  if (value > deadband) return '▲';
  if (value < -deadband) return '▼';
  return '–';
}

/** `pit_window_open` -> `pit window open`. Used by the ticker and the badges. */
export function humanise(key) {
  return String(key || '').replace(/[_:]/g, ' ').trim();
}

/** `vehicle_engineer` -> `VEHICLE ENGINEER`, for a radio badge. */
export function agentBadge(agent) {
  return humanise(agent).toUpperCase() || '?';
}

/** Wall-clock `HH:MM:SS` from an epoch-seconds timestamp, or session time. */
export function stamp(timestamp, sessionTime) {
  if (isNum(timestamp)) {
    const date = new Date(timestamp * 1000);
    return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
  }
  // Replay carries no wall clock on purpose (the event log must stay
  // reproducible), so fall back to the session clock rather than to "now".
  if (isNum(sessionTime)) return clock(sessionTime);
  return NA;
}

export default { NA, isNum, num, integer, lapTime, clock, gap, signed, trendArrow };
