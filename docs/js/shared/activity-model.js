/**
 * The agent layer as data: activity records in, per-agent state and runs out.
 *
 * Pure on purpose. No DOM, no socket, no clock: the live view feeds it records as
 * /ws/pitwall pushes them, the post-race review feeds it a recorded
 * pitwall.jsonl up to the cursor, and both get the same answer because they run
 * the same code. Tested under node by shared.test.js.
 *
 * A record is the backend envelope from rtv.pitwall.activity:
 *
 *   { seq, wall_ms, session_time, tick, kind, agent, run_id, event_key, data }
 *
 * Kinds this module does not know (the recorder adds race_event, radio,
 * lifecycle, state) are ignored by `apply`, but still mapped to graph edges by
 * `edgesFor` where they have one.
 */

export const AGENTS = ['strategist', 'vehicle_engineer', 'spotter', 'coach'];

/** Graph node ids, in pipeline order. flow-graph.js lays these out. */
export const NODES = [
  'telemetry',
  'engine',
  'detectors',
  'bus',
  'orchestrator',
  ...AGENTS,
  'llm',
  'tools',
  'validator',
  'radio',
  'driver',
];

/** How many finished runs the model keeps; the review rebuilds from records. */
export const RUN_LIMIT = 200;

export function createModel() {
  const agents = {};
  for (const name of AGENTS) agents[name] = blankAgent(name);
  return {
    lastSeq: 0,
    agents,
    runs: new Map(), // run_id -> run, insertion ordered (oldest first)
    radio: { queued: 0, aired: 0, superseded: 0 },
    dispatch: { events: 0, routed: 0, blocked: 0, cooldown: 0, superseded: 0 },
    llm: { calls: 0, inflight: 0, errors: 0, latencyTotal: 0 },
    tools: { calls: 0, errors: 0, byName: {} },
    validator: { checks: 0, failed: 0, repaired: 0 },
    lastBlocked: null,
  };
}

function blankAgent(name) {
  return {
    name,
    status: 'idle', // idle | queued | thinking | tool | validating
    runId: null,
    since: null, // session_time the current status began
    detail: '',
    runs: 0,
    calls: 0,
    tools: 0,
    refused: 0,
    errors: 0,
    lastOutcome: null,
    lastLatencyMs: null,
    skipped: null, // last skip reason from dispatch (cooldown, disabled)
  };
}

function agentOf(model, name) {
  if (!name) return null;
  if (!model.agents[name]) model.agents[name] = blankAgent(name);
  return model.agents[name];
}

function runOf(model, record) {
  const id = record.run_id;
  if (!id) return null;
  let run = model.runs.get(id);
  if (!run) {
    run = {
      runId: id,
      agent: record.agent,
      eventKey: record.event_key,
      sessionTime: record.session_time,
      tick: record.tick,
      startWall: record.wall_ms,
      endWall: null,
      trigger: null,
      model: null,
      contextRadio: [],
      spans: [],
      validations: [],
      outcome: null,
      messageId: null,
      latencyMs: null,
      turns: 0,
      seqs: [],
    };
    model.runs.set(id, run);
    while (model.runs.size > RUN_LIMIT) {
      model.runs.delete(model.runs.keys().next().value);
    }
  }
  return run;
}

