"use strict";

/*
 * Sasori Workbench 0.3.0 product-copy and result-presentation layer.
 * Runtime data and public events remain owned by the existing projection path.
 */

(function installProfessionalWorkbenchLayer() {
  function structuredWorkflowResult(value) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      typeof value.workflow_id === "string" && value.workflow_id &&
      typeof value.status === "string" &&
      value.output && typeof value.output === "object" && !Array.isArray(value.output);
  }

  const renderMessagesWithoutStructuredResults = renderMessages;
  renderMessages = function renderMessagesWithStructuredResults() {
    renderMessagesWithoutStructuredResults();
    if (!state.run) return;
    const assistant = $("#message-stack .message.assistant");
    if (!assistant) return;
    if (!state.run.final_message) {
      const pending = $("pre", assistant);
      if (pending) {
        pending.textContent = "正在运行。模型、工具和审批状态会同步显示在右侧事件记录中。";
      }
      return;
    }

    const content = state.run.final_message.content || "";
    let result = null;
    try { result = JSON.parse(content); } catch (_error) { result = null; }
    if (!structuredWorkflowResult(result)) return;

    assistant.classList.add("structured-result");
    assistant.dataset.label = "SASORI / RESULT";
    const overview = element("section", "result-overview");
    const heading = element("header", "result-heading");
    const identity = element("span");
    identity.append(
      element("small", "", "WORKFLOW COMPLETED"),
      element("strong", "", result.workflow_id),
    );
    const status = element("b", "result-status", result.status.toUpperCase());
    status.dataset.state = result.status;
    heading.append(identity, status);

    const value = result.output.value;
    const outcome = element("div", "result-outcome");
    outcome.append(
      element("small", "", "VERIFIED OUTPUT"),
      element("p", "", typeof value === "string" ? value : JSON.stringify(value)),
    );

    const facts = element("dl", "result-facts");
    [
      ["output step", result.output.step_id],
      ["version", result.workflow_version],
      ["definition", result.definition_sha256],
      ["value digest", result.output.value_sha256],
    ].forEach(([label, fact]) => {
      if (fact === null || fact === undefined || fact === "") return;
      facts.append(element("dt", "", label), element("dd", "", String(fact)));
    });
    overview.append(heading, outcome, facts);

    const details = element("details", "result-raw");
    details.append(
      element("summary", "", "查看原始结果"),
      element("pre", "", JSON.stringify(result, null, 2)),
    );
    assistant.replaceChildren(overview, details);
  };

  const renderHistoryWithoutProductCopy = renderHistory;
  renderHistory = function renderHistoryWithProductCopy() {
    renderHistoryWithoutProductCopy();
    const empty = $("#history-list .empty-copy");
    if (empty && empty.textContent === "尚无耐久运行记录。") {
      empty.textContent = "还没有运行记录。";
    }
  };

  const renderTimelineWithoutProductCopy = renderTimeline;
  renderTimeline = function renderTimelineWithProductCopy() {
    renderTimelineWithoutProductCopy();
    const empty = $("#timeline-list .timeline-empty p");
    if (empty) empty.textContent = "运行开始后，版本化事件会按顺序显示在这里。";
  };

  const eventSummaryWithoutProductCopy = eventSummary;
  eventSummary = function eventSummaryWithProductCopy(projected) {
    return eventSummaryWithoutProductCopy(projected)
      .replace("最终结果已耐久提交", "最终结果已持久化保存");
  };

  const renderOperatorActionWithoutProductCopy = renderOperatorAction;
  renderOperatorAction = function renderOperatorActionWithProductCopy() {
    renderOperatorActionWithoutProductCopy();
    const card = $("#operator-action .action-card");
    if (!card) return;
    card.querySelectorAll("b, p").forEach((item) => {
      item.textContent = item.textContent
        .replace("决定已耐久记录", "决定已记录")
        .replace("启动下一格机关", "继续下一步");
    });
  };

  const setConnectionWithoutProductCopy = setConnection;
  setConnection = function setConnectionWithProductCopy(kind, label) {
    setConnectionWithoutProductCopy(kind, label === "启动机关" ? "正在启动" : label);
  };

  if (typeof renderWorkflowSurface === "function") {
    const renderWorkflowSurfaceWithoutProductCopy = renderWorkflowSurface;
    renderWorkflowSurface = function renderWorkflowSurfaceWithProductCopy(app) {
      renderWorkflowSurfaceWithoutProductCopy(app);
      const surface = $("#surface-content .workflow-surface");
      if (!surface) return;
      const heading = $(".workflow-heading h3", surface);
      if (heading) heading.textContent = "串行 Workflow";
      surface.querySelectorAll(".workflow-step-title small").forEach((label) => {
        label.textContent = label.textContent === "OUTPUT MECHANISM" ? "OUTPUT STEP" : "TOOL STEP";
      });
      surface.querySelectorAll(".workflow-step-status").forEach((label) => {
        if (label.textContent === "待装配") label.textContent = "待执行";
      });
    };
  }

  const studioCopy = [
    ["目录为空。新建图纸并执行一次 CAS 保存后，卷宗会在这里显影。", "当前没有已保存的 Workflow。新建定义并完成一次 CAS 保存后，它会出现在这里。"],
    ["正在展开耐久卷宗", "正在加载已保存版本"],
    ["卷宗读取未获权威结果", "未能确认已保存版本"],
    ["正在核验耐久结果", "正在核验保存结果"],
    ["卷宗已存在，但内容并非待核验草稿", "已保存版本与待核验草稿不一致"],
    ["仍无法确定是否完成封存", "仍无法确定保存是否完成"],
    ["新图纸尚未封存", "新定义尚未保存"],
    ["正在封存机关图纸", "正在保存 Workflow 定义"],
    ["服务端拒绝封存当前图纸", "服务端拒绝保存当前定义"],
    ["封存结果未知", "保存结果未知"],
    ["首次保存会生成不可复用的 Catalog UUID 与 revision 1。", "首次保存会生成独立的 Catalog UUID 和 revision 1。"],
    ["图纸尚未显影", "尚未运行预检"],
    ["串行机关", "串行步骤"],
    ["图纸未通过服务端机关尺", "服务端未接受当前定义"],
    ["核验落库结果", "核验保存结果"],
  ];

  function rewriteStudioCopy(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const parent = node.parentElement;
      if (!parent || parent.closest("textarea, pre, code, script, style")) return;
      let value = node.nodeValue;
      studioCopy.forEach(([before, after]) => { value = value.replace(before, after); });
      if (value !== node.nodeValue) node.nodeValue = value;
    });
  }

  const studio = $("#workflow-studio");
  if (studio) {
    rewriteStudioCopy(studio);
    const observer = new MutationObserver(() => rewriteStudioCopy(studio));
    observer.observe(studio, { childList: true, characterData: true, subtree: true });
  }
})();
