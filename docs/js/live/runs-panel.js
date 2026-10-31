/**
 * Agent runs as a waterfall: one row per wake-up, a span per model call and tool
 * call, laid out on the wall clock of that run, plus its validator verdict and
 * outcome. Click a row for what the run was shown and what it did.
 *
 * Reads the model from shared/activity-model.js; owns no state of its own except
 * which run is selected. Text only through textContent (dom.js).
 */

import { recentRuns } from '../shared/activity-model.js';
import { clear, el, setText } from '../pitwall/dom.js';

const MIN_SCALE_MS = 4000;

export function spanLayout(run, scaleMs) {
  const start = run.startWall;
  if (start == null) return [];
  return run.spans
    .filter((s) => s.startWall != null)
    .map((s) => {
      const end = s.endWall != null ? s.endWall : start + scaleMs;
      return {
        span: s,
        left: Math.max(0, ((s.startWall - start) / scaleMs) * 100),
        width: Math.max(0.8, ((end - s.startWall) / scaleMs) * 100),
      };
    });
}

export class RunsPanel {
  constructor(host, { labels } = {}) {
    this.host = host;
    this.labels = labels || ((a) => a);
    this.list = host.querySelector('[data-role="runs-list"]');
    this.detail = host.querySelector('[data-role="run-detail"]');
    this.summary = host.querySelector('[data-role="runs-summary"]');
    this.selected = null;
  }

  render(model) {
    const runs = recentRuns(model, 24);
    const longest = Math.max(
      MIN_SCALE_MS,
      ...runs.map((r) => (r.endWall != null && r.startWall != null ? r.endWall - r.startWall : 0))
    );
    clear(this.list);
    if (!runs.length) {
      this.list.append(
        el(
          'div',
          'muted',
          'No agent runs yet. A run starts when a race event matches an agent trigger and the agent layer is on.'
        )
      );
    }
    for (const run of runs) this.list.append(this._row(run, longest));
    const llm = model.llm;
    setText(
      this.summary,
      `${model.runs.size} runs · ${llm.calls} model calls · ${model.tools.calls} tool calls · ` +
        `${model.validator.failed} failed checks · ${model.radio.aired} aired`
    );
    if (this.selected) {
      const run = model.runs.get(this.selected);
      if (run) this._renderDetail(run);
    }
  }

  _row(run, scaleMs) {
    const row = el('button', `run-row agent-${run.agent}${run.runId === this.selected ? ' selected' : ''}`);
    row.type = 'button';
    row.onclick = () => {
      this.selected = run.runId;
      this._renderDetail(run);
      this.list.querySelectorAll('.run-row').forEach((n) => n.classList.remove('selected'));
      row.classList.add('selected');
    };
    row.append(el('span', `run-agent agent-${run.agent}`, this.labels(run.agent)));
    row.append(el('span', 'run-event', run.eventKey || run.trigger || '—'));
    const track = el('span', 'run-track');
    for (const { span, left, width } of spanLayout(run, scaleMs)) {
      const bar = el(
        'span',
        `run-span span-${span.kind}${span.ok === false ? ' bad' : ''}${span.endWall == null ? ' open' : ''}`
      );
      bar.style.left = `${left}%`;
      bar.style.width = `${Math.min(100 - left, width)}%`;
      bar.title = `${span.kind === 'llm' ? 'model call' : 'tool'} ${span.name}${
        span.latencyMs != null ? ` · ${Math.round(span.latencyMs)} ms` : ''
      }`;
      track.append(bar);
    }
    row.append(track);
    const outcome = run.outcome || 'running';
    row.append(el('span', `run-outcome outcome-${outcome}`, outcome));
    row.append(
      el('span', 'run-latency', run.latencyMs != null ? `${(run.latencyMs / 1000).toFixed(1)} s` : '…')
    );
    return row;
  }

  _renderDetail(run) {
    const d = this.detail;
    clear(d);
    d.hidden = false;
    d.append(el('h3', '', `${this.labels(run.agent)} · ${run.runId}`));
    d.append(
      el(
        'div',
        'meta',
        `woken by ${run.eventKey || '—'} at session ${run.sessionTime != null ? run.sessionTime.toFixed(1) : '—'} s · model ${run.model || '—'}`
      )
    );
    const heard = run.contextRadio.length
      ? run.contextRadio.map((m) => this.labels(m.agent)).join(', ')
      : 'nothing yet';
    d.append(el('div', 'meta', `radio in context (what it heard from the other agents): ${heard}`));
    const steps = el('ol', 'run-steps');
    for (const s of run.spans) {
      const line =
        s.kind === 'llm'
          ? `model call #${s.call} · ${s.latencyMs != null ? `${Math.round(s.latencyMs)} ms` : 'in flight'}` +
            `${s.ok === false ? ` · failed (${s.error || 'error'})` : ''}` +
            `${s.toolCalls && s.toolCalls.length ? ` · asked for ${s.toolCalls.join(', ')}` : ''}`
          : `tool ${s.name}(${formatArgs(s.args)}) · ${s.ok ? 'ok' : 'error'} · ${describeResult(s.result)}`;
      steps.append(el('li', `step-${s.kind}`, line));
    }
    for (const v of run.validations) {
      steps.append(
        el(
          'li',
          `step-validate${v.ok ? '' : ' bad'}`,
          v.ok
            ? `grounding check passed${v.repaired ? ' after repair' : ''}`
            : `grounding check failed: ${v.unsupported.join(', ') || 'word limit'}`
        )
      );
    }
    if (run.outcome) {
      steps.append(
        el('li', `step-outcome outcome-${run.outcome}`, `outcome: ${run.outcome}${run.turns ? ` · ${run.turns} model calls` : ''}`)
      );
    }
    d.append(steps);
  }
}

function formatArgs(args) {
  const entries = Object.entries(args || {});
  if (!entries.length) return '';
  return entries.map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(', ');
}

function describeResult(result) {
  if (!result) return 'no result';
  if (result.error) return `error: ${result.error}`;
  if (result.type === 'object') return `${result.size} fields (${(result.keys || []).slice(0, 5).join(', ')})`;
  if (result.type === 'array') return `${result.size} items`;
  return result.type;
}
