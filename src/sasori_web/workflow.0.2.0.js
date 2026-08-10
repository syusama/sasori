"use strict";

/*
 * Typed Workflow Workbench projection.
 * Definition data comes from /v1/apps; durable step semantics come only from
 * state.run.workflow. The existing event reducer remains the timeline/cursor
 * authority. This file owns no runtime, checkpoint, scheduler, step store, or
 * second reducer.
 */

const workflowSha256 = /^[0-9a-f]{64}$/;
const workflowMaxCallIdBytes = 256;
const workflowEffects = new Set(["read_only", "idempotent", "side_effecting"]);
const workflowStatuses = new Set([
  "pending",
  "requested",
  "running",
  "approval_required",
  "resume_required",
  "retryable_idempotent",
  "effect_unknown",
  "completed",
  "failed",
  "stopped",
]);
const workflowProjectionKeys = [
  "app_id",
  "current_step_id",
  "definition_sha256",
  "execution",
  "latest_seq",
  "output_step",
  "schema_version",
  "steps",
  "version",
  "workflow_id",
].sort();
const workflowProjectionStepKeys = [
  "call_id",
  "dispatch_schema_sha256",
  "dispatch_tool_name",
  "dispatch_tool_revision",
  "effect",
  "error_code",
  "kind",
  "logical_schema_sha256",
  "logical_tool_name",
  "logical_tool_revision",
  "max_result_bytes",
  "position",
  "result_type",
  "status",
  "step_id",
].sort();

function workflowExactObject(value, keys, name) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(keys)) {
    throw new Error(`${name} contract is invalid`);
  }
  return value;
}

function workflowUtf8Bytes(value) {
  const encoded = new TextEncoder().encode(value);
  if (new TextDecoder("utf-8", { fatal: true }).decode(encoded) !== value) {
    throw new Error("Workflow call ID is not valid Unicode text");
  }
  return encoded.length;
}

function workflowContract(app) {
  if (!app || app.workflow === undefined) return null;
  if (!app.availability || app.availability.status !== "ready") return null;
  const value = app.workflow;
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      value.schema_version !== 1 || typeof value.workflow_id !== "string" ||
      typeof value.version !== "string" || !workflowSha256.test(value.definition_sha256) ||
      value.execution !== "single-harness-ordered-tools-v1" ||
      !Number.isSafeInteger(value.step_count) || value.step_count < 1 ||
      value.supports_parallel !== false || value.supports_branches !== false ||
      value.supports_agent_nodes !== false || !Array.isArray(value.steps) ||
      value.steps.length !== value.step_count) {
    throw new Error("Workflow catalog contract is invalid");
  }
  const ids = new Set();
  const dispatchNames = new Set();
  value.steps.forEach((step, index) => {
    if (!step || typeof step !== "object" || Array.isArray(step) ||
        step.position !== index + 1 || typeof step.step_id !== "string" ||
        !step.step_id || ids.has(step.step_id) ||
        typeof step.logical_tool_name !== "string" || !step.logical_tool_name ||
        typeof step.dispatch_tool_name !== "string" || !step.dispatch_tool_name ||
        dispatchNames.has(step.dispatch_tool_name) || !workflowEffects.has(step.effect) ||
        !workflowSha256.test(step.logical_schema_sha256) ||
        !workflowSha256.test(step.dispatch_schema_sha256) ||
        typeof step.result_type !== "string" ||
        !Number.isSafeInteger(step.max_result_bytes) || step.max_result_bytes < 1 ||
        typeof step.is_output !== "boolean") {
      throw new Error("Workflow step catalog contract is invalid");
    }
    ids.add(step.step_id);
    dispatchNames.add(step.dispatch_tool_name);
  });
  if (value.steps.filter((step) => step.is_output).length !== 1) {
    throw new Error("Workflow output step catalog contract is invalid");
  }
  const exposedTools = Array.isArray(app.tools) ? app.tools.map((tool) => tool.name) : [];
  if (JSON.stringify([...dispatchNames]) !== JSON.stringify(exposedTools)) {
    throw new Error("Workflow step and dispatch Tool order disagree");
  }
  return value;
}

function workflowPreview(contract) {
  return {
    currentStepId: null,
    latestSeq: 0,
    steps: contract.steps.map((step) => ({
      status: "pending",
      callId: null,
      errorCode: null,
      pending: null,
    })),
  };
}

