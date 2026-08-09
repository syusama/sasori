"use strict";

/* Artifact Workbench extension. The base application remains immutable. */
state.artifacts = [];
state.artifactRequest = 0;

const artifactMediaForTextPreview = new Set([
  "application/json",
  "text/plain; charset=utf-8",
]);

function artifactContentPath(ref) {
  return `/v1/runs/${encodeURIComponent(ref.run_id)}/artifacts/${encodeURIComponent(ref.artifact_id)}/content`;
}

function artifactRef(value, runId) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      value.version !== 1 || value.run_id !== runId ||
      typeof value.artifact_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.artifact_id) ||
      typeof value.content_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(value.content_sha256) ||
      !Number.isSafeInteger(value.size_bytes) || value.size_bytes < 0 || value.size_bytes > 16 * 1024 * 1024 ||
      typeof value.filename !== "string" || !value.filename || new TextEncoder().encode(value.filename).length > 255 ||
      typeof value.media_type !== "string" || !value.media_type || value.media_type.length > 127 ||
      !Number.isSafeInteger(value.created_seq) || value.created_seq < 1) {
    throw new Error("artifact reference is invalid");
  }
  return Object.freeze({
    version: value.version,
    artifact_id: value.artifact_id,
    run_id: value.run_id,
    content_sha256: value.content_sha256,
    size_bytes: value.size_bytes,
    filename: value.filename,
    media_type: value.media_type,
    created_seq: value.created_seq,
  });
}

function artifactSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}

function closeArtifactPreview() {
  $("#artifact-preview").hidden = true;
  $("#artifact-preview-title").textContent = "产物预览";
  $("#artifact-preview-content").textContent = "";
}

