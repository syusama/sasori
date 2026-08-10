"use strict";

/*
 * Immutable static Workflow manifest validation and disclosure extension.
 * The older workflow.0.2.0.js remains byte-stable and continues to own only
 * rendering/projection integration. This file owns no run state, scheduler,
 * checkpoint, event reducer, Tool dispatch, or authoring persistence.
 */

const workflowManifestKeys = [
  "app_id",
  "definition_sha256",
  "execution",
  "inputs",
  "output_step",
  "schema_version",
  "step_count",
  "steps",
  "supports_agent_nodes",
  "supports_branches",
  "supports_parallel",
  "trust",
  "version",
  "workflow_id",
].sort();
const workflowManifestInputKeys = ["key", "max_bytes", "required", "type"].sort();
const workflowManifestStepKeys = [
  "argument_sources",
  "depends_on",
  "dispatch_schema_sha256",
  "dispatch_tool_name",
  "dispatch_tool_revision",
  "effect",
  "is_output",
  "logical_schema_sha256",
  "logical_tool_name",
  "logical_tool_revision",
  "max_result_bytes",
  "position",
  "recovery_policy",
  "requires_approval",
  "result_type",
  "step_id",
].sort();
const workflowManifestReferenceKeys = ["kind", "name", "ref"].sort();
const workflowManifestLiteralKeys = [
  "canonical_bytes",
  "kind",
  "name",
  "value_sha256",
  "value_type",
].sort();
const workflowManifestJsonTypes = new Set([
  "array",
  "boolean",
  "integer",
  "null",
  "number",
  "object",
  "string",
]);
const workflowManifestPolicies = {
  read_only: [false, "read_only_replay_allowed"],
  idempotent: [true, "same_verified_business_key_only"],
  side_effecting: [true, "manual_effect_resolution_on_ambiguity"],
};

function workflowManifestNonempty(value) {
  return typeof value === "string" && value.length > 0;
}