function workflowRunProjection(app, contract, run = state.run) {
  const bound = Boolean(run && run.app_id === app.id);
  if (!bound) return workflowPreview(contract);
  if (!run.workflow) {
    if (run.detail === "submitting" && Number(run.latest_seq || 0) === 0) {
      return workflowPreview(contract);
    }
    throw new Error("Workflow run projection is missing");
  }

  const value = workflowExactObject(
    run.workflow,
    workflowProjectionKeys,
    "Workflow run projection",
  );
  if (value.schema_version !== 1 || value.workflow_id !== contract.workflow_id ||
      value.version !== contract.version || value.definition_sha256 !== contract.definition_sha256 ||
      value.app_id !== app.id || value.execution !== contract.execution ||
      value.output_step !== contract.steps.find((step) => step.is_output).step_id ||
      !Number.isSafeInteger(value.latest_seq) || value.latest_seq < 0 ||
      value.latest_seq !== Number(run.latest_seq || 0) ||
      !Array.isArray(value.steps) || value.steps.length !== contract.step_count ||
      !(value.current_step_id === null || typeof value.current_step_id === "string")) {
    throw new Error("Workflow run binding is invalid");
  }

  const callIds = new Set();
  const steps = value.steps.map((projected, index) => {
    workflowExactObject(projected, workflowProjectionStepKeys, "Workflow projected step");
    const definition = contract.steps[index];
    if (projected.position !== definition.position || projected.step_id !== definition.step_id ||
        projected.kind !== "tool" || projected.logical_tool_name !== definition.logical_tool_name ||
        projected.dispatch_tool_name !== definition.dispatch_tool_name ||
        projected.effect !== definition.effect ||
        projected.logical_tool_revision !== definition.logical_tool_revision ||
        projected.dispatch_tool_revision !== definition.dispatch_tool_revision ||
        projected.logical_schema_sha256 !== definition.logical_schema_sha256 ||
        projected.dispatch_schema_sha256 !== definition.dispatch_schema_sha256 ||
        projected.result_type !== definition.result_type ||
        projected.max_result_bytes !== definition.max_result_bytes ||
        !workflowStatuses.has(projected.status) ||
        !(projected.error_code === null || typeof projected.error_code === "string")) {
      throw new Error("Workflow projected step binding is invalid");
    }
    const callId = projected.call_id;
    if (callId !== null && (typeof callId !== "string" || !callId ||
        workflowUtf8Bytes(callId) > workflowMaxCallIdBytes || callIds.has(callId))) {
      throw new Error("Workflow projected step call ID is invalid");
    }
    if ((projected.status === "pending" && callId !== null) ||
        (!["pending", "stopped"].includes(projected.status) && callId === null)) {
      throw new Error("Workflow projected step call binding is invalid");
    }
    if (callId !== null) callIds.add(callId);
    const pending = run.pending &&
      run.pending.tool_name === projected.dispatch_tool_name
      ? run.pending
      : null;
    return {
      status: projected.status,
      callId: projected.call_id,
      errorCode: projected.error_code,
      pending,
    };
  });
  const terminal = terminalStates.has(run.state);
  const expectedCurrent = terminal ? null : value.steps.find((step) =>
    !["completed", "stopped"].includes(step.status))?.step_id || null;
  if (value.current_step_id !== expectedCurrent) {
    throw new Error("Workflow current step binding is invalid");
  }
  return { currentStepId: value.current_step_id, latestSeq: value.latest_seq, steps };
}

const workflowStatusLabels = {
  pending: "待装配",
  requested: "已请求",
  running: "执行中",
  approval_required: "等待批准",
  resume_required: "等待显式继续",
  retryable_idempotent: "可按幂等键重试",
  effect_unknown: "等待人工恢复",
  completed: "已提交",
  failed: "失败",
  stopped: "已停止",
};

const workflowVisualStates = {
  pending: "queued",
  approval_required: "approval",
  resume_required: "ready",
  retryable_idempotent: "retry",
  effect_unknown: "recovery",
};

function workflowFact(list, name, value, className = "") {
  const term = element("dt", "", name);
  const detail = element("dd", className, value === null || value === undefined ? "—" : String(value));
  list.append(term, detail);
}