function artifactButton(label, className, handler) {
  const button = element("button", `button ${className}`, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function renderArtifacts() {
  const host = $("#artifact-list");
  host.replaceChildren();
  if (!state.run) {
    host.append(element("p", "empty-copy", "装入运行后，已注册产物会在这里显现。"));
    closeArtifactPreview();
    return;
  }
  if (!state.artifacts.length) {
    host.append(element("p", "empty-copy", "该运行尚无已注册产物。普通 Loop 不会自动扫描工具文件。"));
    closeArtifactPreview();
    return;
  }
  state.artifacts.forEach((ref) => {
    const card = element("article", "artifact-card");
    card.dataset.artifactId = ref.artifact_id;
    const header = element("header");
    header.append(
      element("h3", "", ref.filename),
      element("span", "artifact-integrity", "SHA-256 BOUND"),
    );
    const facts = element("dl", "artifact-meta");
    appendFact(facts, "type", ref.media_type);
    appendFact(facts, "size", artifactSize(ref.size_bytes));
    appendFact(facts, "event", `#${ref.created_seq}`);
    const digestName = element("dt", "", "digest");
    const digest = element("dd", "artifact-digest", ref.content_sha256);
    facts.append(digestName, digest);
    const actions = element("div", "artifact-actions");
    if (artifactMediaForTextPreview.has(ref.media_type)) {
      actions.append(artifactButton("安全预览", "brass artifact-preview-button", () => previewArtifact(ref)));
    }
    actions.append(artifactButton("下载校验件", "ghost artifact-download-button", () => downloadArtifact(ref)));
    card.append(header, facts, actions);
    host.append(card);
  });
}

async function artifactResponse(ref, context) {
  const response = await fetch(artifactContentPath(ref), {
    headers: authHeaders(),
    signal: context.controller.signal,
    cache: "no-store",
  });
  if (!response.ok) {
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    throw new ApiError(response.status, payload);
  }
  const digest = response.headers.get("x-sasori-content-sha256");
  if (digest !== ref.content_sha256) throw new Error("artifact response digest does not match its reference");
  return response;
}

async function previewArtifact(ref) {
  const context = currentRunContext();
  if (!contextIsActive(context) || context.runId !== ref.run_id ||
      !artifactMediaForTextPreview.has(ref.media_type)) return;
  try {
    const response = await artifactResponse(ref, context);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (!contextIsActive(context) || context.runId !== ref.run_id) return;
    if (bytes.length !== ref.size_bytes) throw new Error("artifact response size does not match its reference");
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    $("#artifact-preview-title").textContent = ref.filename;
    $("#artifact-preview-content").textContent = text;
    $("#artifact-preview").hidden = false;
  } catch (error) {
    if (contextIsActive(context)) showError(error, "产物预览失败");
  }
}

async function downloadArtifact(ref) {
  const context = currentRunContext();
  if (!contextIsActive(context) || context.runId !== ref.run_id) return;
  try {
    const response = await artifactResponse(ref, context);
    const blob = await response.blob();
    if (!contextIsActive(context) || context.runId !== ref.run_id) return;
    if (blob.size !== ref.size_bytes) throw new Error("artifact response size does not match its reference");
    const objectUrl = URL.createObjectURL(blob);
    const link = element("a");
    link.href = objectUrl;
    link.download = ref.filename;
    link.hidden = true;
    document.body.append(link);
    try {
      link.click();
      toast("产物已校验", `${ref.filename} · ${artifactSize(ref.size_bytes)}`);
    } finally {
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    }
  } catch (error) {
    if (contextIsActive(context)) showError(error, "产物下载失败");
  }
}

async function loadArtifacts(context = currentRunContext()) {
  if (!contextIsActive(context)) return;
  const request = ++state.artifactRequest;
  const payload = await api(`/v1/runs/${encodeURIComponent(context.runId)}/artifacts`, {
    signal: context.controller.signal,
  });
  if (!contextIsActive(context) || request !== state.artifactRequest) return;
  if (!payload || payload.run_id !== context.runId || !Array.isArray(payload.artifacts)) {
    throw new Error("artifact list response is invalid");
  }
  const refs = payload.artifacts.map((value) => artifactRef(value, context.runId));
  const ids = new Set();
  let sequence = 0;
  refs.forEach((ref) => {
    if (ids.has(ref.artifact_id) || ref.created_seq <= sequence) {
      throw new Error("artifact list is duplicated or out of order");
    }
    ids.add(ref.artifact_id);
    sequence = ref.created_seq;
  });
  state.artifacts = refs;
  renderArtifacts();
}

const activateRunWithoutArtifacts = activateRun;
activateRun = function activateRunWithArtifacts(runId) {
  const context = activateRunWithoutArtifacts(runId);
  state.artifactRequest += 1;
  state.artifacts = [];
  closeArtifactPreview();
  renderArtifacts();
  return context;
};

const addEventWithoutArtifacts = addEvent;
addEvent = function addEventWithArtifacts(projected) {
  const changed = addEventWithoutArtifacts(projected);
  if (changed && projected.event.type === "artifact.available") {
    const context = currentRunContext();
    queueMicrotask(() => loadArtifacts(context).catch((error) => {
      if (contextIsActive(context)) showError(error, "产物目录载入失败");
    }));
  }
  return changed;
};

const openRunWithoutArtifacts = openRun;
openRun = async function openRunWithArtifacts(runId) {
  await openRunWithoutArtifacts(runId);
  const context = currentRunContext();
  if (!contextIsActive(context) || context.runId !== runId) return;
  try {
    await loadArtifacts(context);
  } catch (error) {
    if (contextIsActive(context)) showError(error, "产物目录载入失败");
  }
};

const setInspectorTabWithoutArtifacts = setInspectorTab;
setInspectorTab = function setInspectorTabWithArtifacts(name) {
  if (name !== "artifacts") {
    setInspectorTabWithoutArtifacts(name);
  }
  const selected = name === "artifacts";
  $("#artifacts-tab").setAttribute("aria-selected", String(selected));
  $("#artifacts-tab").tabIndex = selected ? 0 : -1;
  $("#artifacts-panel").hidden = !selected;
  if (selected) {
    $("#timeline-tab").setAttribute("aria-selected", "false");
    $("#timeline-tab").tabIndex = -1;
    $("#timeline-panel").hidden = true;
    $("#surface-tab").setAttribute("aria-selected", "false");
    $("#surface-tab").tabIndex = -1;
    $("#surface-panel").hidden = true;
    const context = currentRunContext();
    if (contextIsActive(context)) loadArtifacts(context).catch((error) => {
      if (contextIsActive(context)) showError(error, "产物目录载入失败");
    });
  }
};

$("#artifacts-tab").addEventListener("click", () => setInspectorTab("artifacts"));
$("#reload-artifacts").addEventListener("click", () => {
  const context = currentRunContext();
  if (contextIsActive(context)) loadArtifacts(context).catch((error) => {
    if (contextIsActive(context)) showError(error, "产物目录载入失败");
  });
});
$("#close-artifact-preview").addEventListener("click", closeArtifactPreview);

$(".inspector-tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const tabs = [$("#timeline-tab"), $("#artifacts-tab"), $("#surface-tab")];
  let index = Math.max(0, tabs.indexOf(document.activeElement));
  if (event.key === "ArrowLeft") index = (index - 1 + tabs.length) % tabs.length;
  if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
  if (event.key === "Home") index = 0;
  if (event.key === "End") index = tabs.length - 1;
  const names = ["timeline", "artifacts", "surface"];
  setInspectorTab(names[index]);
  tabs[index].focus();
}, true);

renderArtifacts();
