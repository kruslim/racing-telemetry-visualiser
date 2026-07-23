/**
 * The smallest DOM helper that keeps the panels readable.
 *
 * Everything user- or agent-supplied goes in through `textContent`, never
 * `innerHTML`: an agent's `detail_text` and a track name from session-info YAML
 * are both strings this app did not write, and a pit stand is not the place to
 * find out that one of them contained a tag.
 */

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** A label/value pair, the unit this UI repeats most. */
export function stat(label, value, className) {
  const wrap = el('div', `stat ${className || ''}`.trim());
  wrap.append(el('span', 'stat-label', label), el('span', 'stat-value', value));
  return wrap;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function setText(node, text) {
  const next = text === null || text === undefined ? '' : String(text);
  if (node.textContent !== next) node.textContent = next;
  return node;
}

export function setClass(node, className) {
  if (node.className !== className) node.className = className;
  return node;
}

/**
 * Size a canvas to its box in device pixels and hand back a scaled context.
 * Without this every line on a HiDPI second monitor is a blurred two pixels.
 */
export function canvas2d(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth));
  const height = Math.max(1, Math.round(canvas.clientHeight));
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
    canvas.width = width * ratio;
    canvas.height = height * ratio;
  }
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

/** The palette, read from the stylesheet so the CSS stays the one source. */
export function cssVar(name, fallback) {
  try {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return value.trim() || fallback;
  } catch (_) {
    return fallback;
  }
}
