"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  EventProjectionError,
  createRunGate,
  createSseFrameState,
  createState,
  frameSseText,
  parseSseBlock,
  reduceEvent,
} = require("../src/sasori_web/event-reducer.0.1.0.js");

function projected(seq, overrides = {}) {
  return {
    seq,
    event: {
      type: "model.completed",
      run_id: "run-1",
      step: 1,
      version: 1,
      tool_name: null,
      call_id: null,
      data: { usage: { output: 3 }, labels: ["stable"] },
      ...overrides,
    },
  };
}

function rejects(code, action) {
  assert.throws(action, (error) => {
    assert.ok(error instanceof EventProjectionError);
    assert.equal(error.code, code);
    return true;
  });
}

test("live, cold, and reconnect deliveries share one immutable reduction", () => {
  const initial = createState("run-1");
  const first = reduceEvent(initial, projected(1, { type: "run.started", step: 0 }));
  const second = reduceEvent(first, projected(2));
  const reorderedKeys = {
    event: {
      data: { labels: ["stable"], usage: { output: 3 } },
      call_id: null,
      tool_name: null,
      version: 1,
      step: 1,
      run_id: "run-1",
      type: "model.completed",
    },
    seq: 2,
  };
  const duplicate = reduceEvent(second, reorderedKeys);

  assert.equal(initial.cursor, 0);
  assert.deepEqual(initial.events, []);
  assert.equal(second.cursor, 2);
  assert.deepEqual(second.events.map((item) => item.seq), [1, 2]);
  assert.strictEqual(duplicate, second);
  assert.ok(Object.isFrozen(second));
  assert.ok(Object.isFrozen(second.events));
  assert.ok(Object.isFrozen(second.events[1].event.data));
});

test("a stale run is ignored without moving the selected run cursor", () => {
  const state = createState("run-2");
  const stale = reduceEvent(state, projected(1));
  assert.strictEqual(stale, state);
  assert.equal(stale.cursor, 0);
});

test("a gap never advances the durable reconnect cursor", () => {
  const state = reduceEvent(
    createState("run-1"),
    projected(1, { type: "run.started", step: 0 }),
  );
  rejects("sequence_gap", () => reduceEvent(state, projected(3)));
  assert.equal(state.cursor, 1);
  assert.deepEqual(state.events.map((item) => item.seq), [1]);
});

test("a conflicting duplicate fails closed instead of overwriting history", () => {
  const state = reduceEvent(createState("run-1"), projected(1));
  rejects("conflicting_duplicate", () => reduceEvent(
    state,
    projected(1, { data: { usage: { output: 99 } } }),
  ));
  assert.equal(state.events[0].event.data.usage.output, 3);
});

test("invalid envelopes, sequences, versions, and JSON data fail closed", () => {
  const state = createState("run-1");
  for (const sequence of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, "1"]) {
    rejects("invalid_sequence", () => reduceEvent(state, projected(sequence)));
  }
  rejects("unsupported_event_version", () => reduceEvent(state, projected(1, { version: 2 })));
  rejects("invalid_event_data", () => reduceEvent(state, projected(1, { data: [] })));
  rejects("invalid_event_data", () => reduceEvent(state, projected(1, { data: { value: NaN } })));
  rejects("invalid_event_data", () => reduceEvent(state, projected(1, { data: { value: new Array(1) } })));
  rejects("invalid_step", () => reduceEvent(state, projected(1, { step: -1 })));
  rejects("invalid_tool_name", () => reduceEvent(state, projected(1, { tool_name: 3 })));
  rejects("invalid_projection", () => reduceEvent(state, { seq: 1, event: {} }));
});

test("unknown version-one event families remain forward compatible", () => {
  const state = reduceEvent(
    createState("run-1"),
    projected(1, { type: "artifact.available", step: 0 }),
  );
  assert.equal(state.events[0].event.type, "artifact.available");
});

test("input mutation cannot rewrite an accepted event", () => {
  const input = projected(1);
  const state = reduceEvent(createState("run-1"), input);
  input.event.data.usage.output = 1000;
  input.event.data.labels.push("mutated");
  assert.equal(state.events[0].event.data.usage.output, 3);
  assert.deepEqual(state.events[0].event.data.labels, ["stable"]);
});