/** Fold one record into the model. Returns the model (mutated in place). */
export function apply(model, record) {
  if (!record || typeof record !== 'object') return model;
  if (typeof record.seq === 'number' && record.seq <= model.lastSeq) return model; // replayed
  if (typeof record.seq === 'number') model.lastSeq = record.seq;
  const data = record.data || {};
  const agent = agentOf(model, record.agent);
  const t = record.session_time;

  switch (record.kind) {
    case 'dispatch': {
      model.dispatch.events += 1;
      if (data.blocked) {
        model.dispatch.blocked += 1;
        model.lastBlocked = data.blocked;
        break;
      }
      for (const name of data.accepted || []) {
        model.dispatch.routed += 1;
        const a = agentOf(model, name);
        a.skipped = null;
        if (a.status === 'idle') setStatus(a, 'queued', t, record.event_key);
      }
      for (const [name, reason] of Object.entries(data.skipped || {})) {
        if (reason === 'cooldown') model.dispatch.cooldown += 1;
        agentOf(model, name).skipped = reason;
      }
      break;
    }
    case 'inbox_superseded':
      model.dispatch.superseded += 1;
      break;
    case 'run_start': {
      const run = runOf(model, record);
      run.trigger = data.trigger || null;
      run.model = data.model || null;
      run.contextRadio = data.context_radio || [];
      run.seqs.push(record.seq);
      if (agent) {
        agent.runs += 1;
        agent.runId = record.run_id;
        setStatus(agent, 'thinking', t, record.event_key);
      }
      break;
    }
    case 'llm_call_start': {
      const run = runOf(model, record);
      model.llm.inflight += 1;
      if (run) {
        run.spans.push({
          kind: 'llm',
          call: data.call,
          name: data.model || 'model',
          startWall: record.wall_ms,
          endWall: null,
          ok: null,
        });
        run.seqs.push(record.seq);
      }
      if (agent) setStatus(agent, 'thinking', t, data.model || '');
      break;
    }
    case 'llm_call_end': {
      const run = runOf(model, record);
      model.llm.inflight = Math.max(0, model.llm.inflight - 1);
      model.llm.calls += 1;
      model.llm.latencyTotal += Number(data.latency_ms) || 0;
      if (!data.ok) model.llm.errors += 1;
      if (run) {
        const span = [...run.spans].reverse().find((s) => s.kind === 'llm' && s.call === data.call);
        if (span) {
          span.endWall = record.wall_ms;
          span.latencyMs = data.latency_ms;
          span.ok = !!data.ok;
          span.error = data.error || null;
          span.toolCalls = data.tool_calls || [];
        }
        run.seqs.push(record.seq);
      }
      if (agent) {
        agent.calls += 1;
        agent.lastLatencyMs = data.latency_ms;
      }
      break;
    }
    case 'tool_call': {
      const run = runOf(model, record);
      model.tools.calls += 1;
      if (!data.ok) model.tools.errors += 1;
      if (data.tool) model.tools.byName[data.tool] = (model.tools.byName[data.tool] || 0) + 1;
      if (run) {
        const latency = Number(data.latency_ms) || 0;
        run.spans.push({
          kind: 'tool',
          name: data.tool,
          startWall: record.wall_ms == null ? null : record.wall_ms - latency,
          endWall: record.wall_ms,
          latencyMs: data.latency_ms,
          ok: !!data.ok,
          args: data.args || {},
          result: data.result || null,
        });
        run.seqs.push(record.seq);
      }
      if (agent) {
        agent.tools += 1;
        setStatus(agent, 'tool', t, data.tool || '');
      }
      break;
    }
    case 'validate': {
      const run = runOf(model, record);
      model.validator.checks += 1;
      if (!data.ok) model.validator.failed += 1;
      if (data.repaired) model.validator.repaired += 1;
      if (run) {
        run.validations.push({
          ok: !!data.ok,
          unsupported: data.unsupported || [],
          repaired: !!data.repaired,
          wordCount: data.word_count,
        });
        run.seqs.push(record.seq);
      }
      if (agent) setStatus(agent, 'validating', t, data.ok ? 'grounded' : 'unsupported figures');
      break;
    }
    case 'run_end': {
      const run = runOf(model, record);
      if (run) {
        run.endWall = record.wall_ms;
        run.outcome = data.outcome || null;
        run.messageId = data.message_id || null;
        run.latencyMs = data.latency_ms;
        run.turns = data.turns || 0;
        run.seqs.push(record.seq);
      }
      if (agent) {
        agent.lastOutcome = data.outcome || null;
        if (data.outcome === 'refused') agent.refused += 1;
        if (data.outcome === 'error') agent.errors += 1;
        agent.runId = null;
        setStatus(agent, 'idle', t, '');
      }
      break;
    }
    case 'radio_queued':
      model.radio.queued += 1;
      break;
    case 'radio_superseded':
      model.radio.superseded += 1;
      break;
    case 'radio_aired':
      model.radio.aired += 1;
      break;
    default:
      break; // a kind this module does not know: ignore, by contract
  }
  return model;
}

function setStatus(agent, status, t, detail) {
  if (agent.status !== status) agent.since = t == null ? null : t;
  agent.status = status;
  agent.detail = detail || '';
}

/**
 * Rebuild the model from scratch from records, optionally only those at or
 * before session time `upTo`. This is what the review calls on every seek.
 */
export function reduce(records, upTo = null) {
  const model = createModel();
  const sorted = [...(records || [])].sort((a, b) => (a.seq || 0) - (b.seq || 0));
  for (const record of sorted) {
    if (upTo != null && record.session_time != null && record.session_time > upTo) continue;
    apply(model, record);
  }
  return model;
}

/** Runs newest first, for the waterfall. */
export function recentRuns(model, limit = 30) {
  return [...model.runs.values()].reverse().slice(0, limit);
}

/** Mean model-call latency, or null before the first call. */
export function meanLatency(model) {
  return model.llm.calls ? model.llm.latencyTotal / model.llm.calls : null;
}

/**
 * Which graph edges a record travelled, as [{from, to, kind, agent}].
 *
 * Every edge here is a code path in the backend: dispatch is the bus handing
 * an event to the orchestrator and the orchestrator to an agent's inbox; the
 * radio -> agent edges are the recent radio a run was shown as context (the
 * only way one agent ever hears another).
 */
export function edgesFor(record) {
  if (!record) return [];
  const data = record.data || {};
  const agent = record.agent;
  const e = (from, to, extra) => ({ from, to, kind: record.kind, agent, ...(extra || {}) });
  switch (record.kind) {
    case 'frames':
      return [e('telemetry', 'engine')];
    case 'race_event':
      return [e('detectors', 'bus')];
    case 'dispatch':
      if (data.blocked) return [e('bus', 'orchestrator', { blocked: true })];
      return [
        e('bus', 'orchestrator'),
        ...(data.accepted || []).map((name) => e('orchestrator', name, { agent: name })),
      ];
    case 'run_start':
      return (data.context_radio || []).map((m) =>
        e('radio', agent, { source: m.agent, context: true })
      );
    case 'llm_call_start':
      return [e(agent, 'llm')];
    case 'llm_call_end':
      return [e('llm', agent, { ok: !!data.ok })];
    case 'tool_call':
      return [e(agent, 'tools'), e('tools', agent, { ok: !!data.ok, delay: true })];
    case 'validate':
      return [e(agent, 'validator', { ok: !!data.ok })];
    case 'radio_queued':
      // Notices about the pitwall itself come from the orchestrator, not a run.
      return [e(AGENTS.includes(agent) ? 'validator' : 'orchestrator', 'radio')];
    case 'radio_aired':
      // The driver's own push-to-talk goes onto the channel the other way.
      return [agent === 'driver' ? e('driver', 'radio') : e('radio', 'driver')];
    default:
      return [];
  }
}