function workflowManifestContract(app) {
  if (!app || app.workflow === undefined) return null;
  if (!app.availability || app.availability.status !== "ready") return null;
  const value = workflowExactObject(app.workflow, workflowManifestKeys, "Workflow manifest");
  workflowExactObject(value.trust, ["execution_mode", "sandboxed"], "Workflow trust");
  if (value.schema_version !== 1 || !workflowManifestNonempty(value.workflow_id) ||
      !workflowManifestNonempty(value.version) || !workflowSha256.test(value.definition_sha256) ||
      value.app_id !== app.id || value.execution !== "single-harness-ordered-tools-v1" ||
      !workflowManifestNonempty(value.output_step) ||
      !Number.isSafeInteger(value.step_count) || value.step_count < 1 ||
      value.supports_parallel !== false || value.supports_branches !== false ||
      value.supports_agent_nodes !== false ||
      value.trust.execution_mode !== "trusted_installed_python" ||
      value.trust.sandboxed !== false || !Array.isArray(value.inputs) ||
      !Array.isArray(value.steps) || value.steps.length !== value.step_count) {
    throw new Error("Workflow manifest contract is invalid");
  }

  const inputIds = new Set();
  value.inputs.forEach((input) => {
    workflowExactObject(input, workflowManifestInputKeys, "Workflow manifest input");
    if (!workflowManifestNonempty(input.key) || inputIds.has(input.key) ||
        !workflowManifestJsonTypes.has(input.type) || typeof input.required !== "boolean" ||
        !Number.isSafeInteger(input.max_bytes) || input.max_bytes < 1) {
      throw new Error("Workflow manifest input contract is invalid");
    }
    inputIds.add(input.key);
  });

  const stepIds = new Set();
  const stepPositions = Object.create(null);
  const dispatchNames = new Set();
  value.steps.forEach((step, index) => {
    workflowExactObject(step, workflowManifestStepKeys, "Workflow manifest step");
    if (step.position !== index + 1 || !workflowManifestNonempty(step.step_id) ||
        stepIds.has(step.step_id) || !workflowManifestNonempty(step.logical_tool_name) ||
        !workflowManifestNonempty(step.dispatch_tool_name) ||
        dispatchNames.has(step.dispatch_tool_name) ||
        !Object.hasOwn(workflowManifestPolicies, step.effect) ||
        !workflowSha256.test(step.logical_schema_sha256) ||
        !workflowSha256.test(step.dispatch_schema_sha256) ||
        !workflowManifestJsonTypes.has(step.result_type) ||
        !Number.isSafeInteger(step.max_result_bytes) || step.max_result_bytes < 1 ||
        typeof step.is_output !== "boolean" || !Array.isArray(step.depends_on) ||
        !Array.isArray(step.argument_sources)) {
      throw new Error("Workflow manifest step contract is invalid");
    }
    const mutable = step.effect !== "read_only";
    const logicalRevision = step.logical_tool_revision === null ||
      workflowManifestNonempty(step.logical_tool_revision);
    if (!logicalRevision ||
        (mutable && (!workflowManifestNonempty(step.logical_tool_revision) ||
        !workflowManifestNonempty(step.dispatch_tool_revision))) ||
        (!mutable && step.dispatch_tool_revision !== null)) {
      throw new Error("Workflow manifest revision policy is invalid");
    }
    const policy = workflowManifestPolicies[step.effect];
    if (step.requires_approval !== policy[0] || step.recovery_policy !== policy[1] ||
        step.is_output !== (step.step_id === value.output_step)) {
      throw new Error("Workflow manifest effect policy is invalid");
    }
    stepIds.add(step.step_id);
    stepPositions[step.step_id] = index;
    dispatchNames.add(step.dispatch_tool_name);
  });

  value.steps.forEach((step, index) => {
    const argumentNames = new Set();
    const dependencyIds = new Set();
    step.argument_sources.forEach((source) => {
      const keys = source && source.kind === "literal"
        ? workflowManifestLiteralKeys
        : workflowManifestReferenceKeys;
      workflowExactObject(source, keys, "Workflow manifest argument source");
      if (!workflowManifestNonempty(source.name) || argumentNames.has(source.name)) {
        throw new Error("Workflow manifest argument name is invalid");
      }
      argumentNames.add(source.name);
      if (source.kind === "input") {
        if (!workflowManifestNonempty(source.ref) || !inputIds.has(source.ref)) {
          throw new Error("Workflow manifest input reference is invalid");
        }
      } else if (source.kind === "step") {
        if (!workflowManifestNonempty(source.ref) ||
            !Number.isSafeInteger(stepPositions[source.ref]) ||
            stepPositions[source.ref] >= index) {
          throw new Error("Workflow manifest step reference is invalid");
        }
        dependencyIds.add(source.ref);
      } else if (source.kind === "literal") {
        if (!workflowManifestJsonTypes.has(source.value_type) ||
            !Number.isSafeInteger(source.canonical_bytes) || source.canonical_bytes < 1 ||
            !workflowSha256.test(source.value_sha256)) {
          throw new Error("Workflow manifest literal descriptor is invalid");
        }
      } else {
        throw new Error("Workflow manifest argument kind is invalid");
      }
    });
    const dependencies = [...dependencyIds].sort(
      (left, right) => stepPositions[left] - stepPositions[right],
    );
    if (JSON.stringify(step.depends_on) !== JSON.stringify(dependencies)) {
      throw new Error("Workflow manifest dependencies are invalid");
    }
  });

  if (!stepIds.has(value.output_step) ||
      value.steps.filter((step) => step.is_output).length !== 1) {
    throw new Error("Workflow manifest output step is invalid");
  }
  const exposedTools = Array.isArray(app.tools) ? app.tools.map((tool) => tool.name) : [];
  if (JSON.stringify([...dispatchNames]) !== JSON.stringify(exposedTools)) {
    throw new Error("Workflow manifest and dispatch Tool order disagree");
  }
  return value;
}

workflowContract = workflowManifestContract;

const workflowManifestStepCardBase = workflowStepCard;
workflowStepCard = function workflowStepCardWithManifest(app, step, projection, current) {
  const card = workflowManifestStepCardBase(app, step, projection, current);
  const facts = card.querySelector(".workflow-step-facts");
  workflowFact(facts, "depends on", step.depends_on.length ? step.depends_on.join(" -> ") : "workflow input");
  workflowFact(facts, "approval", step.requires_approval ? "required" : "not required");
  workflowFact(facts, "recovery", step.recovery_policy);
  return card;
};

const workflowManifestSurfaceBase = renderWorkflowSurface;
renderWorkflowSurface = function renderWorkflowSurfaceWithManifest(app) {
  workflowManifestSurfaceBase(app);
  const contract = workflowContract(app);
  if (!contract) return;
  const boundary = document.querySelector(".workflow-surface .workflow-boundary");
  if (boundary) {
    boundary.append(
      element("span", "", "TRUSTED PYTHON"),
      element("span", "", "NO SANDBOX"),
    );
  }
};
