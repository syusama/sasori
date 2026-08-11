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
  let studioPreflightCount = 0;
  let lastStudioBody = null;
  let workflowCatalogRecord = null;
  let workflowCatalogPutCount = 0;
  let workflowCatalogGetCount = 0;
  let lastWorkflowCatalogBody = null;
  let lastWorkflowCatalogId = null;
  let workflowCatalogExtraRecord = null;
  let workflowCatalogDetailDelay = null;
  let workflowCatalogPutBehavior = null;

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
      capability_kind: "plugin",
      transport_type: null,
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
      app_id: "flow.fixture.aaaaaaaaaaaa",
      execution: "single-harness-ordered-tools-v1",
      output_step: "record",
      step_count: 2,
      supports_parallel: false,
      supports_branches: false,
      supports_agent_nodes: false,
      trust: { execution_mode: "trusted_installed_python", sandboxed: false },
      inputs: [{ key: "incident", type: "string", required: true, max_bytes: 16384 }],
      steps: [
        {
          position: 1,
          step_id: "inspect",
          depends_on: [],
          argument_sources: [{ name: "summary", kind: "input", ref: "incident" }],
          logical_tool_name: "inspect_incident",
          dispatch_tool_name: "wf_inspect_fixture",
          effect: "read_only",
          requires_approval: false,
          recovery_policy: "read_only_replay_allowed",
          logical_tool_revision: "fixture-inspect-v1",
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
          depends_on: ["inspect"],
          argument_sources: [{ name: "summary", kind: "step", ref: "inspect" }],
          logical_tool_name: "record_action",
          dispatch_tool_name: "wf_record_fixture",
          effect: "side_effecting",
          requires_approval: true,
          recovery_policy: "manual_effect_resolution_on_ambiguity",
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
      capability_kind: "plugin",
      transport_type: null,
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
    id: "flow.unavailable-mechanism.ffffffffffff",
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
      app_id: "flow.unavailable-mechanism.ffffffffffff",
      steps: workflowApplication.workflow.steps.map((step) => ({ ...step })),
    },
    skills: workflowApplication.skills.map((skill) => ({
      ...skill,
      tool_names: ["inspect_incident", "record_action"],
    })),
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

  function json(payload, status = 200, extraHeaders = {}) {
    return new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8", ...extraHeaders },
    });
  }

  function studioManifest(definition) {
    const positions = Object.fromEntries(
      definition.steps.map((step, index) => [step.step_id, index]),
    );
    const policies = {
      read_only: [false, "read_only_replay_allowed"],
      idempotent: [true, "same_verified_business_key_only"],
      side_effecting: [true, "manual_effect_resolution_on_ambiguity"],
    };
    const steps = definition.steps.map((step, index) => {
      const dependencies = [];
      const argumentSources = Object.keys(step.arguments).sort().map((name) => {
        const binding = step.arguments[name];
        if (binding.kind === "input") return { name, kind: "input", ref: binding.key };
        if (binding.kind === "step_output") {
          if (!dependencies.includes(binding.step_id)) dependencies.push(binding.step_id);
          return { name, kind: "step", ref: binding.step_id };
        }
        const encoded = JSON.stringify(binding.value);
        return {
          name,
          kind: "literal",
          value_type: binding.value === null ? "null" : Array.isArray(binding.value)
            ? "array" : typeof binding.value === "object" ? "object"
              : typeof binding.value === "boolean" ? "boolean"
                : typeof binding.value === "number" && Number.isInteger(binding.value)
                  ? "integer" : typeof binding.value,
          canonical_bytes: new TextEncoder().encode(encoded).byteLength,
          value_sha256: "a".repeat(64),
        };
      });
      dependencies.sort((left, right) => positions[left] - positions[right]);
      const policy = policies[step.effect];
      return {
        position: index + 1,
        step_id: step.step_id,
        depends_on: dependencies,
        argument_sources: argumentSources,
        logical_tool_name: step.tool_name,
        dispatch_tool_name: `wf_studio_${index + 1}`,
        effect: step.effect,
        requires_approval: policy[0],
        recovery_policy: policy[1],
        logical_tool_revision: step.tool_revision,
        dispatch_tool_revision: step.effect === "read_only" ? null : `fixture-wrapper-${index + 1}`,
        logical_schema_sha256: step.schema_sha256,
        dispatch_schema_sha256: String((index + 1) % 10).repeat(64),
        result_type: step.result.type,
        max_result_bytes: step.result.max_bytes,
        is_output: step.step_id === definition.output_step,
      };
    });
    const appId = `flow.${definition.workflow_id}.999999999999`;
    return {
      schema_version: 1,
      workflow_id: definition.workflow_id,
      version: definition.version,
      definition_sha256: "9".repeat(64),
      app_id: appId,
      execution: definition.execution,
      output_step: definition.output_step,
      step_count: steps.length,
      supports_parallel: false,
      supports_branches: false,
      supports_agent_nodes: false,
      trust: { execution_mode: "trusted_installed_python", sandboxed: false },
      inputs: definition.inputs.map((input) => ({
        key: input.key,
        type: input.type,
        required: input.required,
        max_bytes: input.max_bytes,
      })),
      steps,
    };
  }

  function studioResponse(definition, extra = {}) {
    return {
      ok: true,
      schema_version: 1,
      manifest: studioManifest(definition),
      ...extra,
    };
  }

  async function definitionDigest(definition) {
    const canonical = JSON.stringify(canonicalValue(definition));
    const digest = await crypto.subtle.digest(
      "SHA-256", new TextEncoder().encode(canonical),
    );
    return [...new Uint8Array(digest)]
      .map((value) => value.toString(16).padStart(2, "0")).join("");
  }

  function canonicalValue(value) {
    if (Array.isArray(value)) return value.map(canonicalValue);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value).sort().map(
        (key) => [key, canonicalValue(value[key])],
      ));
    }
    return value;
  }

  async function workflowCatalogValue(catalogId, definition, revision = 1) {
    const manifest = studioManifest(definition);
    const digest = await definitionDigest(definition);
    manifest.definition_sha256 = digest;
    manifest.app_id = `flow.${definition.workflow_id}.${digest.slice(0, 12)}`;
    const record = {
      catalog_id: catalogId,
      catalog_revision: revision,
      parent_revision: revision === 1 ? null : revision - 1,
      definition_sha256: digest,
      definition: structuredClone(definition),
      saved_manifest: manifest,
      head_revision: revision,
      is_head: true,
      current_contract: { status: "compatible", reason_code: null },
    };
    const etag = `"sasori-wfcat-${catalogId.slice(6)}-r${revision}-${digest}"`;
    return { record, etag };
  }

  function workflowCatalogResponse(value = workflowCatalogRecord) {
    return json({ ok: true, schema_version: 1, record: value.record }, 200, {
      ETag: value.etag,
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
    if (method === "GET" && url.pathname === "/v1/workflows") {
      if (mode === "workflow-catalog-pagination") {
        const before = url.searchParams.get("before");
        const start = before === null ? 1 : 101;
        const end = before === null ? 100 : 101;
        const items = Array.from({ length: end - start + 1 }, (_, offset) => {
          const sequence = start + offset;
          return {
            catalog_id: `wfcat_000000000000400080000000${String(sequence).padStart(8, "0")}`,
            catalog_revision: 1,
            workflow_id: `paged-${sequence}`,
            definition_version: "1",
            definition_sha256: "a".repeat(64),
          };
        });
        return json({
          ok: true,
          schema_version: 1,
          items,
          next_before: before === null ? 101 : null,
        });
      }
      if (mode === "workflow-catalog-record-switch" && !workflowCatalogExtraRecord) {
        const definition = structuredClone(workflowCatalogRecord.record.definition);
        definition.version = "switch-b";
        workflowCatalogExtraRecord = await workflowCatalogValue(
          "wfcat_123e4567e89b42d3a456426614174001", definition,
        );
      }
      const records = [workflowCatalogRecord, workflowCatalogExtraRecord].filter(Boolean);
      const items = records.map((value) => ({
        catalog_id: value.record.catalog_id,
        catalog_revision: value.record.catalog_revision,
        workflow_id: value.record.definition.workflow_id,
        definition_version: value.record.definition.version,
        definition_sha256: value.record.definition_sha256,
      }));
      return json({ ok: true, schema_version: 1, items, next_before: null });
    }
    const savedWorkflow = url.pathname.match(/^\/v1\/workflows\/(wfcat_[0-9a-f]{32})$/);
    if (method === "GET" && savedWorkflow) {
      workflowCatalogGetCount += 1;
      const value = [workflowCatalogRecord, workflowCatalogExtraRecord]
        .find((candidate) => candidate && candidate.record.catalog_id === savedWorkflow[1]);
      if (!value) {
        return json({
          ok: false,
          error: {
            code: "workflow_catalog_not_found",
            message: "saved Workflow or revision was not found",
            retryable: false,
          },
        }, 404);
      }
      if (mode === "workflow-catalog-digest-mismatch") {
        const tampered = structuredClone(value);
        tampered.record.definition.steps[0].result.max_bytes += 1;
        return workflowCatalogResponse(tampered);
      }
      if (workflowCatalogDetailDelay === savedWorkflow[1]) {
        workflowCatalogDetailDelay = null;
        return defer(`workflow-catalog-detail-${savedWorkflow[1]}`);
      }
      return workflowCatalogResponse(value);
    }
    if (method === "PUT" && savedWorkflow) {
      workflowCatalogPutCount += 1;
      lastWorkflowCatalogId = savedWorkflow[1];
      lastWorkflowCatalogBody = JSON.parse(await bodyFor(input, options));
      const previousRevision = workflowCatalogRecord &&
        workflowCatalogRecord.record.catalog_id === lastWorkflowCatalogId
        ? workflowCatalogRecord.record.catalog_revision : 0;
      if (mode === "workflow-catalog-conflict") {
        const serverDefinition = structuredClone(lastWorkflowCatalogBody);
        serverDefinition.version = "server-head";
        workflowCatalogRecord = await workflowCatalogValue(
          lastWorkflowCatalogId, serverDefinition, previousRevision + 1,
        );
        return json({
          ok: false,
          error: {
            code: "workflow_catalog_revision_mismatch",
            message: "saved Workflow precondition did not match durable state",
            retryable: false,
          },
        }, 412);
      }
      workflowCatalogRecord = await workflowCatalogValue(
        lastWorkflowCatalogId,
        lastWorkflowCatalogBody,
        Math.max(1, previousRevision + (previousRevision ? 1 : 0)),
      );
      if (mode === "workflow-catalog-stale-edit") {
        return defer("workflow-catalog-save");
      }
      if (mode === "workflow-catalog-outcome-unknown" ||
          workflowCatalogPutBehavior === "outcome-unknown") {
        workflowCatalogPutBehavior = null;
        workflowCatalogDetailDelay = lastWorkflowCatalogId;
        return json({
          ok: false,
          error: {
            code: "workflow_catalog_outcome_unknown",
            message: "saved Workflow mutation outcome is unknown; reconcile with a read-only GET",
            retryable: false,
            catalog_id: lastWorkflowCatalogId,
          },
        }, 504);
      }
      if (mode === "workflow-catalog-malformed-success") {
        return json({ ok: true }, 200, { ETag: workflowCatalogRecord.etag });
      }
      if (workflowCatalogPutBehavior === "delay-success") {
        workflowCatalogPutBehavior = null;
        return defer("workflow-catalog-put");
      }
      return json(
        { ok: true, schema_version: 1, record: workflowCatalogRecord.record },
        workflowCatalogRecord.record.catalog_revision === 1 ? 201 : 200,
        {
          ETag: workflowCatalogRecord.etag,
          Location: `/v1/workflows/${lastWorkflowCatalogId}`,
        },
      );
    }
    if (method === "POST" && url.pathname === "/v1/workflows/preflight") {
      lastStudioBody = JSON.parse(await bodyFor(input, options));
      studioPreflightCount += 1;
      if (mode === "workflow-studio-stale-edit") {
        return defer("workflow-studio-preflight");
      }
      if (mode === "workflow-studio-contract") {
        return json(studioResponse(lastStudioBody, { unexpected: true }));
      }
      if (mode === "workflow-studio-rejected") {
        return json({
          ok: false,
          error: {
            code: "workflow_preflight_rejected",
            message: "fixture Tool contract drift",
            retryable: false,
            reason_code: "tool_contract_mismatch",
          },
        }, 422);
      }
      if (mode === "workflow-studio-malformed-rejection") {
        const variants = [
          {
            ok: false,
            error: {
              code: "workflow_preflight_rejected",
              message: "missing reason",
              retryable: false,
            },
          },
          {
            ok: false,
            error: {
              code: "workflow_preflight_rejected",
              message: "extra field",
              retryable: false,
              reason_code: "tool_contract_mismatch",
              unexpected: true,
            },
          },
          {
            ok: false,
            error: {
              code: "workflow_preflight_rejected",
              message: "unknown reason",
              retryable: false,
              reason_code: "unknown",
            },
          },
          {
            ok: false,
            error: {
              code: "workflow_preflight_rejected",
              message: "wrong retry policy",
              retryable: true,
              reason_code: "tool_contract_mismatch",
            },
          },
          {
            ok: true,
            error: {
              code: "workflow_preflight_rejected",
              message: "wrong top-level verdict",
              retryable: false,
              reason_code: "tool_contract_mismatch",
            },
          },
          {
            ok: false,
            error: {
              code: "workflow_preflight_rejected",
              message: "",
              retryable: false,
              reason_code: "tool_contract_mismatch",
            },
          },
          {
            ok: false,
            error: {
              code: "workflow_preflight_rejected",
              message: "x".repeat(513),
              retryable: false,
              reason_code: "tool_contract_mismatch",
            },
          },
          {
            ok: false,
            error: {
              code: "workflow_preflight_rejected",
              message: "\ud800",
              retryable: false,
              reason_code: "tool_contract_mismatch",
            },
          },
        ];
        return json(variants[studioPreflightCount - 1] || variants.at(-1), 422);
      }
      if (mode === "workflow-studio-transport") {
        return json({
          ok: false,
          error: {
            code: "runtime_busy",
            message: "runtime owner did not respond",
            retryable: true,
          },
        }, 503);
      }
      return json(studioResponse(lastStudioBody));
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
    get studioPreflightCount() { return studioPreflightCount; },
    get lastStudioBody() { return lastStudioBody; },
    get workflowCatalogPutCount() { return workflowCatalogPutCount; },
    get workflowCatalogGetCount() { return workflowCatalogGetCount; },
    get lastWorkflowCatalogBody() { return lastWorkflowCatalogBody; },
    get lastWorkflowCatalogId() { return lastWorkflowCatalogId; },
    get workflowCatalogRecord() { return workflowCatalogRecord; },
    get workflowCatalogExtraRecord() { return workflowCatalogExtraRecord; },
    pendingCount,
    projection,
    workflowProjection,
    workflowProjectionAtCursor,
    workflowProjectionCase,
    projectedEvent,
    eventBatch,
    json,
    studioResponse,
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
      studioPreflightCount = 0;
      lastStudioBody = null;
      workflowCatalogPutCount = 0;
      workflowCatalogGetCount = 0;
      lastWorkflowCatalogBody = null;
      lastWorkflowCatalogId = null;
      workflowCatalogExtraRecord = null;
      workflowCatalogDetailDelay = null;
      workflowCatalogPutBehavior = null;
    },
    delayWorkflowCatalogDetail(catalogId) {
      workflowCatalogDetailDelay = catalogId;
    },
    delayNextWorkflowCatalogPut() {
      workflowCatalogPutBehavior = "delay-success";
    },
    outcomeUnknownNextWorkflowCatalogPut() {
      workflowCatalogPutBehavior = "outcome-unknown";
    },
    resolveNext,
    workflowCatalogResponse,
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

  function workbenchShellCase() {
    const destinations = [...document.querySelectorAll("[data-workbench-destination]")];
    assert(destinations.length === 5, "Workbench command navigation is incomplete");
    assert(JSON.stringify(destinations.map((item) => item.dataset.workbenchDestination)) ===
      JSON.stringify(["command", "workflows", "capabilities", "artifacts", "trace"]),
    "Workbench command navigation order drifted");

    for (const side of ["left", "right"]) {
      const separator = document.querySelector(`#${side}-separator`);
      assert(separator.getAttribute("role") === "separator" &&
        separator.getAttribute("aria-orientation") === "vertical" &&
        separator.tabIndex === 0, `${side} panel separator is not keyboard reachable`);
      assert(Number(separator.getAttribute("aria-valuemin")) <
        Number(separator.getAttribute("aria-valuenow")) &&
        Number(separator.getAttribute("aria-valuenow")) <
        Number(separator.getAttribute("aria-valuemax")),
      `${side} panel separator range is invalid`);
    }

    if (global.innerWidth > 940) {
      const left = document.querySelector("#left-separator");
      left.focus();
      left.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
      assert(left.getAttribute("aria-valuenow") === "306", "left separator ArrowRight did not resize the panel");
      left.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true }));
      assert(left.getAttribute("aria-valuenow") === "220", "left separator Home did not select its minimum");
      left.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true, cancelable: true }));
      assert(left.getAttribute("aria-valuenow") === "380", "left separator End did not select its maximum");
      left.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
      assert(left.getAttribute("aria-valuenow") === "286", "left separator reset is not deterministic");

      const columns = global.getComputedStyle(document.querySelector("#workbench-shell"))
        .gridTemplateColumns.split(" ");
      assert(columns.length === 5, "desktop Workbench is not a three-panel layout with two separators");
    } else {
      const profile = new URLSearchParams(global.location.hash.slice(1)).get("profile");
      const expectedWidth = profile === "narrow-360-reduced" ? 360 : 390;
      assert(global.innerWidth === expectedWidth,
        `narrow browser viewport is ${global.innerWidth}px, expected ${expectedWidth}px`);
      assert(global.getComputedStyle(document.querySelector("#left-separator")).display === "none",
        "narrow Workbench retained a desktop separator");
      assert(global.getComputedStyle(document.querySelector(".mobile-nav")).display === "grid",
        "narrow Workbench did not expose hierarchical navigation");
      assert(global.getComputedStyle(document.querySelector("#studio-button")).display === "flex",
        "narrow Workbench hid the Workflow Studio focus return target");
      assert(document.documentElement.scrollWidth <= global.innerWidth,
        `narrow Workbench overflows horizontally at ${global.innerWidth}px`);
      const composerBounds = document.querySelector("#task-form").getBoundingClientRect();
      const mobileNavBounds = document.querySelector(".mobile-nav").getBoundingClientRect();
      assert(composerBounds.left >= 0 && composerBounds.right <= global.innerWidth,
        "narrow Run composer is clipped horizontally");
      assert(mobileNavBounds.left >= 0 && mobileNavBounds.right <= global.innerWidth &&
        mobileNavBounds.top >= 0 && mobileNavBounds.bottom <= global.innerHeight,
      "narrow bottom navigation is clipped by the viewport");
      assert(composerBounds.bottom <= mobileNavBounds.top + 1,
        "narrow Run composer is obscured by bottom navigation");
    }

    const prompt = document.querySelector("[data-prompt]");
    const input = document.querySelector("#task-input");
    prompt.click();
    assert(input.value === prompt.dataset.prompt && document.activeElement === input,
      "quick command did not populate and focus the real Run composer");
    input.value = "";

    document.querySelector('[data-workbench-destination="capabilities"]').click();
    assert(!document.querySelector("#surface-panel").hidden &&
      document.activeElement === document.querySelector("#surface-tab"),
    "Capability Center navigation did not open the real inspector tab");
    const mcpFilter = document.querySelector('[data-capability-filter="mcp"]');
    for (const filter of document.querySelectorAll("[data-capability-filter]")) {
      assert(filter.getBoundingClientRect().height >= 44,
        "Capability Center filter is below the 44px touch target");
    }
    mcpFilter.click();
    const mcp = document.querySelector('[data-capability="mcp"]');
    assert(mcp && !mcp.hidden && mcp.textContent.includes("没有投影独立 MCP transport"),
      "Capability Center invented or hid the empty MCP projection");
    assert([...document.querySelectorAll("#surface-content > *")]
      .filter((section) => section !== mcp).every((section) => section.hidden),
    "Capability Center filter leaked unrelated sections");

    const skillsFilter = document.querySelector('[data-capability-filter="skills"]');
    skillsFilter.focus();
    skillsFilter.dispatchEvent(new KeyboardEvent("keydown", {
      key: "ArrowRight", bubbles: true, cancelable: true,
    }));
    assert(document.activeElement === document.querySelector('[data-capability-filter="tools"]') &&
      document.querySelector('[data-capability-filter="tools"]').getAttribute("aria-pressed") === "true",
    "Capability Center toolbar is not keyboard operable");
    document.querySelector('[data-capability-filter="all"]').click();

    document.querySelector('[data-workbench-destination="command"]').click();
    assert(document.body.dataset.mobileView === "stage" &&
      document.activeElement === document.querySelector("#workbench-main"),
    "Command Center navigation did not restore the Run surface and focus");
    record("workbench-shell");
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
    assert(text.includes("depends on") && text.includes("workflow input"),
      "Workflow dependency disclosure is not visible");
    assert(text.includes("approval") && text.includes("required"),
      "Workflow approval disclosure is not visible");
    assert(text.includes("recovery") &&
      text.includes("manual_effect_resolution_on_ambiguity"),
    "Workflow recovery disclosure is not visible");
    assert(text.includes("TRUSTED PYTHON") && text.includes("NO SANDBOX"),
      "Workflow trust boundary disclosure is not visible");
    assert(text.includes("fixture-inspect-v1"),
      "versioned read-only logical Tool revision is not visible");
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
    assert(contract.app_id === app.id && contract.output_step === "record",
      "Workflow static application binding is invalid");
    assert(contract.trust.execution_mode === "trusted_installed_python" &&
      contract.trust.sandboxed === false, "Workflow trust boundary is invalid");
    assert(JSON.stringify(contract.steps.map((step) => step.depends_on)) ===
      JSON.stringify([[], ["inspect"]]), "Workflow dependencies are invalid");
    assert(JSON.stringify(contract.steps.map((step) => step.requires_approval)) ===
      JSON.stringify([false, true]), "Workflow approval points are invalid");
    assert(JSON.stringify(contract.steps.map((step) => step.recovery_policy)) ===
      JSON.stringify(["read_only_replay_allowed", "manual_effect_resolution_on_ambiguity"]),
    "Workflow recovery policy is invalid");
    for (const mutation of ["missing", "extra"]) {
      const malformed = structuredClone(app);
      if (mutation === "missing") delete malformed.workflow.steps[0].recovery_policy;
      else malformed.workflow.steps[0].unexpected = true;
      let rejected = false;
      try {
        workflowContract(malformed);
      } catch (error) {
        rejected = /contract is invalid/.test(String(error && error.message));
      }
      assert(rejected, `Workflow manifest ${mutation} field did not fail closed`);
    }

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
    const appId = "flow.unavailable-mechanism.ffffffffffff";
    const card = document.querySelector(`.worker-card[data-app-id="${appId}"]`);
    assert(card, "unavailable Workflow worker card is not visible");
    assert(card.disabled, "unavailable Workflow worker is selectable");
    assert(card.classList.contains("unavailable"), "unavailable Workflow state is not disclosed");
    assert(!card.querySelector(".workflow-worker-badge"), "unbound Workflow invented a dispatch badge");
    const unavailable = state.apps.find((item) => item.id === appId);
    assert(unavailable.workflow.app_id === appId &&
      unavailable.workflow.steps.every((step) => typeof step.dispatch_tool_name === "string"),
    "unavailable Workflow erased its immutable preflight manifest");
    assert(document.querySelector("#connection-label").textContent === "运行时就绪", "unavailable Workflow broke catalog readiness");
    const selected = document.querySelector("#selected-worker-label").textContent;
    card.click();
    assert(document.querySelector("#selected-worker-label").textContent === selected, "unavailable Workflow changed the selected application");
    record("unavailable-workflow");
  }

  async function workflowStudioCase(fixture) {
    fixture.reset("workflow-studio-preflight");
    document.querySelector("#studio-button").click();
    await waitFor(
      () => !document.querySelector("#workflow-studio").hidden &&
        document.querySelectorAll(".studio-tool-chip").length > 0 &&
        document.querySelector("#studio-editor").value.includes('"schema_version": 1'),
      "Workflow Studio did not open with a Tool-bound draft",
    );
    const studio = document.querySelector("#workflow-studio");
    await waitFor(
      () => document.activeElement === document.querySelector("#studio-editor"),
      "Workflow Studio did not focus its editor on open",
    );
    assert(studio.getAttribute("role") === "dialog" &&
      studio.getAttribute("aria-modal") === "true" &&
      document.querySelector("#workbench-shell").inert &&
      document.querySelector(".topbar").inert,
    "Workflow Studio did not isolate its modal accessibility boundary");
    const focusable = [...studio.querySelectorAll(
      'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
    )].filter((item) => !item.hidden && item.getClientRects().length);
    const firstFocusable = focusable[0];
    const lastFocusable = focusable.at(-1);
    lastFocusable.focus();
    lastFocusable.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Tab", bubbles: true, cancelable: true,
    }));
    assert(document.activeElement === firstFocusable,
      "Workflow Studio Tab escaped after its last focusable control");
    firstFocusable.focus();
    firstFocusable.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Tab", shiftKey: true, bubbles: true, cancelable: true,
    }));
    assert(document.activeElement === lastFocusable,
      "Workflow Studio Shift+Tab escaped before its first focusable control");
    document.querySelector("#studio-editor").focus();
    const profile = new URLSearchParams(global.location.hash.slice(1)).get("profile");
    if (profile && profile.startsWith("narrow")) {
      assert(global.matchMedia("(max-width: 700px)").matches,
        "narrow browser profile did not activate its media query");
      assert(global.matchMedia("(prefers-reduced-motion: reduce)").matches,
        "reduced-motion browser profile was not active");
      assert(global.getComputedStyle(studio).animationName === "none",
        "Workflow Studio retained entrance motion under reduced motion");
      assert(global.getComputedStyle(document.querySelector("#studio-status")).display === "none",
        "narrow Workflow Studio did not apply its compact status treatment");
      const columns = global.getComputedStyle(
        document.querySelector(".studio-grid"),
      ).gridTemplateColumns.split(" ");
      assert(columns.length === 1, "narrow Workflow Studio did not collapse to one column");
      for (const selector of ["#studio-close", "#studio-preflight", "#studio-save"]) {
        const bounds = document.querySelector(selector).getBoundingClientRect();
        assert(bounds.left >= 0 && bounds.right <= global.innerWidth,
          `${selector} is clipped in the narrow Workflow Studio`);
      }
    }
    assert(studio.textContent.includes("SAVED AUTHORING") &&
      studio.textContent.includes("IMMUTABLE REVISIONS") &&
      studio.textContent.includes("NO ACTIVATION") &&
      studio.textContent.includes("NO EXECUTION") &&
      studio.textContent.includes("TRUSTED PYTHON") &&
      studio.textContent.includes("NO SANDBOX"),
    "Workflow Studio trust and non-execution boundaries are not visible");
    document.querySelector("#studio-editor").dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    }));
    await waitFor(
      () => document.querySelector("#studio-status").dataset.state === "accepted" &&
        document.querySelector(".studio-manifest-hero"),
      "Workflow Studio did not render the server manifest",
    );
    assert(fixture.studioPreflightCount === 1, "Workflow Studio submitted more than one preflight");
    assert(fixture.lastStudioBody && fixture.lastStudioBody.schema_version === 1,
      "Workflow Studio did not submit the exact definition object");
    const previewText = document.querySelector("#studio-preview").textContent;
    assert(previewText.includes("STATIC CONTRACT ACCEPTED") &&
      previewText.includes("read_only_replay_allowed") &&
      previewText.includes("TRUSTED INSTALLED PYTHON") &&
      previewText.includes("NO SANDBOX"),
    "Workflow Studio omitted manifest or trust disclosure");
    assert(!fixture.requests.some((request) => request.method === "POST" &&
      /\/v1\/runs(?:\/|$)/.test(request.path)),
    "Workflow Studio preflight triggered a run mutation");
    document.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Escape",
      bubbles: true,
      cancelable: true,
    }));
    await waitFor(
      () => studio.hidden && document.activeElement === document.querySelector("#studio-button"),
      "Escape did not close Workflow Studio and restore launch focus",
    );
    assert(document.querySelector("#studio-button").getAttribute("aria-expanded") === "false",
      "Escape did not restore the Workflow Studio disclosure state");
    assert(!document.querySelector("#workbench-shell").inert &&
      !document.querySelector(".topbar").inert,
    "Workflow Studio close did not restore background accessibility");
    assert(document.querySelector('[data-workbench-destination="command"]')
      .getAttribute("aria-current") === "page",
    "Escape did not restore the Command Center navigation state");
    document.querySelector("#studio-button").click();
    await waitFor(
      () => !studio.hidden,
      "Workflow Studio did not reopen for the remaining acceptance cases",
    );
    record("workflow-studio-preflight");
  }

  async function workflowStudioStaleEditCase(fixture) {
    fixture.reset("workflow-studio-stale-edit");
    const editor = document.querySelector("#studio-editor");
    document.querySelector("#studio-preflight").click();
    await waitFor(
      () => fixture.pendingCount("workflow-studio-preflight") === 1,
      "Workflow Studio preflight was not delayed",
    );
    const submitted = structuredClone(fixture.lastStudioBody);
    editor.value = editor.value.replace('"version": "1"', '"version": "2"');
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    assert(document.querySelector("#studio-status").dataset.state === "dirty",
      "editing did not invalidate the pending preflight");
    assert(!document.querySelector(".studio-manifest-hero"),
      "editing retained a successful manifest");
    fixture.resolveNext(
      "workflow-studio-preflight",
      fixture.json(fixture.studioResponse(submitted)),
    );
    await tick();
    await tick();
    assert(editor.value.includes('"version": "2"'), "stale response replaced draft B");
    assert(document.querySelector("#studio-status").dataset.state === "dirty",
      "stale response marked draft B accepted");
    assert(!document.querySelector(".studio-manifest-hero") &&
      !document.querySelector("#studio-preview").textContent.includes("999999999999"),
    "stale response exposed draft A manifest beside draft B");
    record("workflow-studio-stale-edit");
  }

  async function workflowStudioContractCase(fixture) {
    fixture.reset("workflow-studio-contract");
    document.querySelector("#studio-preflight").click();
    await waitFor(
      () => document.querySelector("#studio-status").dataset.state === "dirty" &&
        document.querySelector('.studio-error[data-state="unverified"]'),
      "malformed Workflow Studio response did not fail closed",
    );
    assert(document.querySelector("#studio-preview").textContent.includes(
      "Workflow preflight response contract is invalid",
    ), "Workflow Studio hid its fail-closed response rejection");
    assert(!document.querySelector(".studio-manifest-hero"),
      "malformed Workflow Studio response rendered a manifest");
    assert(document.querySelector("#studio-preview").textContent.includes("NO SERVER VERDICT"),
      "malformed success was presented as an authoritative rejection");
    document.querySelector("#studio-close").click();
    assert(document.querySelector("#workflow-studio").hidden,
      "Workflow Studio close did not restore the Workbench");
    record("workflow-studio-contract");
  }

  async function workflowStudioRejectedCase(fixture) {
    fixture.reset("workflow-studio-rejected");
    document.querySelector("#studio-button").click();
    document.querySelector("#studio-preflight").click();
    await waitFor(
      () => document.querySelector("#studio-status").dataset.state === "rejected" &&
        document.querySelector('.studio-error[data-state="rejected"]'),
      "authoritative Workflow rejection was not rendered",
    );
    const preview = document.querySelector("#studio-preview").textContent;
    assert(preview.includes("PREFLIGHT REJECTED") &&
      preview.includes("tool_contract_mismatch") &&
      !preview.includes("NO SERVER VERDICT"),
    "authoritative Workflow rejection lost its taxonomy");
    document.querySelector("#studio-close").click();
    record("workflow-studio-rejected");
  }

  async function workflowStudioMalformedRejectionCase(fixture) {
    fixture.reset("workflow-studio-malformed-rejection");
    document.querySelector("#studio-button").click();
    const malformedCases = [
      "missing field",
      "extra field",
      "unknown reason",
      "retryable true",
      "ok true",
      "empty message",
      "long message",
      "invalid Unicode message",
    ];
    for (const [index, label] of malformedCases.entries()) {
      document.querySelector("#studio-preflight").click();
      await waitFor(
        () => fixture.studioPreflightCount === index + 1 &&
          document.querySelector("#studio-status").dataset.state === "dirty" &&
          document.querySelector('.studio-error[data-state="unverified"]'),
        `${label} Workflow rejection did not fail closed`,
      );
      const preview = document.querySelector("#studio-preview").textContent;
      assert(preview.includes("NO SERVER VERDICT") &&
        !preview.includes("PREFLIGHT REJECTED") &&
        !document.querySelector(".studio-manifest-hero"),
      `${label} Workflow rejection was presented as authoritative`);
    }
    document.querySelector("#studio-close").click();
    record("workflow-studio-malformed-rejection");
  }

  async function workflowStudioTransportCase(fixture) {
    fixture.reset("workflow-studio-transport");
    document.querySelector("#studio-button").click();
    document.querySelector("#studio-preflight").click();
    await waitFor(
      () => document.querySelector("#studio-status").dataset.state === "dirty" &&
        document.querySelector('.studio-error[data-state="unverified"]'),
      "retryable transport failure did not remain unverified",
    );
    const preview = document.querySelector("#studio-preview").textContent;
    assert(preview.includes("NO SERVER VERDICT") && preview.includes("runtime_busy") &&
      preview.includes("RETRYABLEyes") && !preview.includes("PREFLIGHT REJECTED"),
    "transport failure was presented as a definition rejection");
    document.querySelector("#studio-close").click();
    record("workflow-studio-transport");
  }

  async function workflowStudioInvalidUnicodeCase(fixture) {
    fixture.reset("workflow-studio-invalid-unicode");
    document.querySelector("#studio-button").click();
    const editor = document.querySelector("#studio-editor");
    const before = fixture.studioPreflightCount;
    editor.value += "\ud800";
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    assert(document.querySelector("#studio-byte-count").textContent.includes("INVALID UNICODE"),
      "unpaired Unicode surrogate was not disclosed");
    assert(document.querySelector("#studio-preflight").disabled,
      "unpaired Unicode surrogate remained submittable");
    assert(document.querySelector("#studio-status").dataset.state === "dirty" &&
      !document.querySelector(".studio-manifest-hero") &&
      fixture.studioPreflightCount === before,
    "unpaired Unicode surrogate reached fetch or retained a verdict");
    document.querySelector("#studio-close").click();
    record("workflow-studio-invalid-unicode");
  }

  async function workflowCatalogSaveCase(fixture) {
    fixture.reset("workflow-catalog-save");
    document.querySelector("#studio-button").click();
    document.querySelector("#studio-new").click();
    const editor = document.querySelector("#studio-editor");
    await waitFor(() => editor.value.includes('"schema_version": 1'),
      "new durable Workflow draft was not prepared");
    document.querySelector("#studio-save").click();
    await waitFor(
      () => fixture.workflowCatalogPutCount === 1,
      `Workflow catalog PUT did not fire; disabled=${document.querySelector("#studio-save").disabled}`,
    );
    await waitFor(
      () => document.querySelector("#studio-save-ledger").dataset.state !== "saving",
      `Workflow catalog save did not settle; GETs=${fixture.workflowCatalogGetCount}`,
    );
    const saveLedger = document.querySelector("#studio-save-ledger");
    const savedLabel = document.querySelector("#studio-record-label");
    assert(saveLedger.dataset.state === "saved" && savedLabel.textContent.includes("r1"),
      `Workflow catalog create did not become a durable saved head; state=${saveLedger.dataset.state}; ` +
      `ledger=${saveLedger.textContent}; label=${savedLabel.textContent}; ` +
      `catalog=${fixture.lastWorkflowCatalogId}; GETs=${fixture.workflowCatalogGetCount}`);
    assert(/^wfcat_[0-9a-f]{32}$/.test(fixture.lastWorkflowCatalogId),
      "Workflow catalog create did not use a collision-resistant catalog identity");
    assert(fixture.lastWorkflowCatalogBody.schema_version === 1,
      "Workflow catalog create did not submit the exact strict definition");
    await waitFor(
      () => document.querySelectorAll(".studio-catalog-card").length === 1,
      "saved Workflow did not appear in the durable catalog rail",
    );
    assert(!fixture.requests.some((request) => request.method === "POST" &&
      /\/v1\/runs(?:\/|$)/.test(request.path)),
    "saving a Workflow triggered a run mutation");
    record("workflow-catalog-save");
  }

  async function workflowCatalogStaleEditCase(fixture) {
    fixture.reset("workflow-catalog-stale-edit");
    const editor = document.querySelector("#studio-editor");
    editor.value = editor.value.replace('"version": "1"', '"version": "saved-a"');
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#studio-save").click();
    await waitFor(() => fixture.pendingCount("workflow-catalog-save") === 1,
      "Workflow catalog save was not delayed");
    editor.value = editor.value.replace('"version": "saved-a"', '"version": "draft-b"');
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    const durable = fixture.workflowCatalogRecord;
    fixture.resolveNext("workflow-catalog-save", fixture.json({
      ok: true,
      schema_version: 1,
      record: durable.record,
    }, 200, { ETag: durable.etag }));
    await waitFor(
      () => document.querySelector("#studio-save-ledger").dataset.state === "saved" &&
        document.querySelector("#studio-record-label").textContent.includes("DIRTY ON r2"),
      "stale save response did not retain a durable base under the newer draft",
    );
    assert(editor.value.includes('"version": "draft-b"'),
      "stale save response replaced the newer local draft");
    assert(!document.querySelector(".studio-manifest-hero"),
      "stale save response exposed the old manifest beside the newer draft");
    record("workflow-catalog-stale-edit");
  }

  async function workflowCatalogConflictCase(fixture) {
    fixture.reset("workflow-catalog-conflict");
    const editor = document.querySelector("#studio-editor");
    document.querySelector("#studio-save").click();
    await waitFor(
      () => fixture.workflowCatalogPutCount === 1 && fixture.workflowCatalogGetCount === 1 &&
        document.querySelector("#studio-save-ledger").dataset.state === "conflict" &&
        !document.querySelector("#studio-reconcile").hidden,
      "stale ETag did not produce a reconciled CAS conflict",
    );
    assert(editor.value.includes('"version": "draft-b"'),
      "CAS conflict overwrote the local draft with the server head");
    assert(fixture.workflowCatalogPutCount === 1,
      "CAS conflict automatically retried the mutation");
    document.querySelector("#studio-reconcile").click();
    assert(document.querySelector("#studio-save-ledger").dataset.state === "idle" &&
      document.querySelector("#studio-record-label").textContent.includes("DIRTY ON r3"),
    "explicit conflict rebase did not adopt only the server CAS head");
    assert(editor.value.includes('"version": "draft-b"'),
      "explicit conflict rebase replaced the local draft");
    record("workflow-catalog-conflict");
  }

  async function workflowCatalogUnknownRecoveryCase(fixture) {
    document.querySelector("#studio-new").click();
    fixture.reset("workflow-catalog-outcome-unknown");
    document.querySelector("#studio-save").click();
    await waitFor(
      () => fixture.lastWorkflowCatalogId &&
        fixture.pendingCount(`workflow-catalog-detail-${fixture.lastWorkflowCatalogId}`) === 1,
      "exact outcome-unknown response did not start read-only reconciliation",
    );
    fixture.resolveNext(
      `workflow-catalog-detail-${fixture.lastWorkflowCatalogId}`,
      fixture.workflowCatalogResponse(fixture.workflowCatalogRecord),
    );
    await waitFor(
      () => fixture.workflowCatalogPutCount === 1 && fixture.workflowCatalogGetCount >= 1 &&
        document.querySelector("#studio-save-ledger").dataset.state === "saved" &&
        document.querySelector("#studio-record-label").textContent.includes("SAVED · r1"),
      "exact outcome-unknown response was not recovered through read-only GET",
    );
    assert(fixture.workflowCatalogPutCount === 1,
      "outcome-unknown recovery repeated the Workflow mutation");
    assert(fixture.requests.some((request) => request.method === "GET" &&
      request.path === `/v1/workflows/${fixture.lastWorkflowCatalogId}`),
    "outcome-unknown recovery did not use the known catalog identity");
    document.querySelector("#studio-close").click();
    record("workflow-catalog-unknown-recovery");
  }

  async function workflowCatalogMalformedSuccessCase(fixture) {
    fixture.reset("workflow-catalog-malformed-success");
    document.querySelector("#studio-button").click();
    document.querySelector("#studio-new").click();
    document.querySelector("#studio-save").click();
    await waitFor(
      () => fixture.workflowCatalogPutCount === 1 && fixture.workflowCatalogGetCount >= 1 &&
        document.querySelector("#studio-save-ledger").dataset.state === "saved",
      "malformed save success was not recovered through read-only GET",
    );
    assert(fixture.workflowCatalogPutCount === 1,
      "malformed save success recovery repeated the Workflow mutation");
    document.querySelector("#studio-close").click();
    record("workflow-catalog-malformed-success");
  }

  async function workflowCatalogPaginationCase(fixture) {
    fixture.reset("workflow-catalog-pagination");
    document.querySelector("#studio-button").click();
    await waitFor(
      () => document.querySelectorAll(".studio-catalog-card[data-catalog-id]").length === 100 &&
        document.querySelector("[data-catalog-more]"),
      "valid first catalog page and cursor were not rendered",
    );
    document.querySelector("[data-catalog-more]").click();
    await waitFor(
      () => document.querySelectorAll(".studio-catalog-card[data-catalog-id]").length === 101 &&
        !document.querySelector("[data-catalog-more]"),
      "second catalog page was not merged",
    );
    const identities = [...document.querySelectorAll(".studio-catalog-card[data-catalog-id]")]
      .map((card) => card.dataset.catalogId);
    assert(new Set(identities).size === 101,
      "catalog pages introduced duplicate record identities");
    assert(fixture.requests.some((request) =>
      request.path === "/v1/workflows?limit=100&before=101"),
    "catalog pagination did not use the stable server cursor");
    document.querySelector("#studio-close").click();
    record("workflow-catalog-pagination");
  }

  async function workflowCatalogDigestMismatchCase(fixture) {
    fixture.reset("workflow-catalog-digest-mismatch");
    document.querySelector("#studio-button").click();
    await waitFor(() => document.querySelector(".studio-catalog-card[data-catalog-id]"),
      "saved Workflow card was unavailable for digest mismatch case");
    const editor = document.querySelector("#studio-editor");
    const before = editor.value;
    document.querySelector(".studio-catalog-card[data-catalog-id]").click();
    await waitFor(
      () => document.querySelector("#studio-save-ledger").dataset.state === "unknown",
      "definition bytes not bound to the stored SHA-256 were accepted",
    );
    assert(editor.value === before && !editor.value.includes("tampered-without-new-digest"),
      "digest-mismatched definition replaced the editor");
    document.querySelector("#studio-close").click();
    record("workflow-catalog-digest-mismatch");
  }

  async function workflowCatalogRecordSwitchCase(fixture) {
    fixture.reset("workflow-catalog-record-switch");
    document.querySelector("#studio-button").click();
    await waitFor(
      () => document.querySelectorAll(".studio-catalog-card[data-catalog-id]").length === 2,
      "record-switch fixture did not expose two durable records",
    );
    const recordA = fixture.workflowCatalogRecord;
    const recordB = fixture.workflowCatalogExtraRecord;
    const card = (catalogId) => document.querySelector(
      `.studio-catalog-card[data-catalog-id="${catalogId}"]`,
    );
    const editor = document.querySelector("#studio-editor");

    card(recordA.record.catalog_id).click();
    await waitFor(() =>
      editor.value.includes(`"version": "${recordA.record.definition.version}"`) &&
      document.querySelector("#studio-save-ledger").dataset.state === "saved" &&
      card(recordA.record.catalog_id).getAttribute("aria-current") === "true",
      "record A did not open");
    editor.value = editor.value.replace(
      `"version": "${recordA.record.definition.version}"`,
      '"version": "switch-a-save"',
    );
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    fixture.delayNextWorkflowCatalogPut();
    document.querySelector("#studio-save").click();
    await waitFor(() => fixture.pendingCount("workflow-catalog-put") === 1,
      "record A save response was not delayed");
    card(recordB.record.catalog_id).click();
    await waitFor(() => editor.value.includes('"version": "switch-b"'),
      "record B did not become the active editor");
    fixture.resolveNext(
      "workflow-catalog-put",
      fixture.workflowCatalogResponse(fixture.workflowCatalogRecord),
    );
    await tick();
    assert(editor.value.includes('"version": "switch-b"') &&
      card(recordB.record.catalog_id).getAttribute("aria-current") === "true",
    "late record A PUT success replaced record B");

    card(recordA.record.catalog_id).click();
    await waitFor(() => editor.value.includes('"version": "switch-a-save"'),
      "updated record A did not reopen");
    editor.value = editor.value.replace(
      '"version": "switch-a-save"', '"version": "switch-a-unknown"',
    );
    editor.dispatchEvent(new Event("input", { bubbles: true }));
    fixture.outcomeUnknownNextWorkflowCatalogPut();
    document.querySelector("#studio-save").click();
    await waitFor(
      () => fixture.pendingCount(
        `workflow-catalog-detail-${recordA.record.catalog_id}`,
      ) === 1,
      "record A outcome-unknown reconciliation GET was not delayed",
    );
    card(recordB.record.catalog_id).click();
    await waitFor(() => editor.value.includes('"version": "switch-b"'),
      "record B did not remain selectable during record A reconciliation");
    fixture.resolveNext(
      `workflow-catalog-detail-${recordA.record.catalog_id}`,
      fixture.workflowCatalogResponse(fixture.workflowCatalogRecord),
    );
    await tick();
    assert(editor.value.includes('"version": "switch-b"') &&
      card(recordB.record.catalog_id).getAttribute("aria-current") === "true",
    "late record A reconciliation GET replaced record B");

    fixture.delayWorkflowCatalogDetail(recordB.record.catalog_id);
    card(recordB.record.catalog_id).click();
    await waitFor(
      () => fixture.pendingCount(
        `workflow-catalog-detail-${recordB.record.catalog_id}`,
      ) === 1,
      "record B detail response was not delayed",
    );
    card(recordA.record.catalog_id).click();
    await waitFor(() => editor.value.includes('"version": "switch-a-unknown"'),
      "record A did not become active during delayed record B read");
    fixture.resolveNext(
      `workflow-catalog-detail-${recordB.record.catalog_id}`,
      fixture.workflowCatalogResponse(recordB),
    );
    await tick();
    assert(editor.value.includes('"version": "switch-a-unknown"') &&
      card(recordA.record.catalog_id).getAttribute("aria-current") === "true",
    "late record B detail response replaced record A");
    assert(fixture.workflowCatalogPutCount === 2,
      "record-switch recovery repeated a Workflow mutation");
    document.querySelector("#studio-close").click();
    record("workflow-catalog-record-switch");
  }

  function structuredResultCase(fixture) {
    state.run = fixture.projection("run-structured", "mobile-safe structured result", "completed", {
      app_id: "incident",
      final_message: {
        role: "assistant",
        content: JSON.stringify({
          definition_sha256: "0".repeat(64),
          output: {
            step_id: "record",
            value: "diagnostic captured for a narrow viewport",
            value_sha256: "1".repeat(64),
          },
          status: "succeeded",
          version: "1",
          workflow_id: "incident-mechanism",
          workflow_version: "1",
        }),
      },
    });
    renderMessages();
    const resultCard = document.querySelector("#message-stack .structured-result");
    assert(resultCard && resultCard.textContent.includes("VERIFIED OUTPUT"),
      "structured Workflow result was not rendered");
    if (global.innerWidth <= 940) {
      const resultBounds = resultCard.getBoundingClientRect();
      const composerBounds = document.querySelector("#task-form").getBoundingClientRect();
      assert(resultBounds.left >= 0 && resultBounds.right <= global.innerWidth,
        "structured Workflow result overflows the narrow viewport");
      assert(resultBounds.bottom <= composerBounds.top + 1,
        "narrow Run composer obscures the structured Workflow result");
      assert(document.documentElement.scrollWidth <= global.innerWidth,
        "structured Workflow result creates horizontal page overflow");
    }
    record("structured-result");
  }

  async function run() {
    const fixture = global.__sasoriFixture;
    await waitFor(
      () => global.SasoriEventReducer && historyButton("run-old") && historyButton("run-new") &&
        !document.querySelector("#run-button").disabled,
      "production Workbench did not finish initial loading",
    );
    workbenchShellCase();
    unavailableWorkflowCase();
    await workflowStudioCase(fixture);
    await workflowStudioStaleEditCase(fixture);
    await workflowStudioContractCase(fixture);
    await workflowStudioMalformedRejectionCase(fixture);
    await workflowStudioRejectedCase(fixture);
    await workflowStudioTransportCase(fixture);
    await workflowStudioInvalidUnicodeCase(fixture);
    await workflowCatalogSaveCase(fixture);
    await workflowCatalogStaleEditCase(fixture);
    await workflowCatalogConflictCase(fixture);
    await workflowCatalogUnknownRecoveryCase(fixture);
    await workflowCatalogMalformedSuccessCase(fixture);
    await workflowCatalogPaginationCase(fixture);
    await workflowCatalogDigestMismatchCase(fixture);
    await workflowCatalogRecordSwitchCase(fixture);
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
    structuredResultCase(fixture);
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
