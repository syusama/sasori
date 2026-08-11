"use strict";

/*
 * Cancelled-effect recovery policy. The base Workbench remains immutable.
 * A cancelled run may resolve an ambiguous effect, but it can never retry or
 * re-enter the Loop.
 */
const renderOperatorActionWithoutCancelledPolicy = renderOperatorAction;
const resolveRecoveryWithoutCancelledPolicy = resolveRecovery;

resolveRecovery = async function resolveRecoveryWithCancelledPolicy(event) {
  if (!state.run || state.run.state !== "cancelled" ||
      state.run.pause_reason !== "effect_unknown") {
    return resolveRecoveryWithoutCancelledPolicy(event);
  }
  event.preventDefault();
  if (!state.run.pending) return;
  const context = currentRunContext();
  if (!contextIsActive(context) || state.run.run_id !== context.runId) return;
  const form = event.currentTarget;
  const action = $(".recovery-action", form).value;
  if (!new Set(["record_result", "fail"]).has(action)) {
    throw new Error("cancelled recovery action is forbidden");
  }
  let result = null;
  if (action === "record_result") {
    try {
      result = JSON.parse($(".recovery-result", form).value);
    } catch {
      toast("结果 JSON 无效", "请输入有效、有限的 JSON 值。", "error");
      return;
    }
  }
  state.runGate.stopWatcher();
  try {
    const projection = await api(`/v1/runs/${encodeURIComponent(context.runId)}/effect`, {
      method: "POST",
      body: {
        fingerprint: state.run.pending.fingerprint,
        action,
        reason: $(".recovery-reason", form).value,
        result,
      },
      signal: context.controller.signal,
    });
    if (!contextIsActive(context)) return;
    setRunProjection(projection, context);
    await loadEvents(context);
    if (!contextIsActive(context)) return;
    toast("恢复证据已记录", "运行保持已取消状态，不会重新进入 Loop。");
  } catch (error) {
    if (contextIsActive(context)) showError(error, "恢复决定失败");
  }
};

renderOperatorAction = function renderOperatorActionWithCancelledPolicy() {
  renderOperatorActionWithoutCancelledPolicy();
  if (!state.run || state.run.state !== "cancelled" ||
      state.run.pause_reason !== "effect_unknown" || !state.run.pending) return;

  const host = $("#operator-action");
  const form = $("form", host);
  const action = $(".recovery-action", host);
  if (!form || !action) {
    throw new Error("cancelled effect recovery form is unavailable");
  }
  const retry = action.querySelector('option[value="retry"]');
  if (retry) retry.remove();
  const note = element(
    "p",
    "empty-copy cancelled-recovery-note",
    "已取消运行只能记录已核验结果或标记失败；不能重试，也不会重新驱动 Loop。",
  );
  form.prepend(note);
};

renderOperatorAction();

/*
 * Red Sand Atelier shell. This layer owns layout and navigation only; Run,
 * Artifact, Workflow, and event semantics continue to use the production
 * adapters above it.
 */
const panelBounds = Object.freeze({
  left: Object.freeze({ min: 220, max: 380, initial: 286 }),
  right: Object.freeze({ min: 300, max: 520, initial: 370 }),
});
let activeCapabilityFilter = "all";

function clampPanelWidth(side, value) {
  const bounds = panelBounds[side];
  return Math.min(bounds.max, Math.max(bounds.min, Math.round(value)));
}

function setPanelWidth(side, value) {
  const width = clampPanelWidth(side, value);
  document.documentElement.style.setProperty(`--${side}-panel-width`, `${width}px`);
  $(`#${side}-separator`).setAttribute("aria-valuenow", String(width));
  return width;
}

function panelWidth(side) {
  return Number($(`#${side}-separator`).getAttribute("aria-valuenow"));
}

function bindPanelSeparator(side) {
  const separator = $(`#${side}-separator`);
  separator.addEventListener("keydown", (event) => {
    let next = panelWidth(side);
    if (event.key === "ArrowLeft") next -= 20;
    else if (event.key === "ArrowRight") next += 20;
    else if (event.key === "Home") next = panelBounds[side].min;
    else if (event.key === "End") next = panelBounds[side].max;
    else return;
    event.preventDefault();
    setPanelWidth(side, next);
  });
  separator.addEventListener("dblclick", () => setPanelWidth(side, panelBounds[side].initial));
  separator.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    separator.dataset.dragging = "true";
    separator.setPointerCapture(event.pointerId);
  });
  separator.addEventListener("pointermove", (event) => {
    if (separator.dataset.dragging !== "true") return;
    const shell = $("#workbench-shell").getBoundingClientRect();
    const next = side === "left" ? event.clientX - shell.left : shell.right - event.clientX;
    setPanelWidth(side, next);
  });
  const stopDragging = (event) => {
    if (separator.dataset.dragging !== "true") return;
    separator.dataset.dragging = "false";
    if (separator.hasPointerCapture(event.pointerId)) separator.releasePointerCapture(event.pointerId);
  };
  separator.addEventListener("pointerup", stopDragging);
  separator.addEventListener("pointercancel", stopDragging);
}