function workflowStepCard(app, step, projection, current) {
  const card = element("article", "workflow-step");
  card.dataset.stepState = workflowVisualStates[projection.status] || projection.status;
  card.dataset.stepStatus = projection.status;
  card.dataset.stepId = step.step_id;
  if (current) card.dataset.current = "true";

  const header = element("header");
  const ordinal = element("span", "workflow-step-ordinal", String(step.position).padStart(2, "0"));
  const title = element("span", "workflow-step-title");
  title.append(
    element("small", "", step.is_output ? "OUTPUT MECHANISM" : "TOOL MECHANISM"),
    element("strong", "", step.step_id),
  );
  const status = element("span", "workflow-step-status", workflowStatusLabels[projection.status]);
  header.append(ordinal, title, status);

  const route = element("div", "workflow-tool-route");
  const logical = element("span", "workflow-tool logical");
  logical.append(element("small", "", "LOGICAL TOOL"), element("b", "", step.logical_tool_name));
  const arrow = element("i", "", "→");
  arrow.setAttribute("aria-hidden", "true");
  const dispatch = element("span", "workflow-tool dispatch");
  dispatch.append(element("small", "", "HARNESS WRAPPER"), element("b", "", step.dispatch_tool_name));
  route.append(logical, arrow, dispatch);

  const facts = element("dl", "workflow-step-facts");
  workflowFact(facts, "effect", step.effect);
  workflowFact(facts, "result", `${step.result_type} · ≤ ${step.max_result_bytes} B`);
  workflowFact(facts, "logical rev", step.logical_tool_revision || "read-only");
  workflowFact(facts, "wrapper rev", step.dispatch_tool_revision || "read-only", "workflow-digest");
  workflowFact(facts, "call", projection.callId || "not requested", "workflow-digest");
  workflowFact(facts, "schema", step.dispatch_schema_sha256, "workflow-digest");
  if (projection.errorCode) workflowFact(facts, "error", projection.errorCode);

  card.append(header, route, facts);
  if (projection.pending && [
    "approval_required",
    "resume_required",
    "retryable_idempotent",
    "effect_unknown",
  ].includes(projection.status)) {
    const gate = element("div", "workflow-gate");
    gate.append(
      element("span", "", "HUMAN GATE"),
      element("b", "", workflowStatusLabels[projection.status]),
      element("small", "", `effect / ${projection.pending.effect}`),
    );
    card.append(gate);
  }
  return card;
}

function renderWorkflowSurface(app) {
  const host = $("#surface-content");
  if (!host) return;
  const contract = workflowContract(app);
  if (!contract) return;
  const bound = Boolean(state.run && state.run.app_id === app.id);
  const projected = workflowRunProjection(app, contract);
  const completed = projected.steps.filter((item) => item.status === "completed").length;
  const currentIndex = projected.currentStepId === null
    ? -1
    : contract.steps.findIndex((step) => step.step_id === projected.currentStepId);
  const current = currentIndex >= 0 ? contract.steps[currentIndex] : null;
  const section = element("section", "surface-block workflow-surface");
  section.dataset.workflowId = contract.workflow_id;
  section.dataset.projection = bound && state.run.workflow ? "public-v1" : "definition-preview";

  const heading = element("header", "workflow-heading");
  const headingTitle = element("span");
  headingTitle.append(
    element("small", "", "ORDERED / ONE HARNESS"),
    element("h3", "", "串行机关图谱"),
  );
  heading.append(headingTitle, element("span", "workflow-version", `V${contract.version}`));

  const identity = element("div", "workflow-identity");
  const seal = element("span", "workflow-seal");
  seal.append(element("small", "", "DEFINITION BOUND"), element("b", "", contract.workflow_id));
  const digest = element("code", "workflow-definition-digest", contract.definition_sha256);
  digest.title = contract.definition_sha256;
  identity.append(seal, digest);

  const progress = element("div", "workflow-progress");
  const progressCopy = bound
    ? current
      ? `CURRENT DURABLE STEP · ${String(current.position).padStart(2, "0")} / ${current.step_id}`
      : `${completed} / ${contract.step_count} STEPS COMMITTED`
    : `${contract.step_count} DEFINITION-BOUND STEPS · PREVIEW`;
  progress.append(
    element("span", "", progressCopy),
    element("b", "", bound ? `${completed}/${contract.step_count}` : `0/${contract.step_count}`),
  );
  const meter = element("i");
  meter.style.setProperty("--workflow-progress", `${bound ? (completed / contract.step_count) * 100 : 0}%`);
  progress.append(meter);

  const rail = element("div", "workflow-rail");
  rail.setAttribute("role", "list");
  rail.setAttribute("aria-label", `${contract.workflow_id} ordered steps`);
  contract.steps.forEach((step, index) => {
    const card = workflowStepCard(app, step, projected.steps[index], index === currentIndex && bound);
    card.setAttribute("role", "listitem");
    rail.append(card);
  });

  const boundary = element("footer", "workflow-boundary");
  boundary.append(
    element("span", "", "SERIAL ONLY"),
    element("span", "", "NO BRANCHES"),
    element("span", "", "NO AGENT NODES"),
  );
  section.append(heading, identity, progress, rail, boundary);
  host.prepend(section);
}

