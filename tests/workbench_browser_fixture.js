"use strict";

(function installWorkbenchBrowserFixture(global) {
  const nativeFetch = global.fetch.bind(global);
  const pending = new Map();
  const requests = [];
  let mode = "initial";
  let oldStatusCount = 0;
  let oldEventHistoryCount = 0;
  let acceptedCreates = 0;
  let lastCreatedRunId = null;

  const application = {
    id: "incident-response",
    title: "Incident response",
    description: "Deterministic browser fixture application",
    availability: { status: "ready", reason_code: null },
    worker: { id: "fixture-worker", title: "Fixture worker", model_slot: "deterministic" },
    skills: [{
      id: "com.sasori.memory/bounded-recall",
      version: "1",
      title: "Durable bounded recall",
      description: "Recall fixed-scope Memory through Harness-gated tools.",
      tool_names: ["search_memory", "remember_memory", "forget_memory"],
      content_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }],
    tools: [
      { name: "search_memory", description: "Search Memory", effect: "read_only", tool_revision: null, plugin_id: "com.sasori.memory", input_schema: {}, schema_sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" },
      { name: "remember_memory", description: "Remember Memory", effect: "idempotent", tool_revision: "memory-v1", plugin_id: "com.sasori.memory", input_schema: {}, schema_sha256: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" },
      { name: "forget_memory", description: "Forget Memory", effect: "idempotent", tool_revision: "memory-v1", plugin_id: "com.sasori.memory", input_schema: {}, schema_sha256: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd" },
    ],
    plugins: [{
      id: "com.sasori.memory",
      name: "Sasori Durable bounded Memory",
      version: "0.1.0.dev0",
      execution_mode: "trusted_process",
      requested_permissions: {
        filesystem_read: ["configured:memory.sqlite3"],
        filesystem_write: ["configured:memory.sqlite3"],
        network_egress: [],
        host_process: [],
        secrets: [],
      },
      effective_access: "FULL HOST PROCESS PRIVILEGES",
      enforced: false,
    }],
  };

  function projection(runId, input, state = "completed", overrides = {}) {
    return {
      run_id: runId,
      app_id: application.id,
      input,
      state,
      pause_reason: null,
      detail: null,
      step: 1,
      latest_seq: state === "completed" ? 1 : 0,
      revision: 1,
      final_message: state === "completed" ? { role: "assistant", content: `${input} final` } : null,
      pending: null,
      ...overrides,
    };
  }

  function projectedEvent(runId, type = "run.completed") {
    return {
      seq: 1,
      event: {
        type,
        run_id: runId,
        step: 1,
        version: 1,
        tool_name: null,
        call_id: null,
        data: {},
      },
    };
  }

  function json(payload, status = 200) {
    return new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }

  function eventBatch(runId, events = []) {
    return json({
      run_id: runId,
      after_seq: 0,
      latest_seq: events.length,
      events,
    });
  }

  function defer(name) {
    return new Promise((resolve) => {
      const queue = pending.get(name) || [];
      queue.push(resolve);
      pending.set(name, queue);
    });
  }

  function resolveNext(name, response) {
    const queue = pending.get(name) || [];
    if (!queue.length) throw new Error(`no pending response named ${name}`);
    const resolve = queue.shift();
    resolve(response);
  }

  function pendingCount(name) {
    return (pending.get(name) || []).length;
  }

  function gatedEventStream(name) {
    let delivered = false;
    return {
      ok: true,
      status: 200,
      headers: new Headers({ "Content-Type": "text/event-stream; charset=utf-8" }),
      body: {
        getReader() {
          return {
            async read() {
              if (delivered) return { value: undefined, done: true };
              const text = await defer(name);
              delivered = true;
              return { value: new TextEncoder().encode(text), done: false };
            },
          };
        },
      },
    };
  }

  function headersFor(input, options) {
    if (options && options.headers) return new Headers(options.headers);
    if (input instanceof Request) return new Headers(input.headers);
    return new Headers();
  }

  async function bodyFor(input, options) {
    if (options && options.body !== undefined) return String(options.body);
    if (input instanceof Request) return input.clone().text();
    return "";
  }

  global.fetch = async function fixtureFetch(input, options = {}) {
    const url = new URL(input instanceof Request ? input.url : String(input), global.location.href);
    if (url.origin !== global.location.origin) return nativeFetch(input, options);
    const method = (options.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const headers = headersFor(input, options);
    const accept = headers.get("Accept") || "";
    requests.push({ method, path: `${url.pathname}${url.search}`, accept, mode });

    if (method === "GET" && url.pathname === "/v1/apps") {
      return json({ schema_version: 1, apps: [application] });
    }
    if (method === "GET" && url.pathname === "/v1/runs") {
      return json({
        items: [
          { run_id: "run-old", state: "completed", input_preview: "old fixture run" },
          { run_id: "run-new", state: "completed", input_preview: "new fixture run" },
        ],
        next_before: null,
      });
    }
    if (method === "GET" && url.pathname === "/v1/runs/run-new") {
      return json(projection("run-new", "fresh selected run"));
    }
    if (method === "GET" && url.pathname === "/v1/runs/run-new/events") {
      return eventBatch("run-new", [projectedEvent("run-new")]);
    }
    if (method === "GET" && url.pathname === "/v1/runs/run-old") {
      oldStatusCount += 1;
      if (mode === "stale-status") {
        return defer("stale-status");
      }
      if (mode === "same-run-epoch" && oldStatusCount === 1) {
        return defer("same-run-epoch");
      }
      if (mode === "same-run-epoch") {
        return json(projection("run-old", "fresh same-run epoch"));
      }
      if (mode === "cold-events") {
        if (oldStatusCount === 1) {
          return json(projection("run-old", "cold-events source", "running", {
            latest_seq: 1,
            final_message: null,
          }));
        }
        return json(projection("run-old", "fresh cold-events epoch"));
      }
      if (mode === "late-sse") {
        if (oldStatusCount <= 2) {
          return json(projection("run-old", "late-sse source", "running", {
            latest_seq: 0,
            final_message: null,
          }));
        }
        return json(projection("run-old", "fresh late-sse epoch"));
      }
      if (mode === "approval") {
        return json(projection("run-old", "approval source", "paused", {
          pause_reason: "approval_required",
          latest_seq: 0,
          pending: {
            tool_name: "fixture.effect",
            effect: "write",
            tool_revision: "fixture-v1",
            fingerprint: "fixture-fingerprint",
            idempotency_key: "fixture-idempotency",
            arguments: { value: 1 },
          },
        }));
      }
      if (mode === "artifact-stale") {
        return json(projection("run-old", "artifact stale source"));
      }
      return json(projection("run-old", `${mode} source`, "running", {
        latest_seq: mode === "cold-events" ? 1 : 0,
        final_message: null,
      }));
    }
    if (method === "GET" && url.pathname === "/v1/runs/run-old/events") {
      if (accept.includes("text/event-stream") && mode === "late-sse") {
        return gatedEventStream("late-sse");
      }
      if (!accept.includes("text/event-stream") && mode === "cold-events") {
        oldEventHistoryCount += 1;
        if (oldEventHistoryCount === 1) return defer("cold-events");
      }
      return eventBatch("run-old");
    }
    const artifactList = url.pathname.match(/^\/v1\/runs\/(run-old|run-new)\/artifacts$/);
    if (method === "GET" && artifactList) {
      const runId = artifactList[1];
      if (mode === "artifact-stale" && runId === "run-old") {
        return defer("artifact-stale");
      }
      return json({ run_id: runId, artifacts: [] });
    }
    if (method === "POST" && url.pathname === "/v1/runs/run-old/approval") {
      return defer("approval");
    }
    if (method === "POST" && url.pathname === "/v1/runs") {
      const body = JSON.parse(await bodyFor(input, options));
      acceptedCreates += 1;
      lastCreatedRunId = body.run_id;
      return defer("create-run");
    }
    if (method === "GET" && /^\/v1\/runs\/run-[0-9a-f-]+$/i.test(url.pathname)) {
      return json({ error: { code: "run_not_found", message: "fixture run is pending" } }, 404);
    }
    if (method === "GET" && /^\/v1\/runs\/run-[0-9a-f-]+\/events$/i.test(url.pathname)) {
      const runId = url.pathname.split("/")[3];
      return eventBatch(runId);
    }
    return json({ error: { code: "fixture_route_missing", message: `${method} ${url.pathname}` } }, 404);
  };

  global.__sasoriFixture = {
    get acceptedCreates() { return acceptedCreates; },
    get lastCreatedRunId() { return lastCreatedRunId; },
    get requests() { return requests.slice(); },
    pendingCount,
    projection,
    projectedEvent,
    eventBatch,
    json,
    reset(nextMode) {
      mode = nextMode;
      oldStatusCount = 0;
      oldEventHistoryCount = 0;
    },
    resolveNext,
  };
})(window);

(function runWorkbenchBrowserAcceptance(global) {
  const cases = [];
  const result = document.createElement("pre");
  result.id = "sasori-browser-result";
  result.hidden = true;
  document.documentElement.append(result);

  function fail(message) {
    throw new Error(message);
  }

  function assert(condition, message) {
    if (!condition) fail(message);
  }

  function record(name) {
    cases.push(name);
    result.textContent = `RUNNING:${cases.join(",")}`;
  }

  function tick() {
    return new Promise((resolve) => setTimeout(resolve, 0));
  }

  async function waitFor(predicate, message, turns = 200) {
    for (let index = 0; index < turns; index += 1) {
      if (predicate()) return;
      await tick();
    }
    fail(`timed out: ${message}`);
  }

  function historyButton(runId) {
    return document.querySelector(`.history-card[data-run-id="${runId}"]`);
  }

  async function open(runId) {
    const button = historyButton(runId);
    assert(button, `history button is missing for ${runId}`);
    button.click();
  }

  async function waitForSelected(runId, input) {
    await waitFor(
      () => document.querySelector("#active-run-label")?.textContent === runId &&
        document.querySelector("#run-title")?.textContent === input,
      `${runId} did not become the active run`,
    );
  }

  function assertFreshNewView(caseName) {
    assert(document.querySelector("#active-run-label").textContent === "run-new", `${caseName}: stale run replaced the label`);
    assert(document.querySelector("#run-title").textContent === "fresh selected run", `${caseName}: stale run replaced the title`);
    assert(document.querySelector("#sequence-label").textContent === "1", `${caseName}: stale run replaced the cursor`);
    assert(document.querySelector("#event-count").textContent === "1", `${caseName}: stale run replaced the event count`);
    assert(!document.querySelector("#timeline-list").textContent.includes("fixture.stale"), `${caseName}: stale SSE event reached the timeline`);
  }

  function assertFreshSameRunView(caseName, input) {
    assert(document.querySelector("#active-run-label").textContent === "run-old", `${caseName}: active run changed`);
    assert(document.querySelector("#run-title").textContent === input, `${caseName}: stale epoch replaced the title`);
    assert(document.querySelector("#sequence-label").textContent === "0", `${caseName}: stale epoch replaced the cursor`);
    assert(document.querySelector("#event-count").textContent === "0", `${caseName}: stale epoch replaced the event count`);
    assert(!document.querySelector("#timeline-list").textContent.includes("fixture.stale"), `${caseName}: stale epoch reached the timeline`);
  }

  async function staleStatusCase(fixture) {
    fixture.reset("stale-status");
    await open("run-old");
    await waitFor(() => fixture.pendingCount("stale-status") === 1, "old status request was not delayed");
    await open("run-new");
    await waitForSelected("run-new", "fresh selected run");
    fixture.resolveNext("stale-status", fixture.json(fixture.projection("run-old", "stale status epoch")));
    await tick();
    await tick();
    assertFreshNewView("stale-status");
    record("stale-status");
  }

  async function sameRunEpochCase(fixture) {
    fixture.reset("same-run-epoch");
    await open("run-old");
    await waitFor(() => fixture.pendingCount("same-run-epoch") === 1, "first same-run status was not delayed");
    await open("run-old");
    await waitForSelected("run-old", "fresh same-run epoch");
    fixture.resolveNext("same-run-epoch", fixture.json(fixture.projection("run-old", "stale same-run epoch")));
    await tick();
    await tick();
    assert(document.querySelector("#run-title").textContent === "fresh same-run epoch", "same-run stale epoch replaced the newer epoch");
    record("same-run-epoch");
  }

  async function coldEventsCase(fixture) {
    fixture.reset("cold-events");
    await open("run-old");
    await waitFor(() => fixture.pendingCount("cold-events") === 1, "old cold event history was not delayed");
    await open("run-old");
    await waitForSelected("run-old", "fresh cold-events epoch");
    fixture.resolveNext("cold-events", fixture.eventBatch("run-old", [fixture.projectedEvent("run-old", "fixture.stale")]));
    await tick();
    await tick();
    assertFreshSameRunView("cold-events", "fresh cold-events epoch");
    record("cold-events");
  }

  async function lateSseCase(fixture) {
    fixture.reset("late-sse");
    await open("run-old");
    await waitFor(() => fixture.pendingCount("late-sse") === 1, "old SSE reader was not waiting for a chunk");
    await open("run-old");
    await waitForSelected("run-old", "fresh late-sse epoch");
    const stale = fixture.projectedEvent("run-old", "fixture.stale");
    const block = `id: 1\nevent: fixture.stale\ndata: ${JSON.stringify(stale)}\n\n`;
    fixture.resolveNext("late-sse", block);
    await tick();
    await tick();
    assertFreshSameRunView("late-sse", "fresh late-sse epoch");
    record("late-sse");
  }

  async function staleArtifactCase(fixture) {
    fixture.reset("artifact-stale");
    await open("run-old");
    await waitFor(() => fixture.pendingCount("artifact-stale") === 1, "old artifact list was not delayed");
    await open("run-new");
    await waitForSelected("run-new", "fresh selected run");
    fixture.resolveNext("artifact-stale", fixture.json({
      run_id: "run-old",
      artifacts: [{
        version: 1,
        artifact_id: "artifact-stale",
        run_id: "run-old",
        content_sha256: "a".repeat(64),
        size_bytes: 5,
        filename: "stale.txt",
        media_type: "text/plain; charset=utf-8",
        created_seq: 2,
      }],
    }));
    await tick();
    await tick();
    assertFreshNewView("artifact-stale");
    assert(!document.querySelector("#artifact-list").textContent.includes("stale.txt"), "stale artifact response reached the active view");
    record("artifact-stale");
  }

  async function acceptedCreateCase(fixture) {
    fixture.reset("create-run");
    const input = document.querySelector("#task-input");
    input.value = "accepted stale create";
    document.querySelector("#task-form").requestSubmit();
    await waitFor(() => fixture.pendingCount("create-run") === 1, "create mutation was not delayed");
    await open("run-new");
    await waitForSelected("run-new", "fresh selected run");
    const createRequest = fixture.requests.some((request) => request.method === "POST" && request.path === "/v1/runs");
    assert(createRequest, "create request was not recorded");
    const generated = fixture.lastCreatedRunId;
    assert(generated, "generated run watcher was not observed");
    fixture.resolveNext("create-run", fixture.json(fixture.projection(generated, "accepted stale create")));
    await tick();
    await tick();
    assert(fixture.acceptedCreates === 1, "accepted create was not retained by the fixture ledger");
    assertFreshNewView("create-run");
    record("create-run");
  }

  async function approvalCase(fixture) {
    fixture.reset("approval");
    await open("run-old");
    await waitFor(() => document.querySelector(".approve-action"), "approval action was not rendered");
    document.querySelector(".approve-action").click();
    await waitFor(() => fixture.pendingCount("approval") === 1, "approval mutation was not delayed");
    await open("run-new");
    await waitForSelected("run-new", "fresh selected run");
    fixture.resolveNext("approval", fixture.json(fixture.projection("run-old", "stale approval response", "paused", {
      pause_reason: "resume_required",
      final_message: null,
    })));
    await tick();
    await tick();
    assertFreshNewView("approval");
    assert(!document.querySelector("#operator-action").textContent.includes("显式继续运行"), "stale approval action reached the active view");
    record("approval");
  }

  function memorySkillSurfaceCase() {
    document.querySelector("#surface-tab").click();
    const text = document.querySelector("#surface-content").textContent;
    assert(text.includes("Durable bounded recall"), "Memory skill is not visible");
    assert(text.includes("search_memory"), "Memory search tool is not visible");
    assert(text.includes("remember_memory"), "Memory remember tool is not visible");
    assert(text.includes("forget_memory"), "Memory forget tool is not visible");
    assert(text.includes("com.sasori.memory"), "Memory plugin identity is not visible");
    assert(text.includes("configured:memory.sqlite3"), "Memory permission disclosure is not visible");
    document.querySelector("#timeline-tab").click();
    record("memory-skill-surface");
  }

  async function run() {
    const fixture = global.__sasoriFixture;
    await waitFor(
      () => global.SasoriEventReducer && historyButton("run-old") && historyButton("run-new") &&
        !document.querySelector("#run-button").disabled,
      "production Workbench did not finish initial loading",
    );
    memorySkillSurfaceCase();
    await staleStatusCase(fixture);
    await sameRunEpochCase(fixture);
    await coldEventsCase(fixture);
    await lateSseCase(fixture);
    await staleArtifactCase(fixture);
    await acceptedCreateCase(fixture);
    await approvalCase(fixture);
    result.dataset.result = "passed";
    result.textContent = `PASS:${cases.join(",")}`;
    document.title = "Sasori browser acceptance passed";
  }

  function reportFailure(error) {
    result.dataset.result = "failed";
    result.textContent = `FAIL:${error && error.stack ? error.stack : String(error)}`;
    document.title = "Sasori browser acceptance failed";
  }

  global.addEventListener("error", (event) => reportFailure(event.error || event.message));
  global.addEventListener("unhandledrejection", (event) => reportFailure(event.reason));
  global.addEventListener("load", () => run().catch(reportFailure), { once: true });
})(window);
