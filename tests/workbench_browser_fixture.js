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
  let workflowStatusCount = 0;
  let workflowStatusInFlight = 0;
  let workflowStatusMaxInFlight = 0;
  let cancelledRecoveryCount = 0;
  let cancelledRecoveryBody = null;
  let cancelledRecoveryResolved = false;

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

  const workflowApplication = {
    id: "flow.fixture.aaaaaaaaaaaa",
    title: "Fixture mechanism",
    description: "Definition-bound serial Workflow fixture",
    availability: { status: "ready", reason_code: null },
    worker: {
      id: "fixture-workflow-v1",
      title: "Fixture Workflow",
      model_slot: "deterministic-workflow",
      tool_names: ["wf_inspect_fixture", "wf_record_fixture"],
      logical_tool_names: ["inspect_incident", "record_action"],
    },
    skills: [{
      id: "fixture-workflow",
      title: "Fixture typed workflow",
      description: "Inspect, then pause before one mutable action.",
      tool_names: ["wf_inspect_fixture", "wf_record_fixture"],
      logical_tool_names: ["inspect_incident", "record_action"],
    }],
    workflow: {
      schema_version: 1,
      workflow_id: "fixture-mechanism",
      version: "1",
      definition_sha256: "a".repeat(64),
      execution: "single-harness-ordered-tools-v1",
      step_count: 2,
      supports_parallel: false,
      supports_branches: false,
      supports_agent_nodes: false,
      steps: [
        {
          position: 1,
          step_id: "inspect",
          logical_tool_name: "inspect_incident",
          dispatch_tool_name: "wf_inspect_fixture",
          effect: "read_only",
          logical_tool_revision: null,
          dispatch_tool_revision: null,
          logical_schema_sha256: "b".repeat(64),
          dispatch_schema_sha256: "c".repeat(64),
          result_type: "string",
          max_result_bytes: 32768,
          is_output: false,
        },
        {
          position: 2,
          step_id: "record",
          logical_tool_name: "record_action",
          dispatch_tool_name: "wf_record_fixture",
          effect: "side_effecting",
          logical_tool_revision: "fixture-record-v1",
          dispatch_tool_revision: "fixture-wrapper-v1",
          logical_schema_sha256: "d".repeat(64),
          dispatch_schema_sha256: "e".repeat(64),
          result_type: "string",
          max_result_bytes: 32768,
          is_output: true,
        },
      ],
    },
    tools: [
      { name: "wf_inspect_fixture", description: "Inspect", effect: "read_only", tool_revision: null, plugin_id: "com.sasori.flow", input_schema: {}, schema_sha256: "c".repeat(64) },
      { name: "wf_record_fixture", description: "Record", effect: "side_effecting", tool_revision: "fixture-wrapper-v1", plugin_id: "com.sasori.flow", input_schema: {}, schema_sha256: "e".repeat(64) },
    ],
    plugins: [{
      id: "com.sasori.flow",
      name: "Sasori Typed Workflow",
      version: "0.1.0.dev0",
      execution_mode: "trusted_process",
      requested_permissions: {
        filesystem_read: [],
        filesystem_write: ["configured:incident-action-log"],
        network_egress: [],
        host_process: [],
        secrets: [],
      },
      effective_access: "FULL HOST PROCESS PRIVILEGES",
      enforced: false,
    }],
  };

  const unavailableWorkflowApplication = {
    ...workflowApplication,
    id: "flow.unavailable.ffffffffffff",
    title: "Unavailable mechanism",
    description: "Logical Workflow metadata without a loaded Harness binding",
    availability: { status: "unavailable", reason_code: "not_enabled" },
    worker: {
      ...workflowApplication.worker,
      id: "fixture-workflow-unavailable",
      title: "Unavailable Workflow",
      tool_names: ["inspect_incident", "record_action"],
    },
    workflow: {
      ...workflowApplication.workflow,
      workflow_id: "unavailable-mechanism",
      definition_sha256: "f".repeat(64),
      steps: workflowApplication.workflow.steps.map((step) => ({
        ...step,
        dispatch_tool_name: null,
        dispatch_tool_revision: null,
        dispatch_schema_sha256: null,
      })),
    },
    tools: [],
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

  function workflowProjection(runId) {
    const steps = workflowApplication.workflow.steps.map((step, index) => ({
      position: step.position,
      step_id: step.step_id,
      kind: "tool",
      logical_tool_name: step.logical_tool_name,
      dispatch_tool_name: step.dispatch_tool_name,
      effect: step.effect,
      logical_tool_revision: step.logical_tool_revision,
      dispatch_tool_revision: step.dispatch_tool_revision,
      logical_schema_sha256: step.logical_schema_sha256,
      dispatch_schema_sha256: step.dispatch_schema_sha256,
      result_type: step.result_type,
      max_result_bytes: step.max_result_bytes,
      call_id: `wf_fixture_${index + 1}`,
      status: index === 0 ? "completed" : "approval_required",
      error_code: null,
    }));
    return projection(runId, "fixture Workflow projection", "paused", {
      app_id: workflowApplication.id,
      pause_reason: "approval_required",
      detail: "awaiting_approval",
      step: 2,
      latest_seq: 0,
      final_message: null,
      pending: {
        call_id: "wf_fixture_2",
        tool_name: "wf_record_fixture",
        effect: "side_effecting",
        tool_revision: "fixture-wrapper-v1",
        fingerprint: "fixture-workflow-fingerprint",
        idempotency_key: null,
        arguments: { redacted_from_workflow_extension: true },
      },
      workflow: {
        schema_version: 1,
        workflow_id: workflowApplication.workflow.workflow_id,
        version: workflowApplication.workflow.version,
        definition_sha256: workflowApplication.workflow.definition_sha256,
        app_id: workflowApplication.id,
        execution: workflowApplication.workflow.execution,
        output_step: "record",
        current_step_id: "record",
        latest_seq: 0,
        steps,
      },
    });
  }

  function workflowProjectionCase(runId, kind) {
    const value = JSON.parse(JSON.stringify(workflowProjection(runId)));
    value.pause_reason = null;
    value.pending = null;
    value.final_message = null;
    if (kind === "requested-pending") {
      value.state = "running";
      value.detail = "processing_reply";
      value.step = 1;
      value.latest_seq = 1;
      value.workflow.latest_seq = 1;
      value.workflow.current_step_id = "inspect";
      value.workflow.steps[0].status = "requested";
      value.workflow.steps[1].status = "pending";
      value.workflow.steps[1].call_id = null;
    } else if (kind === "failed-stopped") {
      value.state = "failed";
      value.detail = "failed";
      value.step = 1;
      value.latest_seq = 2;
      value.workflow.latest_seq = 2;
      value.workflow.current_step_id = null;
      value.workflow.steps[0].status = "failed";
      value.workflow.steps[0].error_code = "workflow_result_type";
      value.workflow.steps[1].status = "stopped";
      value.workflow.steps[1].call_id = null;
    } else if (kind === "cancelled-unknown") {
      value.state = "cancelled";
      value.pause_reason = "effect_unknown";
      value.detail = "cancelled";
      value.step = 2;
      value.latest_seq = 3;
      value.workflow.latest_seq = 3;
      value.workflow.current_step_id = null;
      value.workflow.steps[0].status = "completed";
      value.workflow.steps[1].status = "effect_unknown";
      value.pending = {
        call_id: "wf_fixture_2",
        tool_name: "wf_record_fixture",
        effect: "side_effecting",
        tool_revision: "fixture-wrapper-v1",
        fingerprint: "fixture-cancelled-fingerprint",
        idempotency_key: null,
        arguments: { private_recovery_input: true },
      };
    } else if (kind === "cancelled-resolved") {
      value.state = "cancelled";
      value.detail = "cancelled";
      value.step = 2;
      value.latest_seq = 5;
      value.revision = 2;
      value.workflow.latest_seq = 5;
      value.workflow.current_step_id = null;
      value.workflow.steps[0].status = "completed";
      value.workflow.steps[1].status = "failed";
      value.workflow.steps[1].error_code = "manual_recovery_failed";
    } else {
      throw new Error(`unknown Workflow projection fixture: ${kind}`);
    }
    return value;
  }

  function workflowProjectionAtCursor(runId, latestSeq, revision) {
    const value = workflowProjection(runId);
    value.latest_seq = latestSeq;
    value.revision = revision;
    value.workflow.latest_seq = latestSeq;
    return value;
  }

  function projectedEvent(runId, type = "run.completed", seq = 1) {
    return {
      seq,
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

  function eventBatch(runId, events = [], afterSeq = 0, latestSeq = events.length) {
    return json({
      run_id: runId,
      after_seq: afterSeq,
      latest_seq: latestSeq,
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
      return json({
        schema_version: 1,
        apps: [application, workflowApplication, unavailableWorkflowApplication],
      });
    }
    if (method === "GET" && url.pathname === "/v1/runs") {
      return json({
        items: [
          { run_id: "run-old", state: "completed", input_preview: "old fixture run" },
          { run_id: "run-new", state: "completed", input_preview: "new fixture run" },
          { run_id: "run-workflow", state: "paused", input_preview: "fixture Workflow projection" },
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
    if (method === "GET" && url.pathname === "/v1/runs/run-workflow") {
      if (mode === "cancelled-recovery") {
        return json(workflowProjectionCase(
          "run-workflow",
          cancelledRecoveryResolved ? "cancelled-resolved" : "cancelled-unknown",
        ));
      }
      if (["workflow-refresh-burst", "workflow-refresh-switch"].includes(mode)) {
        workflowStatusCount += 1;
        workflowStatusInFlight += 1;
        workflowStatusMaxInFlight = Math.max(
          workflowStatusMaxInFlight,
          workflowStatusInFlight,
        );
        try {
          if (workflowStatusCount === 1) {
            return await defer(`${mode}-status`);
          }
          const latestSeq = mode === "workflow-refresh-burst" ? 20 : 1;
          return json(workflowProjectionAtCursor(
            "run-workflow",
            latestSeq,
            workflowStatusCount + 1,
          ));
        } finally {
          workflowStatusInFlight -= 1;
        }
      }
      return json(workflowProjection("run-workflow"));
    }
    if (method === "GET" && url.pathname === "/v1/runs/run-workflow/events") {
      if (mode === "cancelled-recovery") {
        const afterSeq = Number(url.searchParams.get("after_seq") || "0");
        return eventBatch(
          "run-workflow",
          [],
          afterSeq,
          cancelledRecoveryResolved ? 5 : 3,
        );
      }
      if (mode === "workflow-refresh-burst") {
        const afterSeq = Number(url.searchParams.get("after_seq") || "0");
        return eventBatch("run-workflow", [], afterSeq, 20);
      }
      return eventBatch("run-workflow");
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
    if (method === "POST" && url.pathname === "/v1/runs/run-workflow/effect" &&
        mode === "cancelled-recovery") {
      cancelledRecoveryBody = JSON.parse(await bodyFor(input, options));
      cancelledRecoveryCount += 1;
      cancelledRecoveryResolved = true;
      return json(workflowProjectionCase("run-workflow", "cancelled-resolved"));
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
    get workflowStatusCount() { return workflowStatusCount; },
    get workflowStatusInFlight() { return workflowStatusInFlight; },
    get workflowStatusMaxInFlight() { return workflowStatusMaxInFlight; },
    get cancelledRecoveryCount() { return cancelledRecoveryCount; },
    get cancelledRecoveryBody() { return cancelledRecoveryBody; },
    pendingCount,
    projection,
    workflowProjection,
    workflowProjectionAtCursor,
    workflowProjectionCase,
    projectedEvent,
    eventBatch,
    json,
    reset(nextMode) {
      mode = nextMode;
      oldStatusCount = 0;
      oldEventHistoryCount = 0;
      workflowStatusCount = 0;
      workflowStatusInFlight = 0;
      workflowStatusMaxInFlight = 0;
      cancelledRecoveryCount = 0;
      cancelledRecoveryBody = null;
      cancelledRecoveryResolved = false;
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

  async function workflowSurfaceCase() {
    const card = document.querySelector('.worker-card[data-app-id="flow.fixture.aaaaaaaaaaaa"]');
    assert(card, "Workflow worker card is not visible");
    card.click();
    document.querySelector("#surface-tab").click();
    const surface = document.querySelector(".workflow-surface");
    assert(surface, "Workflow ordered-step surface is not visible");
    const text = surface.textContent;
    assert(text.includes("fixture-mechanism"), "Workflow identity is not visible");
    assert(text.includes("inspect_incident"), "logical Tool is not visible");
    assert(text.includes("wf_inspect_fixture"), "wrapper Tool is not visible");
    assert(text.includes("SERIAL ONLY"), "serial-only boundary is not visible");
    assert(text.includes("NO BRANCHES"), "branch non-goal is not visible");
    assert(surface.querySelectorAll(".workflow-step").length === 2, "ordered step count is invalid");
    assert(surface.querySelectorAll('[data-step-status="pending"]').length === 2, "definition preview invented durable progress");
    await open("run-workflow");
    await waitFor(() => document.querySelector("#active-run-label").textContent === "run-workflow", "Workflow run projection did not load");
    document.querySelector("#surface-tab").click();
    const projected = document.querySelector(".workflow-surface");
    const statuses = [...projected.querySelectorAll(".workflow-step")].map((step) => step.dataset.stepStatus);
    assert(JSON.stringify(statuses) === JSON.stringify(["completed", "approval_required"]), "Workbench did not consume the public Workflow projection");
    assert(projected.textContent.includes("HUMAN GATE"), "public Workflow approval gate is not visible");
    document.querySelector('.worker-card[data-app-id="incident-response"]').click();
    document.querySelector("#timeline-tab").click();
    record("workflow-surface");
  }

  function workflowProjectionContractCase(fixture) {
    const app = state.apps.find((item) => item.id === "flow.fixture.aaaaaaaaaaaa");
    const contract = workflowContract(app);
    assert(app && contract, "Workflow production contract is unavailable to the fixture");

    const requested = workflowRunProjection(
      app,
      contract,
      fixture.workflowProjectionCase("run-workflow", "requested-pending"),
    );
    assert(
      JSON.stringify(requested.steps.map((step) => [step.status, step.callId])) ===
        JSON.stringify([["requested", "wf_fixture_1"], ["pending", null]]),
      "requested and pending nullable call bindings were rejected",
    );

    const failed = workflowRunProjection(
      app,
      contract,
      fixture.workflowProjectionCase("run-workflow", "failed-stopped"),
    );
    assert(
      failed.currentStepId === null && failed.steps[0].status === "failed" &&
        failed.steps[1].status === "stopped" && failed.steps[1].callId === null,
      "terminal failed and stopped projection was rejected",
    );

    const cancelled = workflowRunProjection(
      app,
      contract,
      fixture.workflowProjectionCase("run-workflow", "cancelled-unknown"),
    );
    assert(
      cancelled.currentStepId === null && cancelled.steps[0].status === "completed" &&
        cancelled.steps[1].status === "effect_unknown",
      "cancelled mutable ambiguity was hidden or rejected",
    );

    for (const nestedCursor of [3, 5]) {
      const mismatched = fixture.workflowProjectionAtCursor("run-workflow", 4, 1);
      mismatched.workflow.latest_seq = nestedCursor;
      let rejected = false;
      try {
        workflowRunProjection(app, contract, mismatched);
      } catch (error) {
        rejected = /binding is invalid/.test(String(error && error.message));
      }
      assert(rejected, `Workflow nested cursor ${nestedCursor} did not fail closed`);
    }
    record("workflow-projection-contract");
  }

  async function cancelledRecoveryCase(fixture) {
    fixture.reset("cancelled-recovery");
    await open("run-workflow");
    await waitFor(
      () => document.querySelector("#operator-action .recovery-card"),
      "cancelled effect recovery form was not rendered",
    );
    const form = document.querySelector("#operator-action .recovery-card");
    const action = form.querySelector(".recovery-action");
    assert(!action.querySelector('option[value="retry"]'),
      "cancelled recovery offered a forbidden retry action");
    assert(form.querySelector(".cancelled-recovery-note"),
      "cancelled recovery policy was not disclosed");
    action.value = "fail";
    action.dispatchEvent(new Event("change", { bubbles: true }));
    form.querySelector(".recovery-reason").value = "fixture outcome could not be verified";
    form.requestSubmit();
    await waitFor(
      () => fixture.cancelledRecoveryCount === 1,
      "cancelled recovery decision was not submitted",
    );
    assert(
      JSON.stringify(fixture.cancelledRecoveryBody) === JSON.stringify({
        fingerprint: "fixture-cancelled-fingerprint",
        action: "fail",
        reason: "fixture outcome could not be verified",
        result: null,
      }),
      "cancelled recovery request changed its bounded contract",
    );
    await waitFor(
      () => document.querySelectorAll('.workflow-step[data-step-status="failed"]').length === 1,
      "cancelled recovery result did not update the Workflow rail",
    );
    assert(document.querySelector("#operator-action").hidden,
      "resolved cancelled effect kept an actionable recovery form");
    await waitFor(
      () => document.querySelector("#toast-region .toast:last-child")?.textContent.includes(
        "运行保持已取消状态",
      ),
      "cancelled recovery terminal toast was not rendered",
    );
    const recoveryToast = document.querySelector("#toast-region .toast:last-child");
    assert(recoveryToast && recoveryToast.textContent.includes("运行保持已取消状态"),
      "cancelled recovery did not disclose its terminal outcome");
    assert(!recoveryToast.textContent.includes("显式恢复"),
      "cancelled recovery incorrectly invited Loop re-entry");
    record("cancelled-recovery");
  }

  async function workflowRefreshBurstCase(fixture) {
    fixture.reset("workflow-refresh-burst");
    const context = activateRun("run-workflow");
    setRunProjection(fixture.workflowProjectionAtCursor("run-workflow", 20, 1), context);
    addEvent(fixture.projectedEvent("run-workflow", "fixture.refresh", 1));
    await waitFor(
      () => fixture.pendingCount("workflow-refresh-burst-status") === 1,
      "first Workflow status refresh was not delayed",
    );
    for (let seq = 2; seq <= 20; seq += 1) {
      addEvent(fixture.projectedEvent("run-workflow", "fixture.refresh", seq));
    }
    await tick();
    assert(fixture.workflowStatusInFlight === 1, "Workflow refresh lost its active request");
    assert(fixture.workflowStatusMaxInFlight === 1, "Workflow refresh requests overlapped");
    fixture.resolveNext(
      "workflow-refresh-burst-status",
      fixture.json(fixture.workflowProjectionAtCursor("run-workflow", 20, 2)),
    );
    await waitFor(
      () => fixture.workflowStatusCount === 2 && fixture.workflowStatusInFlight === 0 &&
        state.run && state.run.run_id === "run-workflow" && state.run.revision === 3,
      "coalesced Workflow follow-up refresh did not settle",
    );
    assert(fixture.workflowStatusMaxInFlight === 1, "Workflow refresh exceeded one in-flight GET");
    assert(state.run.latest_seq === 20 && state.run.workflow.latest_seq === 20,
      "Workflow refresh did not retain the greatest coherent cursor");
    await open("run-new");
    await waitForSelected("run-new", "fresh selected run");
    record("workflow-refresh-burst");
  }

  async function workflowRefreshSwitchCase(fixture) {
    fixture.reset("workflow-refresh-switch");
    const context = activateRun("run-workflow");
    setRunProjection(fixture.workflowProjectionAtCursor("run-workflow", 1, 1), context);
    addEvent(fixture.projectedEvent("run-workflow", "fixture.refresh", 1));
    await waitFor(
      () => fixture.pendingCount("workflow-refresh-switch-status") === 1,
      "old Workflow refresh was not delayed",
    );
    await open("run-new");
    await waitForSelected("run-new", "fresh selected run");
    fixture.resolveNext(
      "workflow-refresh-switch-status",
      fixture.json(fixture.workflowProjectionAtCursor("run-workflow", 1, 2)),
    );
    await tick();
    await tick();
    assertFreshNewView("workflow-refresh-switch");
    assert(fixture.workflowStatusMaxInFlight === 1,
      "old Workflow refresh overlapped another status request");
    record("workflow-refresh-switch");
  }

  function unavailableWorkflowCase() {
    const card = document.querySelector('.worker-card[data-app-id="flow.unavailable.ffffffffffff"]');
    assert(card, "unavailable Workflow worker card is not visible");
    assert(card.disabled, "unavailable Workflow worker is selectable");
    assert(card.classList.contains("unavailable"), "unavailable Workflow state is not disclosed");
    assert(!card.querySelector(".workflow-worker-badge"), "unbound Workflow invented a dispatch badge");
    assert(document.querySelector("#connection-label").textContent === "运行时就绪", "unavailable Workflow broke catalog readiness");
    const selected = document.querySelector("#selected-worker-label").textContent;
    card.click();
    assert(document.querySelector("#selected-worker-label").textContent === selected, "unavailable Workflow changed the selected application");
    record("unavailable-workflow");
  }

  async function run() {
    const fixture = global.__sasoriFixture;
    await waitFor(
      () => global.SasoriEventReducer && historyButton("run-old") && historyButton("run-new") &&
        !document.querySelector("#run-button").disabled,
      "production Workbench did not finish initial loading",
    );
    unavailableWorkflowCase();
    memorySkillSurfaceCase();
    await workflowSurfaceCase();
    workflowProjectionContractCase(fixture);
    await cancelledRecoveryCase(fixture);
    await workflowRefreshBurstCase(fixture);
    await workflowRefreshSwitchCase(fixture);
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