test("a newer run selection owns the view even when an older response arrives", async () => {
  const gate = createRunGate();
  const accepted = [];
  let releaseOld;
  const oldResponse = new Promise((resolve) => { releaseOld = resolve; });
  const oldContext = gate.activate("run-1");
  const oldUpdate = oldResponse.then((value) => {
    if (gate.isActive(oldContext)) accepted.push(value);
  });

  const newContext = gate.activate("run-2");
  assert.equal(oldContext.controller.signal.aborted, true);
  assert.equal(gate.isActive(oldContext), false);
  assert.equal(gate.isActive(newContext), true);
  accepted.push("run-2");
  releaseOld("run-1");
  await oldUpdate;
  assert.deepEqual(accepted, ["run-2"]);
});

test("reopening the same run invalidates the older epoch", () => {
  const gate = createRunGate();
  const first = gate.activate("run-1");
  const firstWatcher = gate.startWatcher(first);
  const second = gate.activate("run-1");
  assert.equal(first.runId, second.runId);
  assert.equal(second.epoch, first.epoch + 1);
  assert.equal(first.controller.signal.aborted, true);
  assert.equal(firstWatcher.controller.signal.aborted, true);
  assert.equal(gate.isActive(first), false);
  assert.equal(gate.isActive(second), true);
  gate.clear();
  assert.equal(second.controller.signal.aborted, true);
  assert.equal(gate.current(), null);
});

test("one selected run owns at most one active watcher", () => {
  const gate = createRunGate();
  const context = gate.activate("run-1");
  const first = gate.startWatcher(context);
  const second = gate.startWatcher(context);
  assert.equal(first.controller.signal.aborted, true);
  assert.equal(gate.isWatcherActive(first), false);
  assert.equal(gate.isWatcherActive(second), true);
  assert.equal(gate.stopWatcher(first), false);
  assert.equal(second.controller.signal.aborted, false);
  gate.stopWatcher();
  assert.equal(second.controller.signal.aborted, true);
  assert.equal(gate.isWatcherActive(second), false);
});

test("SSE metadata must agree with the projected durable event", () => {
  const value = JSON.stringify(projected(1));
  assert.deepEqual(
    parseSseBlock(`id: 1\r\nevent: model.completed\r\ndata: ${value}`),
    projected(1),
  );
  assert.equal(parseSseBlock("retry: 1000"), null);
  assert.equal(parseSseBlock(": keepalive"), null);
  rejects("sse_id_mismatch", () => parseSseBlock(`id: 2\nevent: model.completed\ndata: ${value}`));
  rejects("sse_event_mismatch", () => parseSseBlock(`id: 1\nevent: tool.started\ndata: ${value}`));
  rejects("invalid_sse_frame", () => parseSseBlock(`event: model.completed\ndata: ${value}`));
  rejects("invalid_sse_field", () => parseSseBlock(`unknown: value\nid: 1\nevent: model.completed\ndata: ${value}`));
});

test("SSE CRLF split across arbitrary chunks never invents a frame boundary", () => {
  const value = JSON.stringify(projected(1));
  const chunks = [
    "retry: 1000\r",
    "\n\r",
    "\nid: 1\r",
    "\nevent: model.completed\r",
    `\ndata: ${value}\r`,
    "\n\r",
    "\n",
  ];
  let frameState = createSseFrameState();
  const blocks = [];
  chunks.forEach((chunk, index) => {
    const framed = frameSseText(frameState, chunk, index === chunks.length - 1);
    frameState = framed.state;
    blocks.push(...framed.blocks);
  });
  assert.equal(blocks.length, 2);
  assert.equal(parseSseBlock(blocks[0]), null);
  assert.deepEqual(parseSseBlock(blocks[1]), projected(1));
  assert.deepEqual(frameState, { buffer: "", pendingCR: false });
  rejects("incomplete_sse_frame", () => frameSseText(
    createSseFrameState(),
    `id: 1\nevent: model.completed\ndata: ${value}`,
    true,
  ));
});