function setWorkbenchDestination(name) {
  $$("[data-workbench-destination]").forEach((button) => {
    const active = button.dataset.workbenchDestination === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

function openInspectorDestination(name, tab) {
  setInspectorTab(tab);
  setWorkbenchDestination(name);
  if (window.matchMedia("(max-width: 940px)").matches) setMobileView("inspector");
  $(`#${tab}-tab`).focus();
}

const setMobileViewWithoutAtelierNavigation = setMobileView;
setMobileView = function setMobileViewWithAtelierNavigation(view) {
  setMobileViewWithoutAtelierNavigation(view);
  $$(".mobile-nav [data-mobile-view]").forEach((button) => {
    if (button.dataset.mobileView === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
};

function capabilitySection(title, capability, count) {
  const section = element("section", "surface-block");
  section.dataset.capability = capability;
  const header = element("header");
  header.append(element("h3", "", title), element("span", "", String(count).padStart(2, "0")));
  section.append(header);
  return section;
}

function applyCapabilityFilter(name = activeCapabilityFilter) {
  activeCapabilityFilter = name;
  $$("[data-capability-filter]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.capabilityFilter === name));
  });
  $$("#surface-content > *").forEach((section) => {
    const capability = section.dataset.capability || "workflow";
    section.hidden = name !== "all" && capability !== name;
  });
}

const renderSurfaceWithoutCapabilityCenter = renderSurface;
renderSurface = function renderSurfaceWithCapabilityCenter(app) {
  renderSurfaceWithoutCapabilityCenter(app);
  if (!app) return;
  const host = $("#surface-content");
  const sections = $$(".surface-block", host);
  const capabilityByTitle = new Map([
    ["Skills", "skills"],
    ["Tools", "tools"],
    ["Plugin access", "plugins"],
  ]);
  sections.forEach((section, index) => {
    const title = $("h3", section);
    section.dataset.capability = capabilityByTitle.get(title && title.textContent) || (index === 0 ? "providers" : "plugins");
  });

  const mcpPlugins = (app.plugins || []).filter((plugin) =>
    /mcp/i.test(`${plugin.id || ""} ${plugin.name || ""} ${plugin.execution_mode || ""}`));
  const mcp = capabilitySection("MCP transports", "mcp", mcpPlugins.length);
  if (!mcpPlugins.length) {
    mcp.append(element("p", "surface-empty", "该应用目录没有投影独立 MCP transport；Sasori 不会把普通插件冒充为 MCP。"));
  } else {
    mcpPlugins.forEach((plugin) => {
      const card = element("article", "permission-card");
      card.append(element("strong", "", plugin.name), element("p", "", `${plugin.id} · ${plugin.execution_mode}`));
      mcp.append(card);
    });
  }
  const pluginSection = $("[data-capability='plugins']", host);
  host.insertBefore(mcp, pluginSection || null);
  applyCapabilityFilter();
};

const capabilityObserver = new MutationObserver(() => applyCapabilityFilter());
capabilityObserver.observe($("#surface-content"), { childList: true });

const capabilityButtons = $$("[data-capability-filter]");
capabilityButtons.forEach((button, index) => {
  button.addEventListener("click", () => applyCapabilityFilter(button.dataset.capabilityFilter));
  button.addEventListener("keydown", (event) => {
    let next = index;
    if (event.key === "ArrowLeft") next = (index - 1 + capabilityButtons.length) % capabilityButtons.length;
    else if (event.key === "ArrowRight") next = (index + 1) % capabilityButtons.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = capabilityButtons.length - 1;
    else return;
    event.preventDefault();
    capabilityButtons[next].focus();
    applyCapabilityFilter(capabilityButtons[next].dataset.capabilityFilter);
  });
});

$$('[data-workbench-destination="command"]').forEach((button) => button.addEventListener("click", () => {
  setWorkbenchDestination("command");
  setMobileView("stage");
  $("#workbench-main").focus();
}));
$$('[data-workbench-destination="workflows"]').forEach((button) => button.addEventListener("click", () => {
  setWorkbenchDestination("workflows");
}));
$$('[data-workbench-destination="capabilities"]').forEach((button) => button.addEventListener("click", () => {
  openInspectorDestination("capabilities", "surface");
}));
$$('[data-workbench-destination="artifacts"]').forEach((button) => button.addEventListener("click", () => {
  openInspectorDestination("artifacts", "artifacts");
}));
$$('[data-workbench-destination="trace"]').forEach((button) => button.addEventListener("click", () => {
  openInspectorDestination("trace", "timeline");
}));

$("#timeline-tab").addEventListener("click", () => setWorkbenchDestination("trace"));
$("#artifacts-tab").addEventListener("click", () => setWorkbenchDestination("artifacts"));
$("#surface-tab").addEventListener("click", () => setWorkbenchDestination("capabilities"));
$("#studio-close").addEventListener("click", () => setWorkbenchDestination("command"));
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || $("#workflow-studio").hidden) return;
  queueMicrotask(() => {
    if ($("#workflow-studio").hidden) setWorkbenchDestination("command");
  });
});

$$('[data-prompt]').forEach((button) => button.addEventListener("click", () => {
  const input = $("#task-input");
  input.value = button.dataset.prompt;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
}));

bindPanelSeparator("left");
bindPanelSeparator("right");
setMobileView(document.body.dataset.mobileView || "stage");

window.__sasoriWorkbenchShell = Object.freeze({
  applyCapabilityFilter,
  panelBounds,
  setPanelWidth,
  setWorkbenchDestination,
});
