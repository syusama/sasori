"use strict";

/*
 * Immutable Static Serial Workflow Studio preview.
 * Owns transient editor text and request epochs only. It does not save,
 * activate, execute, checkpoint, reduce events, or authorize Tool contracts.
 */

(function installWorkflowStudio() {
  const studio = document.querySelector("#workflow-studio");
  const editor = document.querySelector("#studio-editor");
  const preview = document.querySelector("#studio-preview");
  const button = document.querySelector("#studio-button");
  const preflightButton = document.querySelector("#studio-preflight");
  const maxDefinitionBytes = 1024 * 1024;
  const jsonTypes = new Set([
    "array", "boolean", "integer", "null", "number", "object", "string",
  ]);
  let editEpoch = 0;
  let activeRequest = null;
  let initialDraft = "";

  function studioSetStatus(kind, label) {
    const status = document.querySelector("#studio-status");
    status.dataset.state = kind;
    status.querySelector("b").textContent = label;
    const revision = document.querySelector("#studio-revision");
    revision.dataset.state = kind;
    revision.textContent = kind === "accepted"
      ? "CONTRACT ACCEPTED"
      : kind === "rejected" ? "REJECTED" : "UNVERIFIED";
  }

  function studioEmptyPreview() {
    const empty = element("div", "studio-empty-state");
    const mechanism = element("i");
    mechanism.setAttribute("aria-hidden", "true");
    mechanism.append(element("b"), element("b"), element("b"));
    empty.append(
      mechanism,
      element("small", "", "NO SERVER VERDICT"),
      element("h3", "", "图纸尚未显影"),
      element(
        "p",
        "",
        "提交后，服务端会返回 detached manifest：定义指纹、依赖、Tool 合同、审批点与恢复策略会在这里逐层展开。",
      ),
    );
    preview.replaceChildren(empty);
  }

  function studioInvalidate({ dirty = true } = {}) {
    editEpoch += 1;
    if (activeRequest) activeRequest.controller.abort();
    activeRequest = null;
    preflightButton.disabled = false;
    studioEmptyPreview();
    studioSetStatus(dirty ? "dirty" : "idle", dirty ? "草稿未校验" : "等待草稿");
  }

  function studioEncoding(value) {
    const encoded = new TextEncoder().encode(value);
    let scalarValid = false;
    try {
      scalarValid = new TextDecoder("utf-8", { fatal: true }).decode(encoded) === value;
    } catch (_error) {
      scalarValid = false;
    }
    return { bytes: encoded.byteLength, scalarValid };
  }

  function studioUpdateByteCount() {
    const encoding = studioEncoding(editor.value);
    const { bytes, scalarValid } = encoding;
    document.querySelector("#studio-byte-count").textContent =
      scalarValid
        ? `${bytes.toLocaleString("en-US")} B / 1 MiB`
        : "INVALID UNICODE / 1 MiB";
    const lines = editor.value.split("\n").length;
    document.querySelector("#studio-gutter").textContent = Array.from(
      { length: lines },
      (_, index) => String(index + 1).padStart(2, "0"),
    ).join("\n");
    preflightButton.disabled = !editor.value.trim() ||
      bytes > maxDefinitionBytes || !scalarValid;
    return encoding;
  }

  function studioToolCandidates() {
    const occurrences = new Map();
    state.apps
      .filter((app) => app.availability && app.availability.status === "ready" && !app.workflow)
      .forEach((app) => {
        (Array.isArray(app.tools) ? app.tools : []).forEach((tool) => {
          const item = { appId: app.id, appTitle: app.title, tool };
          const values = occurrences.get(tool.name) || [];
          values.push(item);
          occurrences.set(tool.name, values);
        });
      });
    return [...occurrences.values()]
      .filter((values) => values.length === 1)
      .map((values) => values[0])
      .sort((left, right) => left.tool.name.localeCompare(right.tool.name));
  }

  function studioIdentifier(name, prefix) {
    const normalized = String(name || "draft")
      .toLowerCase()
      .replace(/[^a-z0-9._-]+/g, "-")
      .replace(/^[^a-z0-9]+/, "")
      .slice(0, 44) || "draft";
    return `${prefix}-${normalized}`;
  }

  function studioDraftFor(candidate) {
    if (!candidate) {
      return JSON.stringify({
        schema_version: 1,
        workflow_id: "studio-draft",
        version: "1",
        execution: "single-harness-ordered-tools-v1",
        inputs: [],
        steps: [],
        output_step: "step-1",
      }, null, 2);
    }
    const tool = candidate.tool;
    const schema = tool.input_schema && typeof tool.input_schema === "object"
      ? tool.input_schema : {};
    const properties = schema.properties && typeof schema.properties === "object"
      ? schema.properties : {};
    const required = new Set(Array.isArray(schema.required) ? schema.required : []);
    const inputs = [];
    const argumentsMap = {};
    Object.keys(properties).sort().forEach((name) => {
      const property = properties[name] || {};
      const valueType = jsonTypes.has(property.type) ? property.type : "string";
      inputs.push({ key: name, type: valueType, required: required.has(name), max_bytes: 65536 });
      argumentsMap[name] = { kind: "input", key: name };
    });
    const stepId = studioIdentifier(tool.name, "step");
    return JSON.stringify({
      schema_version: 1,
      workflow_id: studioIdentifier(tool.name, "studio"),
      version: "1",
      execution: "single-harness-ordered-tools-v1",
      inputs,
      steps: [{
        step_id: stepId,
        kind: "tool",
        tool_name: tool.name,
        effect: tool.effect,
        tool_revision: tool.tool_revision,
        schema_sha256: tool.schema_sha256,
        arguments: argumentsMap,
        result: { type: "string", max_bytes: 65536 },
      }],
      output_step: stepId,
    }, null, 2);
  }

  function studioPreferredCandidate(candidates) {
    return candidates.find((item) => item.tool.name === "inspect_incident") ||
      candidates.find((item) => item.tool.effect === "read_only") ||
      candidates[0] || null;
  }

  function studioSetDraft(value) {
    editor.value = value;
    editor.setSelectionRange(0, 0);
    editor.scrollTop = 0;
    document.querySelector("#studio-gutter").scrollTop = 0;
    studioInvalidate();
    studioUpdateByteCount();
  }

  function studioRenderTools() {
    const list = document.querySelector("#studio-tool-list");
    const candidates = studioToolCandidates();
    list.replaceChildren();
    if (!candidates.length) {
      list.append(element("span", "studio-tool-empty", "当前部署没有唯一的普通 Harness Tool"));
    } else {
      candidates.forEach((candidate) => {
        const chip = element("button", "studio-tool-chip", candidate.tool.name);
        chip.type = "button";
        chip.dataset.effect = candidate.tool.effect;
        chip.title = `${candidate.appTitle} · ${candidate.tool.effect}`;
        chip.addEventListener("click", () => studioSetDraft(studioDraftFor(candidate)));
        list.append(chip);
      });
    }
    if (!initialDraft) {
      initialDraft = studioDraftFor(studioPreferredCandidate(candidates));
      studioSetDraft(initialDraft);
    }
  }

  function studioExactResponse(value) {
    const keys = value && typeof value === "object" && !Array.isArray(value)
      ? Object.keys(value).sort()
      : [];
    if (JSON.stringify(keys) !== JSON.stringify(["manifest", "ok", "schema_version"]) ||
        value.ok !== true || value.schema_version !== 1) {
      throw new Error("Workflow preflight response contract is invalid");
    }
    const manifest = value.manifest;
    const app = {
      id: manifest && manifest.app_id,
      availability: { status: "ready", reason_code: null },
      tools: manifest && Array.isArray(manifest.steps)
        ? manifest.steps.map((step) => ({ name: step.dispatch_tool_name }))
        : [],
      workflow: manifest,
    };
    return workflowManifestContract(app);
  }

  function studioExactRejection(status, value) {
    const topKeys = value && typeof value === "object" && !Array.isArray(value)
      ? Object.keys(value).sort()
      : [];
    const error = value && value.error;
    const errorKeys = error && typeof error === "object" && !Array.isArray(error)
      ? Object.keys(error).sort()
      : [];
    const message = error && error.message;
    const messageEncoding = typeof message === "string"
      ? studioEncoding(message)
      : { bytes: 0, scalarValid: false };
    return status === 422 && value && value.ok === false &&
      JSON.stringify(topKeys) === JSON.stringify(["error", "ok"]) &&
      JSON.stringify(errorKeys) === JSON.stringify([
        "code", "message", "reason_code", "retryable",
      ]) &&
      error.code === "workflow_preflight_rejected" &&
      [
        "invalid_definition", "tool_contract_mismatch", "manifest_rejected",
      ].includes(error.reason_code) &&
      error.retryable === false && message.length > 0 &&
      messageEncoding.scalarValid && messageEncoding.bytes <= 512;
  }

  function studioFact(label, value) {
    const fact = element("div", "studio-manifest-fact");
    fact.append(element("small", "", label), element("b", "", String(value)));
    return fact;
  }

  function studioStepSources(step) {
    if (!step.argument_sources.length) return "no arguments";
    return step.argument_sources.map((source) => {
      if (source.kind === "literal") {
        return `${source.name} ← literal:${source.value_type} / sha256:${source.value_sha256.slice(0, 12)}…`;
      }
      return `${source.name} ← ${source.kind}:${source.ref}`;
    }).join(" · ");
  }

  function studioRenderManifest(manifest) {
    const fragment = document.createDocumentFragment();
    const hero = element("section", "studio-manifest-hero");
    const identity = element("div");
    identity.append(
      element("small", "", "STATIC CONTRACT ACCEPTED"),
      element("h3", "", manifest.workflow_id),
      element("p", "", `${manifest.app_id} · version ${manifest.version}`),
    );
    hero.append(identity, element("div", "studio-manifest-seal", "验"));
    fragment.append(hero);

    const facts = element("div", "studio-manifest-facts");
    facts.append(
      studioFact("DEFINITION SHA-256", manifest.definition_sha256),
      studioFact("EXECUTION", manifest.execution),
      studioFact("OUTPUT / STEPS", `${manifest.output_step} / ${manifest.step_count}`),
    );
    fragment.append(facts);

    if (manifest.inputs.length) {
      const inputSection = element("section", "studio-manifest-section");
      const heading = element("header");
      heading.append(element("h4", "", "输入槽"), element("span", "", `${manifest.inputs.length} BOUNDED`));
      inputSection.append(heading);
      const inputFacts = element("div", "studio-manifest-facts");
      manifest.inputs.forEach((input) => inputFacts.append(
        studioFact(input.key, `${input.type} · ${input.required ? "required" : "optional"} · ${input.max_bytes} B`),
      ));
      inputSection.append(inputFacts);
      fragment.append(inputSection);
    }

    const steps = element("section", "studio-manifest-section");
    const stepsHeading = element("header");
    stepsHeading.append(element("h4", "", "串行机关"), element("span", "", "DEFINITION ORDER"));
    steps.append(stepsHeading);
    manifest.steps.forEach((step) => {
      const card = element("article", "studio-step-card");
      card.dataset.position = String(step.position).padStart(2, "0");
      const heading = element("header");
      const effect = element("span", "", step.effect);
      effect.dataset.effect = step.effect;
      heading.append(element("h5", "", step.step_id), effect);
      const grid = element("div", "studio-step-grid");
      [
        ["LOGICAL TOOL", step.logical_tool_name],
        ["DISPATCH TOOL", step.dispatch_tool_name],
        ["DEPENDS ON", step.depends_on.length ? step.depends_on.join(" → ") : "workflow input"],
        ["APPROVAL", step.requires_approval ? "required" : "not required"],
        ["RECOVERY", step.recovery_policy],
        ["REVISION", step.logical_tool_revision || "read-only / unversioned"],
      ].forEach(([label, value]) => {
        const item = element("div");
        item.append(element("small", "", label), element("b", "", value));
        grid.append(item);
      });
      card.append(heading, grid, element("p", "studio-step-sources", studioStepSources(step)));
      steps.append(card);
    });
    fragment.append(steps);

    const trust = element("div", "studio-trust-strip");
    trust.append(
      element("span", "", "TRUSTED INSTALLED PYTHON"),
      element("span", "", "NO SANDBOX / FULL HOST PROCESS"),
    );
    fragment.append(trust);
    preview.replaceChildren(fragment);
  }

  function studioRenderError(error, { rejected = false } = {}) {
    const card = element("section", "studio-error");
    card.dataset.state = rejected ? "rejected" : "unverified";
    const code = typeof error.code === "string" ? error.code : "client_error";
    const reason = typeof error.reasonCode === "string" && error.reasonCode
      ? error.reasonCode : "not_accepted";
    card.append(
      element("small", "", rejected ? "PREFLIGHT REJECTED" : "NO SERVER VERDICT"),
      element("h3", "", rejected ? "图纸未通过服务端机关尺" : "尚未获得服务端结论"),
      element("p", "", error.message || String(error)),
    );
    const facts = element("dl");
    facts.append(
      element("dt", "", "CODE"), element("dd", "", code),
      element("dt", "", "REASON"), element("dd", "", reason),
      element("dt", "", "RETRYABLE"), element("dd", "", error.retryable ? "yes" : "no"),
      element("dt", "", "MUTATION"), element("dd", "", "none"),
    );
    card.append(facts);
    preview.replaceChildren(card);
  }

  async function studioPreflight() {
    const draft = editor.value;
    const { bytes, scalarValid } = studioUpdateByteCount();
    if (!draft.trim() || bytes > maxDefinitionBytes || !scalarValid) {
      studioRenderError(new Error(!scalarValid
        ? "Workflow JSON contains an unpaired Unicode surrogate and cannot be sent byte-exactly"
        : bytes > maxDefinitionBytes
          ? "Workflow JSON exceeds the 1 MiB request boundary"
          : "Workflow JSON draft is empty"));
      studioSetStatus("dirty", "草稿未获得服务端结论");
      return;
    }
    if (activeRequest) activeRequest.controller.abort();
    const controller = new AbortController();
    const request = { controller, draft, editEpoch };
    activeRequest = request;
    preflightButton.disabled = true;
    studioEmptyPreview();
    studioSetStatus("checking", "服务端校验中");
    try {
      const response = await fetch("/v1/workflows/preflight", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json; charset=utf-8" }),
        body: draft,
        signal: controller.signal,
        cache: "no-store",
      });
      const contentType = response.headers.get("content-type") || "";
      const payload = contentType.includes("application/json")
        ? await response.json()
        : await response.text();
      if (!response.ok) {
        const error = new ApiError(response.status, payload);
        error.workflowPreflightRejected = studioExactRejection(
          response.status, payload,
        );
        throw error;
      }
      if (activeRequest !== request || request.editEpoch !== editEpoch ||
          editor.value !== request.draft || controller.signal.aborted) return;
      const manifest = studioExactResponse(payload);
      studioRenderManifest(manifest);
      studioSetStatus("accepted", "当前草稿通过静态合同预检");
    } catch (error) {
      if (error && error.name === "AbortError") return;
      if (activeRequest !== request || request.editEpoch !== editEpoch ||
          editor.value !== request.draft || controller.signal.aborted) return;
      const rejected = error instanceof ApiError &&
        error.workflowPreflightRejected === true;
      studioRenderError(error, { rejected });
      studioSetStatus(
        rejected ? "rejected" : "dirty",
        rejected ? "服务端拒绝草稿合同" : "未获得服务端结论",
      );
    } finally {
      if (activeRequest === request) {
        activeRequest = null;
        preflightButton.disabled = false;
        studioUpdateByteCount();
      }
    }
  }

  function studioOpen() {
    studioRenderTools();
    studio.hidden = false;
    document.body.classList.add("studio-open");
    button.setAttribute("aria-expanded", "true");
    editor.setSelectionRange(0, 0);
    editor.scrollTop = 0;
    document.querySelector("#studio-gutter").scrollTop = 0;
    requestAnimationFrame(() => editor.focus({ preventScroll: true }));
  }

  function studioClose() {
    studioInvalidate({ dirty: Boolean(editor.value.trim()) });
    studio.hidden = true;
    document.body.classList.remove("studio-open");
    button.setAttribute("aria-expanded", "false");
    button.focus();
  }

  editor.addEventListener("input", () => {
    studioInvalidate();
    studioUpdateByteCount();
  });
  editor.addEventListener("scroll", () => {
    document.querySelector("#studio-gutter").scrollTop = editor.scrollTop;
  });
  editor.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.ctrlKey) {
      event.preventDefault();
      studioPreflight();
    }
  });
  button.addEventListener("click", studioOpen);
  document.querySelector("#studio-close").addEventListener("click", studioClose);
  document.querySelector("#studio-reset").addEventListener("click", () => {
    initialDraft = "";
    studioRenderTools();
    editor.focus();
  });
  preflightButton.addEventListener("click", studioPreflight);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !studio.hidden) studioClose();
  });
  studioUpdateByteCount();
}());
