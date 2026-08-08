"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const terminalStates = new Set(["completed", "failed", "cancelled"]);
const state = {
  token: sessionStorage.getItem("sasori.token") || "",
  apps: [],
  selectedApp: null,
  history: [],
  historyBefore: null,
  run: null,
  events: new Map(),
  cursor: 0,
  streamController: null,
};

class ApiError extends Error {
  constructor(status, payload) {
    const error = payload && payload.error ? payload.error : {};
    super(error.message || `HTTP ${status}`);
    this.status = status;
    this.code = error.code || "http_error";
    this.reasonCode = error.reason_code || null;
    this.retryable = Boolean(error.retryable);
  }
}

function element(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
}

function delay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    if (signal) {
      signal.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    }
  });
}

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  return headers;
}

async function api(path, options = {}) {
  const headers = authHeaders(options.headers || {});
  let body = options.body;
  if (body !== undefined && !(body instanceof Uint8Array)) {
    headers["Content-Type"] = "application/json; charset=utf-8";
    body = JSON.stringify(body);
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body,
    signal: options.signal,
    cache: "no-store",
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

function setConnection(kind, label) {
  const signal = $("#connection-signal");
  signal.dataset.state = kind;
  $("#connection-label").textContent = label;
}

function toast(title, message, kind = "info") {
  const item = element("div", `toast ${kind}`);
  item.append(element("strong", "", title), element("p", "", message));
  $("#toast-region").append(item);
  setTimeout(() => item.remove(), 6500);
}

function showError(error, context) {
  if (error && error.name === "AbortError") return;
  const code = error instanceof ApiError ? error.code : "client_error";
  toast(`${context} · ${code}`, error.message || String(error), "error");
  if (error instanceof ApiError && error.status === 401) {
    $("#connection-panel").hidden = false;
    $("#settings-button").setAttribute("aria-expanded", "true");
    setConnection("error", "需要认证");
  } else {
    setConnection("error", "连接异常");
  }
}

function selectedApplication() {
  return state.apps.find((app) => app.id === state.selectedApp) || null;
}

function selectApplication(appId, { focus = false } = {}) {
  const app = state.apps.find((item) => item.id === appId);
  if (!app || app.availability.status !== "ready") return;
  state.selectedApp = appId;
  $$(".worker-card").forEach((card) => {
    const selected = card.dataset.appId === appId;
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-pressed", String(selected));
    if (selected && focus) card.focus();
  });
  $("#selected-worker-label").textContent = `${app.title} / ${app.worker.title}`;
  $("#run-button").disabled = false;
  renderSurface(app);
}

function renderWorkers() {
  const list = $("#worker-list");
  list.replaceChildren();
  state.apps.forEach((app, index) => {
    const ready = app.availability.status === "ready";
    const card = element("button", `worker-card${ready ? "" : " unavailable"}`);
    card.type = "button";
    card.dataset.appId = app.id;
    card.dataset.index = String(index + 1).padStart(2, "0");
    card.disabled = !ready;
    card.setAttribute("aria-pressed", String(app.id === state.selectedApp));
    card.append(
      element("h3", "", app.title),
      element("p", "", app.description),
    );
    const meta = element("div", "worker-meta");
    meta.append(
      element("i"),
      element("span", "", ready ? `${app.worker.title} · READY` : `UNAVAILABLE · ${app.availability.reason_code}`),
    );
    card.append(meta);
    card.addEventListener("click", () => selectApplication(app.id));
    list.append(card);
  });
  const candidate = selectedApplication() || state.apps.find((app) => app.availability.status === "ready");
  if (candidate) selectApplication(candidate.id);
}

async function loadApplications() {
  setConnection("busy", "装入能力面");
  try {
    const payload = await api("/v1/apps");
    if (payload.schema_version !== 1 || !Array.isArray(payload.apps)) {
      throw new Error("unsupported application catalog");
    }
    state.apps = payload.apps;
    renderWorkers();
    setConnection("live", "运行时就绪");
  } catch (error) {
    showError(error, "应用目录载入失败");
  }
}

function historyCard(run) {
  const card = element("button", "history-card");
  card.type = "button";
  card.dataset.runId = run.run_id;
  const header = element("header");
  header.append(
    element("strong", "", run.run_id),
    element("span", "state-dot", run.state),
  );
  header.lastChild.dataset.state = run.state;
  card.append(header, element("p", "", run.input_preview || "（无任务摘要）"));
  card.addEventListener("click", () => openRun(run.run_id));
  return card;
}

function renderHistory({ append = false } = {}) {
  const list = $("#history-list");
  if (!append) list.replaceChildren();
  if (!state.history.length) {
    list.append(element("p", "empty-copy", "尚无耐久运行记录。"));
  } else {
    const existing = new Set($$(".history-card", list).map((item) => item.dataset.runId));
    state.history.forEach((run) => {
      if (!existing.has(run.run_id)) list.append(historyCard(run));
    });
  }
  $("#load-more").hidden = state.historyBefore === null;
  $$(".history-card", list).forEach((card) => {
    card.classList.toggle("selected", state.run && card.dataset.runId === state.run.run_id);
  });
}

async function loadHistory({ more = false } = {}) {
  const query = new URLSearchParams({ limit: "25" });
  if (more && state.historyBefore !== null) query.set("before", String(state.historyBefore));
  try {
    const payload = await api(`/v1/runs?${query}`);
    state.history = more ? state.history.concat(payload.items) : payload.items;
    state.historyBefore = payload.next_before;
    renderHistory({ append: more });
  } catch (error) {
    showError(error, "运行档案载入失败");
  }
}

function setRunProjection(projection) {
  state.run = projection;
  $("#active-run-label").textContent = projection ? projection.run_id : "—";
  $("#sequence-label").textContent = projection ? String(projection.latest_seq || state.cursor) : "0";
  $("#stage-intro").hidden = Boolean(projection);
  $("#run-heading").hidden = !projection;
  if (projection) {
    const app = state.apps.find((item) => item.id === projection.app_id);
    if (app) selectApplication(app.id);
    $("#run-app-title").textContent = app ? `${app.title} / ${app.worker.title}` : projection.app_id || "LEGACY RUN";
    $("#run-title").textContent = projection.input || projection.run_id;
    const seal = $("#run-state");
    seal.dataset.state = projection.state;
    seal.textContent = projection.pause_reason || projection.state;
  }
  renderMessages();
  renderOperatorAction();
  renderHistory();
}

function renderMessages() {
  const stack = $("#message-stack");
  stack.replaceChildren();
  if (!state.run) return;
  if (state.run.input) {
    const user = element("article", "message user");
    user.dataset.label = "OPERATOR / INPUT";
    user.append(element("pre", "", state.run.input));
    stack.append(user);
  }
  if (state.run.final_message) {
    const assistant = element("article", "message assistant");
    assistant.dataset.label = "SASORI / FINAL";
    const content = state.run.final_message.content || "";
    if (/\[UNTRUSTED (?:MCP OUTPUT|EXTERNAL CONTENT)\]/.test(content)) {
      assistant.append(element("span", "untrusted", "UNTRUSTED TOOL CONTENT · TEXT ONLY"));
    }
    assistant.append(element("pre", "", content));
    stack.append(assistant);
  } else if (state.run.state === "running") {
    const pending = element("article", "message assistant");
    pending.dataset.label = "SASORI / WORKING";
    pending.append(element("pre", "", "机关正在咬合，耐久事件将沿右侧时间轴显现……"));
    stack.append(pending);
  }
}

function eventFamily(type) {
  return type.split(".", 1)[0];
}

function eventSummary(projected) {
  const event = projected.event;
  const labels = {
    "run.started": "运行已建立",
    "run.completed": "最终结果已耐久提交",
    "run.failed": "运行失败",
    "run.cancelled": "运行已取消",
    "model.started": "模型推演开始",
    "model.completed": "模型推演完成",
    "model.failed": "模型调用失败",
    "tool.requested": "工具调用已接收",
    "tool.started": "工具已派发",
    "tool.completed": "工具结果已提交",
    "tool.failed": "工具返回错误",
    "approval.requested": "需要人工审批",
    "approval.resolved": "审批决定已记录",
    "recovery.resolved": "人工恢复已记录",
  };
  return labels[event.type] || event.type;
}

function addEvent(projected) {
  if (!projected || typeof projected.seq !== "number" || !projected.event) return;
  if (state.run && projected.event.run_id !== state.run.run_id) return;
  state.events.set(projected.seq, projected);
  state.cursor = Math.max(state.cursor, projected.seq);
  $("#sequence-label").textContent = String(state.cursor);
  renderTimeline();
}

function renderTimeline() {
  const list = $("#timeline-list");
  list.replaceChildren();
  const events = [...state.events.values()].sort((left, right) => left.seq - right.seq);
  $("#event-count").textContent = String(events.length);
  if (!events.length) {
    const empty = element("li", "timeline-empty");
    empty.append(element("i"), element("p", "", "启动任务后，耐久事件会在这里逐格咬合。"));
    list.append(empty);
    return;
  }
  events.forEach((projected) => {
    const event = projected.event;
    const item = element("li", "timeline-event");
    item.dataset.family = eventFamily(event.type);
    const header = element("header");
    header.append(
      element("strong", "", eventSummary(projected)),
      element("small", "", `#${projected.seq} · STEP ${event.step}`),
    );
    item.append(header);
    const detail = [event.tool_name, event.call_id].filter(Boolean).join(" · ");
    if (detail) item.append(element("p", "", detail));
    if (event.data && Object.keys(event.data).length) {
      const disclosure = element("details");
      disclosure.append(
        element("summary", "", "查看语义数据（不受信任文本）"),
        element("pre", "", JSON.stringify(event.data, null, 2)),
      );
      item.append(disclosure);
    }
    list.append(item);
  });
}

function appendFact(list, name, value) {
  list.append(element("dt", "", name), element("dd", "", value === null || value === undefined ? "—" : String(value)));
}

function resumeCard(reason) {
  const card = element("div", "action-card approval-card");
  const title = element("div", "action-title");
  title.append(element("span", "", "EXPLICIT CONTINUATION"), element("b", "", "决定已耐久记录"));
  card.append(title, element("p", "", reason === "retryable_idempotent" ? "幂等工具可沿相同 key 安全恢复。继续仍需显式触发。" : "审批或恢复决定不会自动继续运行。请确认后启动下一格机关。"));
  const button = element("button", "button brass wide", "显式继续运行");
  button.type = "button";
  button.addEventListener("click", resumeRun);
  card.append(button);
  return card;
}

function renderOperatorAction() {
  const host = $("#operator-action");
  host.replaceChildren();
  host.hidden = true;
  if (!state.run) return;
  const pending = state.run.pending;
  if (state.run.pause_reason === "approval_required" && pending) {
    const fragment = $("#approval-template").content.cloneNode(true);
    const facts = $(".action-facts", fragment);
    appendFact(facts, "tool", pending.tool_name);
    appendFact(facts, "effect", pending.effect);
    appendFact(facts, "revision", pending.tool_revision);
    appendFact(facts, "fingerprint", pending.fingerprint);
    appendFact(facts, "idempotency", pending.idempotency_key);
    $(".arguments", fragment).textContent = JSON.stringify(pending.arguments, null, 2);
    $(".approve-action", fragment).addEventListener("click", () => resolveApproval(true));
    $(".deny-action", fragment).addEventListener("click", () => resolveApproval(false));
    host.append(fragment);
    host.hidden = false;
    return;
  }
  if (state.run.pause_reason === "effect_unknown" && pending) {
    const fragment = $("#recovery-template").content.cloneNode(true);
    const facts = $(".action-facts", fragment);
    appendFact(facts, "tool", pending.tool_name);
    appendFact(facts, "effect", pending.effect);
    appendFact(facts, "fingerprint", pending.fingerprint);
    const form = $("form", fragment);
    $(".recovery-action", fragment).addEventListener("change", (event) => {
      $(".result-field", form).hidden = event.target.value !== "record_result";
    });
    form.addEventListener("submit", resolveRecovery);
    host.append(fragment);
    host.hidden = false;
    return;
  }
  if (state.run.pause_reason === "resume_required" || state.run.pause_reason === "retryable_idempotent") {
    host.append(resumeCard(state.run.pause_reason));
    host.hidden = false;
  }
}

async function resolveApproval(approved) {
  if (!state.run || !state.run.pending) return;
  setConnection("busy", approved ? "记录批准" : "记录拒绝");
  try {
    const projection = await api(`/v1/runs/${encodeURIComponent(state.run.run_id)}/approval`, {
      method: "POST",
      body: { fingerprint: state.run.pending.fingerprint, approved },
    });
    setRunProjection(projection);
    await loadEvents();
    setConnection("live", "决定已记录");
    toast("审批已记录", approved ? "工具尚未执行；需要显式继续。" : "拒绝将作为工具错误返回模型；需要显式继续。");
  } catch (error) {
    showError(error, "审批失败");
  }
}

async function resolveRecovery(event) {
  event.preventDefault();
  if (!state.run || !state.run.pending) return;
  const form = event.currentTarget;
  const action = $(".recovery-action", form).value;
  let result = null;
  if (action === "record_result") {
    try {
      result = JSON.parse($(".recovery-result", form).value);
    } catch {
      toast("结果 JSON 无效", "请输入有效、有限的 JSON 值。", "error");
      return;
    }
  }
  try {
    const projection = await api(`/v1/runs/${encodeURIComponent(state.run.run_id)}/effect`, {
      method: "POST",
      body: {
        fingerprint: state.run.pending.fingerprint,
        action,
        reason: $(".recovery-reason", form).value,
        result,
      },
    });
    setRunProjection(projection);
    await loadEvents();
    toast("恢复决定已记录", "运行不会自动继续。请显式恢复。", action === "retry" ? "error" : "info");
  } catch (error) {
    showError(error, "恢复决定失败");
  }
}

async function resumeRun() {
  if (!state.run) return;
  setConnection("busy", "恢复运行");
  try {
    const projection = await api(`/v1/runs/${encodeURIComponent(state.run.run_id)}/resume`, { method: "POST", body: {} });
    setRunProjection(projection);
    await loadEvents();
    if (!terminalStates.has(projection.state)) watchRun(projection.run_id);
    else await loadHistory();
    setConnection("live", "运行时就绪");
  } catch (error) {
    showError(error, "恢复运行失败");
    await refreshCurrentRun();
  }
}

function renderSurface(app) {
  const host = $("#surface-content");
  host.replaceChildren();
  if (!app) return;

  const worker = element("section", "surface-block");
  const workerHeader = element("header");
  workerHeader.append(element("h3", "", app.worker.title), element("span", "", app.worker.model_slot));
  worker.append(workerHeader, element("p", "empty-copy", `${app.worker.id} · 固定 Harness 执行面`));
  host.append(worker);

  const skills = element("section", "surface-block");
  const skillsHeader = element("header");
  skillsHeader.append(element("h3", "", "Skills"), element("span", "", String(app.skills.length).padStart(2, "0")));
  skills.append(skillsHeader);
  app.skills.forEach((skill) => {
    const card = element("article", "skill-card");
    card.append(element("strong", "", skill.title), element("p", "", skill.description));
    skills.append(card);
  });
  host.append(skills);

  const tools = element("section", "surface-block");
  const toolsHeader = element("header");
  toolsHeader.append(element("h3", "", "Tools"), element("span", "", String(app.tools.length).padStart(2, "0")));
  tools.append(toolsHeader);
  app.tools.forEach((tool) => {
    const row = element("article", "tool-row");
    row.append(element("strong", "", tool.name));
    const chip = element("span", "effect-chip", tool.effect);
    chip.dataset.effect = tool.effect;
    row.append(chip, element("p", "", `${tool.description} · rev ${tool.tool_revision || "read-only"}`));
    tools.append(row);
  });
  host.append(tools);

  const permissions = element("section", "surface-block");
  const permissionHeader = element("header");
  permissionHeader.append(element("h3", "", "Plugin access"), element("span", "", "DISCLOSURE"));
  permissions.append(permissionHeader);
  app.plugins.forEach((plugin) => {
    const card = element("article", "permission-card");
    card.append(element("strong", "", plugin.name));
    const facts = element("dl");
    appendFact(facts, "plugin", plugin.id);
    appendFact(facts, "mode", plugin.execution_mode);
    appendFact(facts, "requested", plugin.requested_permissions ? JSON.stringify(plugin.requested_permissions) : "not projected");
    appendFact(facts, "enforced", String(plugin.enforced));
    card.append(facts, element("span", "host-warning", plugin.effective_access));
    permissions.append(card);
  });
  host.append(permissions);
}

async function loadEvents() {
  if (!state.run) return;
  const payload = await api(`/v1/runs/${encodeURIComponent(state.run.run_id)}/events?after_seq=${state.cursor}`);
  payload.events.forEach(addEvent);
  if (state.run) state.run.latest_seq = Math.max(state.run.latest_seq || 0, payload.latest_seq || 0);
}

function parseSseBlock(block) {
  const data = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  return JSON.parse(data.join("\n"));
}

async function streamEvents(runId, signal) {
  const response = await fetch(`/v1/runs/${encodeURIComponent(runId)}/events?after_seq=${state.cursor}`, {
    headers: authHeaders({ Accept: "text/event-stream" }),
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    throw new ApiError(response.status, payload);
  }
  if (!response.body) throw new Error("SSE response has no readable body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (!block.trim()) continue;
      const projected = parseSseBlock(block);
      if (projected) addEvent(projected);
    }
    if (done) break;
  }
}

async function watchRun(runId) {
  if (state.streamController) state.streamController.abort();
  const controller = new AbortController();
  state.streamController = controller;
  let retry = 600;
  while (!controller.signal.aborted && state.run && state.run.run_id === runId) {
    try {
      const projection = await api(`/v1/runs/${encodeURIComponent(runId)}`, { signal: controller.signal });
      setRunProjection(projection);
      await loadEvents();
      if (terminalStates.has(projection.state)) break;
      setConnection("live", "事件流已连接");
      await streamEvents(runId, controller.signal);
      retry = 600;
      const current = await api(`/v1/runs/${encodeURIComponent(runId)}`, { signal: controller.signal });
      setRunProjection(current);
      if (terminalStates.has(current.state) || current.pause_reason) break;
    } catch (error) {
      if (controller.signal.aborted) break;
      if (error instanceof ApiError && error.code === "cursor_ahead") {
        state.events.clear();
        state.cursor = 0;
        renderTimeline();
      } else if (!(error instanceof ApiError && error.status === 404)) {
        setConnection("busy", "事件流重连中");
      }
      await delay(retry, controller.signal);
      retry = Math.min(retry * 1.7, 5000);
    }
  }
  if (!controller.signal.aborted && state.run && terminalStates.has(state.run.state)) {
    setConnection("live", "运行已落盘");
    await loadHistory();
  }
}

async function refreshCurrentRun() {
  if (!state.run) return;
  try {
    const projection = await api(`/v1/runs/${encodeURIComponent(state.run.run_id)}`);
    setRunProjection(projection);
    await loadEvents();
  } catch (error) {
    showError(error, "运行状态同步失败");
  }
}

async function openRun(runId) {
  if (state.streamController) state.streamController.abort();
  state.events.clear();
  state.cursor = 0;
  renderTimeline();
  setConnection("busy", "展开运行档案");
  try {
    const projection = await api(`/v1/runs/${encodeURIComponent(runId)}`);
    setRunProjection(projection);
    await loadEvents();
    if (!terminalStates.has(projection.state) && !projection.pause_reason) watchRun(runId);
    setConnection("live", "档案已装入");
    if (window.innerWidth <= 940) setMobileView("stage");
  } catch (error) {
    showError(error, "运行档案打开失败");
  }
}

async function createRun(event) {
  event.preventDefault();
  const app = selectedApplication();
  const input = $("#task-input").value.trim();
  if (!app || app.availability.status !== "ready" || !input) return;
  const runId = `run-${crypto.randomUUID()}`;
  state.events.clear();
  state.cursor = 0;
  setRunProjection({
    run_id: runId,
    app_id: app.id,
    input,
    state: "running",
    pause_reason: null,
    detail: "submitting",
    step: 0,
    latest_seq: 0,
    final_message: null,
    pending: null,
  });
  renderTimeline();
  $("#task-input").value = "";
  $("#run-button").disabled = true;
  setConnection("busy", "启动机关");
  watchRun(runId);
  try {
    const projection = await api("/v1/runs", {
      method: "POST",
      body: { run_id: runId, app_id: app.id, input },
    });
    setRunProjection(projection);
    await loadEvents();
    setConnection("live", projection.state === "paused" ? "等待操作者" : "运行时就绪");
    if (!terminalStates.has(projection.state) && !projection.pause_reason) watchRun(runId);
    await loadHistory();
  } catch (error) {
    showError(error, "任务启动失败");
    await refreshCurrentRun();
  } finally {
    $("#run-button").disabled = !selectedApplication();
  }
}

function setInspectorTab(name) {
  const timeline = name === "timeline";
  $("#timeline-tab").setAttribute("aria-selected", String(timeline));
  $("#timeline-tab").tabIndex = timeline ? 0 : -1;
  $("#surface-tab").setAttribute("aria-selected", String(!timeline));
  $("#surface-tab").tabIndex = timeline ? -1 : 0;
  $("#timeline-panel").hidden = !timeline;
  $("#surface-panel").hidden = timeline;
}

function setMobileView(view) {
  document.body.dataset.mobileView = view;
  $$(".mobile-nav [data-mobile-view]").forEach((button) => {
    const active = button.dataset.mobileView === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function bindInteractions() {
  $("#task-form").addEventListener("submit", createRun);
  $("#task-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.ctrlKey) {
      event.preventDefault();
      $("#task-form").requestSubmit();
    }
  });
  $("#settings-button").addEventListener("click", () => {
    const panel = $("#connection-panel");
    panel.hidden = !panel.hidden;
    $("#settings-button").setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) $("#token-input").focus();
  });
  $("#connect-button").addEventListener("click", async () => {
    state.token = $("#token-input").value.trim();
    if (state.token) sessionStorage.setItem("sasori.token", state.token);
    else sessionStorage.removeItem("sasori.token");
    $("#connection-panel").hidden = true;
    $("#settings-button").setAttribute("aria-expanded", "false");
    await Promise.all([loadApplications(), loadHistory()]);
  });
  $("#reload-apps").addEventListener("click", loadApplications);
  $("#reload-history").addEventListener("click", () => loadHistory());
  $("#load-more").addEventListener("click", () => loadHistory({ more: true }));
  $("#timeline-tab").addEventListener("click", () => setInspectorTab("timeline"));
  $("#surface-tab").addEventListener("click", () => setInspectorTab("surface"));
  const inspectorTabs = [$("#timeline-tab"), $("#surface-tab")];
  inspectorTabs.forEach((tab, index) => tab.addEventListener("keydown", (event) => {
    let next = index;
    if (event.key === "ArrowLeft") next = (index - 1 + inspectorTabs.length) % inspectorTabs.length;
    else if (event.key === "ArrowRight") next = (index + 1) % inspectorTabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = inspectorTabs.length - 1;
    else return;
    event.preventDefault();
    setInspectorTab(next === 0 ? "timeline" : "surface");
    inspectorTabs[next].focus();
  }));
  $$(".mobile-nav [data-mobile-view]").forEach((button) => button.addEventListener("click", () => setMobileView(button.dataset.mobileView)));
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
      event.preventDefault();
      $("#task-input").focus();
    }
    if (event.key === "Escape" && !$("#connection-panel").hidden) {
      $("#connection-panel").hidden = true;
      $("#settings-button").setAttribute("aria-expanded", "false");
      $("#settings-button").focus();
    }
  });
  window.addEventListener("hashchange", () => {
    const match = location.hash.match(/^#\/runs\/([A-Za-z0-9._-]+)$/);
    if (match) openRun(match[1]);
  });
}

async function start() {
  document.body.dataset.mobileView = "stage";
  $("#token-input").value = state.token;
  bindInteractions();
  await Promise.all([loadApplications(), loadHistory()]);
  const match = location.hash.match(/^#\/runs\/([A-Za-z0-9._-]+)$/);
  if (match) await openRun(match[1]);
}

start();