function decorateWorkflowWorkers() {
  state.apps.forEach((app) => {
    const contract = workflowContract(app);
    if (!contract) return;
    const card = $(`.worker-card[data-app-id="${CSS.escape(app.id)}"]`);
    if (!card || $(".workflow-worker-badge", card)) return;
    const badge = element("span", "workflow-worker-badge", `WORKFLOW V${contract.version} · SERIAL`);
    card.append(badge);
  });
}

const renderWorkersWithoutWorkflow = renderWorkers;
renderWorkers = function renderWorkersWithWorkflow() {
  renderWorkersWithoutWorkflow();
  decorateWorkflowWorkers();
};

const renderSurfaceWithoutWorkflow = renderSurface;
renderSurface = function renderSurfaceWithWorkflow(app) {
  renderSurfaceWithoutWorkflow(app);
  renderWorkflowSurface(app);
};

const setRunProjectionWithoutWorkflow = setRunProjection;
setRunProjection = function setRunProjectionWithWorkflow(projection, context) {
  const stale = contextIsActive(context) && state.run && projection &&
    state.run.run_id === projection.run_id &&
    Number.isInteger(state.run.revision) && Number.isInteger(projection.revision) &&
    projection.revision < state.run.revision;
  if (contextIsActive(context) && !stale && projection && typeof projection === "object") {
    const app = state.apps.find((item) => item.id === projection.app_id);
    const contract = workflowContract(app);
    if (contract) workflowRunProjection(app, contract, projection);
  }
  const changed = setRunProjectionWithoutWorkflow(projection, context);
  if (changed !== false) renderSurface(selectedApplication());
  return changed;
};

let workflowRefreshInFlight = false;
let workflowRefreshPendingContext = null;
let workflowRefreshScheduled = false;

async function drainWorkflowRefresh() {
  if (workflowRefreshInFlight) return;
  workflowRefreshScheduled = false;
  const context = workflowRefreshPendingContext;
  workflowRefreshPendingContext = null;
  if (!contextIsActive(context)) return;
  workflowRefreshInFlight = true;
  try {
    await refreshCurrentRun(context);
  } finally {
    workflowRefreshInFlight = false;
    if (workflowRefreshPendingContext &&
        !contextIsActive(workflowRefreshPendingContext)) {
      workflowRefreshPendingContext = null;
    }
    if (workflowRefreshPendingContext && !workflowRefreshScheduled) {
      workflowRefreshScheduled = true;
      queueMicrotask(drainWorkflowRefresh);
    }
  }
}

function scheduleWorkflowRefresh(context) {
  if (!contextIsActive(context)) return;
  workflowRefreshPendingContext = context;
  if (workflowRefreshInFlight || workflowRefreshScheduled) return;
  workflowRefreshScheduled = true;
  queueMicrotask(drainWorkflowRefresh);
}

const addEventWithoutWorkflow = addEvent;
addEvent = function addEventWithWorkflowRefresh(projected) {
  const changed = addEventWithoutWorkflow(projected);
  const app = selectedApplication();
  if (!changed || !workflowContract(app)) return changed;
  const context = currentRunContext();
  if (!contextIsActive(context) || !state.run || state.run.app_id !== app.id) return changed;
  scheduleWorkflowRefresh(context);
  return changed;
};
