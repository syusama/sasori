"use strict";

(function installEventReducer(global) {
  const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
  const EVENT_TYPE = /^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$/;
  const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

  class EventProjectionError extends Error {
    constructor(code, message) {
      super(message);
      this.name = "EventProjectionError";
      this.code = code;
    }
  }

  function fail(code, message) {
    throw new EventProjectionError(code, message);
  }

  function isRecord(value) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function copyJson(value, ancestors = new Set()) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") {
      if (!Number.isFinite(value)) fail("invalid_event_data", "event data numbers must be finite");
      return value;
    }
    if (typeof value !== "object") {
      fail("invalid_event_data", "event data must contain only JSON values");
    }
    if (ancestors.has(value)) fail("invalid_event_data", "event data must not be cyclic");
    ancestors.add(value);
    let copy;
    if (Array.isArray(value)) {
      const keys = Object.keys(value);
      if (keys.length !== value.length || keys.some((key, index) => key !== String(index))) {
        fail("invalid_event_data", "event data arrays must be dense JSON arrays");
      }
      copy = value.map((item) => copyJson(item, ancestors));
    } else if (isRecord(value)) {
      copy = Object.create(null);
      for (const key of Object.keys(value)) copy[key] = copyJson(value[key], ancestors);
    } else {
      fail("invalid_event_data", "event data objects must be plain records");
    }
    ancestors.delete(value);
    return Object.freeze(copy);
  }

  function canonicalJson(value) {
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
    if (value !== null && typeof value === "object") {
      return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
    }
    return JSON.stringify(value);
  }

  function normalizeProjection(projected) {
    if (!isRecord(projected) || !hasOwn(projected, "seq") || !hasOwn(projected, "event")) {
      fail("invalid_projection", "event projection requires seq and event fields");
    }
    if (!Number.isSafeInteger(projected.seq) || projected.seq < 1) {
      fail("invalid_sequence", "event sequence must be a positive integer");
    }
    const event = projected.event;
    if (!isRecord(event)) fail("invalid_projection", "projected event must be a record");
    for (const key of ["type", "run_id", "step", "version", "tool_name", "call_id", "data"]) {
      if (!hasOwn(event, key)) fail("invalid_projection", `projected event is missing ${key}`);
    }
    if (typeof event.type !== "string" || !EVENT_TYPE.test(event.type)) {
      fail("invalid_event_type", "event type is invalid");
    }
    if (typeof event.run_id !== "string" || !RUN_ID.test(event.run_id)) {
      fail("invalid_run_id", "event run_id is invalid");
    }
    if (!Number.isSafeInteger(event.step) || event.step < 0) {
      fail("invalid_step", "event step must be a non-negative integer");
    }
    if (!Number.isSafeInteger(event.version) || event.version !== 1) {
      fail("unsupported_event_version", "event version is unsupported");
    }
    if (event.tool_name !== null && typeof event.tool_name !== "string") {
      fail("invalid_tool_name", "event tool_name must be a string or null");
    }
    if (event.call_id !== null && typeof event.call_id !== "string") {
      fail("invalid_call_id", "event call_id must be a string or null");
    }
    if (!isRecord(event.data)) fail("invalid_event_data", "event data must be a record");

    return Object.freeze({
      seq: projected.seq,
      event: Object.freeze({
        type: event.type,
        run_id: event.run_id,
        step: event.step,
        version: event.version,
        tool_name: event.tool_name,
        call_id: event.call_id,
        data: copyJson(event.data),
      }),
    });
  }

  function createState(runId) {
    if (typeof runId !== "string" || !RUN_ID.test(runId)) {
      fail("invalid_run_id", "reducer run_id is invalid");
    }
    return Object.freeze({ runId, cursor: 0, events: Object.freeze([]) });
  }

  function reduceEvent(state, projected) {
    if (!state || !RUN_ID.test(state.runId) || !Number.isSafeInteger(state.cursor) ||
        !Array.isArray(state.events) || state.events.length !== state.cursor) {
      fail("invalid_reducer_state", "event reducer state is invalid");
    }
    const normalized = normalizeProjection(projected);
    if (normalized.event.run_id !== state.runId) return state;

    if (normalized.seq <= state.cursor) {
      const existing = state.events[normalized.seq - 1];
      if (existing && canonicalJson(existing) === canonicalJson(normalized)) return state;
      fail("conflicting_duplicate", `event sequence ${normalized.seq} conflicts with durable history`);
    }
    const expected = state.cursor + 1;
    if (normalized.seq !== expected) {
      fail("sequence_gap", `expected event sequence ${expected}, received ${normalized.seq}`);
    }
    return Object.freeze({
      runId: state.runId,
      cursor: normalized.seq,
      events: Object.freeze(state.events.concat(normalized)),
    });
  }

  function createRunGate() {
    let epoch = 0;
    let watcherEpoch = 0;
    let active = null;
    let watcher = null;
    const activeContext = (context) => Boolean(
      context && context === active && !context.controller.signal.aborted
    );
    return Object.freeze({
      activate(runId) {
        if (typeof runId !== "string" || !RUN_ID.test(runId)) {
          fail("invalid_run_id", "selected run_id is invalid");
        }
        if (watcher) watcher.controller.abort();
        watcher = null;
        if (active) active.controller.abort();
        active = Object.freeze({
          runId,
          epoch: ++epoch,
          controller: new AbortController(),
        });
        return active;
      },
      current() {
        return active;
      },
      isActive(context) {
        return activeContext(context);
      },
      startWatcher(context) {
        if (!activeContext(context)) fail("inactive_run_context", "cannot watch an inactive run view");
        if (watcher) watcher.controller.abort();
        watcher = Object.freeze({
          context,
          epoch: ++watcherEpoch,
          controller: new AbortController(),
        });
        return watcher;
      },
      isWatcherActive(candidate) {
        return Boolean(
          candidate && candidate === watcher && activeContext(candidate.context) &&
          !candidate.controller.signal.aborted
        );
      },
      stopWatcher(candidate = null) {
        if (candidate && candidate !== watcher) return false;
        if (watcher) watcher.controller.abort();
        watcher = null;
        return true;
      },
      clear() {
        if (watcher) watcher.controller.abort();
        watcher = null;
        if (active) active.controller.abort();
        active = null;
      },
    });
  }

  function parseSseBlock(block) {
    if (typeof block !== "string") fail("invalid_sse_frame", "SSE frame must be text");
    let id = null;
    let eventName = null;
    let retry = null;
    const data = [];
    for (const line of block.replace(/\r\n?/g, "\n").split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      let value = colon < 0 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") {
        data.push(value);
      } else if (field === "id") {
        if (id !== null || !/^[0-9]+$/.test(value)) {
          fail("invalid_sse_id", "SSE event id is invalid or repeated");
        }
        id = Number(value);
        if (!Number.isSafeInteger(id) || id < 1) fail("invalid_sse_id", "SSE event id is out of range");
      } else if (field === "event") {
        if (eventName !== null || !EVENT_TYPE.test(value)) {
          fail("invalid_sse_event", "SSE event name is invalid or repeated");
        }
        eventName = value;
      } else if (field === "retry") {
        if (retry !== null || !/^[0-9]+$/.test(value)) {
          fail("invalid_sse_retry", "SSE retry field is invalid or repeated");
        }
        retry = Number(value);
        if (!Number.isSafeInteger(retry)) fail("invalid_sse_retry", "SSE retry field is out of range");
      } else {
        fail("invalid_sse_field", `SSE field is unsupported: ${field}`);
      }
    }
    if (!data.length) {
      if (id !== null || eventName !== null) fail("invalid_sse_frame", "SSE event metadata has no data");
      return null;
    }
    if (retry !== null || id === null || eventName === null) {
      fail("invalid_sse_frame", "SSE data frame requires exactly one id and event");
    }
    let projected;
    try {
      projected = JSON.parse(data.join("\n"));
    } catch {
      fail("invalid_sse_json", "SSE data is not valid JSON");
    }
    if (!isRecord(projected) || projected.seq !== id) {
      fail("sse_id_mismatch", "SSE id does not match projected sequence");
    }
    if (!isRecord(projected.event) || projected.event.type !== eventName) {
      fail("sse_event_mismatch", "SSE event name does not match projected type");
    }
    return projected;
  }

  function createSseFrameState() {
    return Object.freeze({ buffer: "", pendingCR: false });
  }

  function frameSseText(state, text, done = false) {
    if (!state || typeof state.buffer !== "string" || typeof state.pendingCR !== "boolean") {
      fail("invalid_sse_framer_state", "SSE framer state is invalid");
    }
    if (typeof text !== "string" || typeof done !== "boolean") {
      fail("invalid_sse_chunk", "SSE chunk and completion flag are invalid");
    }
    let incoming = `${state.pendingCR ? "\r" : ""}${text}`;
    const pendingCR = !done && incoming.endsWith("\r");
    if (pendingCR) incoming = incoming.slice(0, -1);
    let buffer = state.buffer + incoming.replace(/\r\n|\r/g, "\n");
    const blocks = [];
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      blocks.push(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
    if (done && buffer.trim()) {
      fail("incomplete_sse_frame", "SSE stream ended mid-frame");
    }
    return Object.freeze({
      state: Object.freeze({ buffer: done ? "" : buffer, pendingCR }),
      blocks: Object.freeze(blocks),
    });
  }

  const api = Object.freeze({
    EventProjectionError,
    createRunGate,
    createSseFrameState,
    createState,
    frameSseText,
    parseSseBlock,
    reduceEvent,
  });
  if (typeof module === "object" && module && module.exports) module.exports = api;
  Object.defineProperty(global, "SasoriEventReducer", {
    value: api,
    configurable: false,
    enumerable: false,
    writable: false,
  });
}(globalThis));
