"use strict";

/*
 * Typed Workflow Workbench projection.
 * Definition data comes from /v1/apps; progress comes only from the existing
 * run projection and the one public event reducer. This file owns no runtime
 * state, checkpoint, scheduler, or second event reducer.
 */

const workflowSha256 = /^[0-9a-f]{64}$/;
const workflowEffects = new Set(["read_only", "idempotent", "side_effecting"]);

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

function workflowEvents() {
  return state.eventState && Array.isArray(state.eventState.events)
    ? state.eventState.events
    : [];
}

function workflowStepProjection(app, step) {
  const bound = Boolean(state.run && state.run.app_id === app.id);
  const events = bound
    ? workflowEvents().filter((projected) =>
        projected.event && projected.event.tool_name === step.dispatch_tool_name)
    : [];
  const types = new Set(events.map((projected) => projected.event.type));
  const pending = bound && state.run.pending &&
    state.run.pending.tool_name === step.dispatch_tool_name
    ? state.run.pending
    : null;
  let status = "queued";
  if (types.has("tool.failed")) status = "failed";
  else if (types.has("tool.completed")) status = "completed";
  else if (pending && state.run.pause_reason === "approval_required") status = "approval";
  else if (pending && state.run.pause_reason === "resume_required") status = "ready";
  else if (pending && state.run.pause_reason === "effect_unknown") status = "recovery";
  else if (pending && state.run.pause_reason === "retryable_idempotent") status = "retry";
  else if (types.has("tool.started")) status = "running";
  else if (types.has("tool.requested")) status = "requested";
  else if (bound && terminalStates.has(state.run.state)) status = "stopped";
  const latest = events.length ? events[events.length - 1].event : null;
  return {
    status,
    callId: pending ? pending.call_id : latest && latest.call_id,
    pending,
  };
}

const workflowStatusLabels = {
  queued: "待装配",
  requested: "已请求",
  running: "执行中",
  approval: "等待批准",
  ready: "等待显式继续",
  recovery: "等待人工恢复",
  retry: "可按幂等键重试",
  completed: "已提交",
  failed: "失败",
  stopped: "已停止",
};

function workflowFact(list, name, value, className = "") {
  const term = element("dt", "", name);
  const detail = element("dd", className, value === null || value === undefined ? "—" : String(value));
  list.append(term, detail);
}

function workflowStepCard(app, step, projection, current) {
  const card = element("article", "workflow-step");
  card.dataset.stepState = projection.status;
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

  card.append(header, route, facts);
  if (projection.pending) {
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
  const projections = contract.steps.map((step) => workflowStepProjection(app, step));
  const completed = projections.filter((item) => item.status === "completed").length;
  const currentIndex = projections.findIndex((item) => !["completed", "stopped"].includes(item.status));
  const current = currentIndex >= 0 ? contract.steps[currentIndex] : null;
  const section = element("section", "surface-block workflow-surface");
  section.dataset.workflowId = contract.workflow_id;

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
    const card = workflowStepCard(app, step, projections[index], index === currentIndex && bound);
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
  setRunProjectionWithoutWorkflow(projection, context);
  renderSurface(selectedApplication());
};

const renderTimelineWithoutWorkflow = renderTimeline;
renderTimeline = function renderTimelineWithWorkflow() {
  renderTimelineWithoutWorkflow();
  renderSurface(selectedApplication());
};
