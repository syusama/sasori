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
