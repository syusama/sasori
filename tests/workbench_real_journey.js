(function runRealWorkbenchJourney(global) {
  const INPUT = "browser lifecycle incident";
  const WORKFLOW_INPUT = "browser workflow incident";
  const PHASE_KEY = "sasori.real-journey.phase";
  const RUN_KEY = "sasori.real-journey.run-id";
  const WORKFLOW_RUN_KEY = "sasori.real-journey.workflow-run-id";
  const nativeAnchorClick = global.HTMLAnchorElement.prototype.click;
  let capturedDownload = null;
  const result = document.createElement("pre");
  result.id = "sasori-real-journey-result";
  result.hidden = true;
  document.documentElement.append(result);

  function fail(message) {
    throw new Error(message);
  }

  function assert(condition, message) {
    if (!condition) fail(message);
  }

  global.HTMLAnchorElement.prototype.click = function click() {
    if (this.download && this.href.startsWith("blob:")) {
      capturedDownload = { filename: this.download, href: this.href };
      return;
    }
    return nativeAnchorClick.call(this);
  };

  function tick() {
    return new Promise((resolve) => setTimeout(resolve, 25));
  }

  async function waitFor(predicate, message, turns = 400) {
    for (let index = 0; index < turns; index += 1) {
      if (predicate()) return;
      await tick();
    }
    fail(`timed out: ${message}`);
  }

  async function actionCount() {
    const response = await fetch("/__journey__/action-count", { cache: "no-store" });
    if (!response.ok) fail(`action ledger probe returned HTTP ${response.status}`);
    const payload = await response.json();
    if (!Number.isSafeInteger(payload.count) || payload.count < 0) {
      fail("action ledger probe returned an invalid count");
    }
    return payload.count;
  }

  function assertCapabilitySurface() {
    document.querySelector("#surface-tab").click();
    const text = document.querySelector("#surface-content").textContent;
    assert(text.includes("record_action"), "Incident side-effect tool is not visible");
    assert(text.includes("side_effecting"), "tool effect disclosure is not visible");
    assert(text.includes("sasori_apps.incident"), "Incident plugin identity is not visible");
    assert(text.includes("enforcedfalse"), "non-enforced permission disclosure is not visible");
    assert(text.includes("FULL HOST PROCESS PRIVILEGES"), "effective host privilege warning is not visible");
    document.querySelector("#timeline-tab").click();
  }

  async function studioJourney() {
    assert(await actionCount() === 0, "Studio journey started after an unexpected side effect");
    assert(document.querySelectorAll(".history-card").length === 0,
      "Studio journey started with an unexpected durable run");
    document.querySelector("#studio-button").click();
    await waitFor(
      () => !document.querySelector("#workflow-studio").hidden &&
        document.querySelector("#studio-editor").value.includes("inspect_incident") &&
        document.querySelectorAll(".studio-tool-chip").length > 0,
      "real Static Serial Workflow Studio did not expose a Tool-bound draft",
    );
    const studio = document.querySelector("#workflow-studio");
    assert(studio.textContent.includes("DRAFT ONLY") &&
      studio.textContent.includes("NO EXECUTION") &&
      studio.textContent.includes("TRUSTED PYTHON") &&
      studio.textContent.includes("NO SANDBOX"),
    "real Studio omitted its authority boundary");
    document.querySelector("#studio-preflight").click();
    await waitFor(
      () => document.querySelector("#studio-status").dataset.state === "accepted" &&
        document.querySelector(".studio-manifest-hero"),
      "real Studio preflight did not render the server manifest",
    );
    const preview = document.querySelector("#studio-preview").textContent;
    assert(preview.includes("STATIC CONTRACT ACCEPTED") &&
      preview.includes("inspect_incident") &&
      preview.includes("read_only_replay_allowed") &&
      preview.includes("TRUSTED INSTALLED PYTHON") &&
      preview.includes("NO SANDBOX"),
    "real Studio manifest omitted contract or trust evidence");
    assert(await actionCount() === 0, "Studio preflight executed a Tool side effect");
    assert(document.querySelectorAll(".history-card").length === 0,
      "Studio preflight created a durable run");
    document.querySelector("#studio-close").click();
    assert(document.querySelector("#workflow-studio").hidden,
      "real Studio did not return to the Workbench");
  }

  async function initialJourney() {
    await waitFor(
      () => document.querySelector('.worker-card[data-app-id="incident"]') &&
        !document.querySelector("#run-button").disabled,
      "production Workbench did not load the real Incident application",
    );
    assertCapabilitySurface();
    await studioJourney();

    const input = document.querySelector("#task-input");
    input.value = INPUT;
    document.querySelector("#task-form").requestSubmit();

    await waitFor(
      () => document.querySelector(".approve-action") &&
        document.querySelector("#run-state").dataset.state === "paused",
      "real Incident run did not reach approval_required",
    );
    const runId = document.querySelector("#active-run-label").textContent;
    assert(/^run-[0-9a-f-]+$/i.test(runId), "Workbench did not expose the generated run ID");
    const approvalText = document.querySelector("#operator-action").textContent;
    assert(approvalText.includes("record_action"), "approval does not name the side-effecting tool");
    assert(approvalText.includes(INPUT), "approval does not show the exact Incident arguments");
    assert(await actionCount() === 0, "side effect occurred before approval");

    document.querySelector(".approve-action").click();
    await waitFor(
      () => document.querySelector("#run-state").textContent === "resume_required" &&
        document.querySelector("#operator-action .button.brass"),
      "approval did not stop at the explicit resume boundary",
    );
    assert(await actionCount() === 0, "approval implicitly executed the side effect");

    document.querySelector("#operator-action .button.brass").click();
    await waitFor(
      () => document.querySelector("#run-state").dataset.state === "completed" &&
        document.querySelector("#message-stack").textContent.includes("Incident action recorded"),
      "explicit resume did not complete the real Incident run",
    );
    assert(await actionCount() === 1, "completed journey did not execute exactly one side effect");
    assert(document.querySelector("#event-count").textContent === "17", "visible durable timeline is not the exact 17-event artifact lifecycle");
    document.querySelector("#artifacts-tab").click();
    await waitFor(
      () => document.querySelector(".artifact-card") &&
        document.querySelector("#artifact-list").textContent.includes(`${runId}-result.md`),
      "completed run did not expose its durable artifact card",
    );
    assert(document.querySelector("#artifact-list").textContent.includes("SHA-256 BOUND"), "artifact digest binding is not visible");
    document.querySelector(".artifact-preview-button").click();
    await waitFor(
      () => !document.querySelector("#artifact-preview").hidden &&
        document.querySelector("#artifact-preview-content").textContent.includes("Incident action recorded"),
      "verified text artifact preview did not render",
    );
    document.querySelector(".artifact-download-button").click();
    await waitFor(
      () => document.querySelector("#toast-region").textContent.includes("产物已校验"),
      "authenticated artifact download did not finish",
    );
    assert(
      capturedDownload && capturedDownload.filename === `${runId}-result.md` &&
        capturedDownload.href.startsWith("blob:"),
      "verified artifact download did not construct the expected browser payload",
    );
    document.querySelector("#timeline-tab").click();
    await waitFor(
      () => document.querySelector(`.history-card[data-run-id="${runId}"]`),
      "completed run did not appear in history",
    );

    sessionStorage.setItem(PHASE_KEY, "incident-reopen");
    sessionStorage.setItem(RUN_KEY, runId);
    global.location.reload();
  }

  async function reopenedIncidentJourney(runId) {
    await waitFor(
      () => document.querySelector(`.history-card[data-run-id="${runId}"]`) &&
        !document.querySelector("#run-button").disabled,
      "cold page load did not restore the durable history entry",
    );
    document.querySelector(`.history-card[data-run-id="${runId}"]`).click();
    await waitFor(
      () => document.querySelector("#active-run-label").textContent === runId &&
        document.querySelector("#run-state").dataset.state === "completed" &&
        document.querySelector("#event-count").textContent === "17" &&
        document.querySelector("#message-stack").textContent.includes("Incident action recorded"),
      "cold history reopen did not reconstruct final output and timeline",
    );
    assert(await actionCount() === 1, "history reopen repeated the side effect");
    assertCapabilitySurface();
    document.querySelector("#artifacts-tab").click();
    await waitFor(
      () => document.querySelector(".artifact-card") &&
        document.querySelector("#artifact-list").textContent.includes(`${runId}-result.md`),
      "cold history reopen did not restore the durable artifact",
    );
    document.querySelector(".artifact-preview-button").click();
    await waitFor(
      () => !document.querySelector("#artifact-preview").hidden &&
        document.querySelector("#artifact-preview-content").textContent.includes("Incident action recorded"),
      "cold history artifact preview did not survive reload",
    );

    await workflowJourney(runId);
  }

  function workflowCard() {
    return [...document.querySelectorAll(".worker-card")].find((card) =>
      card.dataset.appId.startsWith("flow.incident-mechanism."));
  }

  function assertWorkflowSurface(expectedStates) {
    document.querySelector("#surface-tab").click();
    const surface = document.querySelector(".workflow-surface");
    assert(surface, "typed Workflow inspection surface is missing");
    const text = surface.textContent;
    assert(text.includes("incident-mechanism"), "Workflow identity is not visible");
    assert(text.includes("DEFINITION BOUND"), "Workflow definition binding is not visible");
    assert(text.includes("inspect_incident"), "logical inspect Tool is not visible");
    assert(text.includes("record_action"), "logical mutable Tool is not visible");
    assert(text.includes("HARNESS WRAPPER"), "wrapper Tool mapping is not visible");
    assert(text.includes("SERIAL ONLY"), "serial-only boundary is not visible");
    assert(text.includes("NO BRANCHES"), "branch non-goal is not visible");
    const steps = [...surface.querySelectorAll(".workflow-step")];
    assert(steps.length === 2, "Workflow ordered step count is invalid");
    assert(
      JSON.stringify(steps.map((step) => step.dataset.stepStatus)) === JSON.stringify(expectedStates),
      `Workflow durable step states are invalid: ${steps.map((step) => step.dataset.stepStatus)}`,
    );
    return surface;
  }

  async function workflowJourney(incidentRunId) {
    const card = workflowCard();
    assert(card, "production Workbench did not load the real typed Workflow application");
    card.click();
    assertWorkflowSurface(["pending", "pending"]);

    const input = document.querySelector("#task-input");
    input.value = WORKFLOW_INPUT;
    document.querySelector("#task-form").requestSubmit();
    await waitFor(
      () => document.querySelector(".approve-action") &&
        document.querySelector("#run-state").dataset.state === "paused" &&
        document.querySelector("#operator-action").textContent.includes("side_effecting"),
      "real typed Workflow did not reach approval_required",
    );
    const workflowRunId = document.querySelector("#active-run-label").textContent;
    assert(/^run-[0-9a-f-]+$/i.test(workflowRunId), "Workflow run ID is invalid");
    assert(workflowRunId !== incidentRunId, "Workflow reused the Incident run ID");
    const approvalText = document.querySelector("#operator-action").textContent;
    assert(approvalText.includes("wf_record_"), "approval does not name the wrapper Tool");
    assert(approvalText.includes("record"), "approval does not expose the bound Workflow step");
    assert(await actionCount() === 1, "Workflow effect occurred before approval");
    const pausedSurface = assertWorkflowSurface(["completed", "approval_required"]);
    assert(pausedSurface.textContent.includes("CURRENT DURABLE STEP · 02 / record"), "current durable step is not visible");
    assert(pausedSurface.textContent.includes("HUMAN GATE"), "Workflow human gate is not visible");

    document.querySelector("#timeline-tab").click();
    document.querySelector(".approve-action").click();
    await waitFor(
      () => document.querySelector("#run-state").textContent === "resume_required" &&
        document.querySelector("#operator-action .button.brass"),
      "Workflow approval did not stop at explicit resume",
    );
    assert(await actionCount() === 1, "Workflow approval implicitly executed the effect");
    assertWorkflowSurface(["completed", "resume_required"]);

    document.querySelector("#timeline-tab").click();
    document.querySelector("#operator-action .button.brass").click();
    await waitFor(
      () => document.querySelector("#run-state").dataset.state === "completed" &&
        document.querySelector("#message-stack").textContent.includes('"workflow_id":"incident-mechanism"'),
      "explicit resume did not complete the typed Workflow",
    );
    assert(await actionCount() === 2, "typed Workflow did not execute exactly one effect");
    assert(document.querySelector("#event-count").textContent === "17", "Workflow durable event count is not 17");
    assertWorkflowSurface(["completed", "completed"]);
    document.querySelector("#artifacts-tab").click();
    await waitFor(
      () => document.querySelector(".artifact-card") &&
        document.querySelector("#artifact-list").textContent.includes(`${workflowRunId}-result.md`),
      "typed Workflow final artifact is not visible",
    );
    document.querySelector(".artifact-preview-button").click();
    await waitFor(
      () => !document.querySelector("#artifact-preview").hidden &&
        document.querySelector("#artifact-preview-content").textContent.includes('"workflow_id":"incident-mechanism"'),
      "typed Workflow artifact preview is invalid",
    );
    await waitFor(
      () => document.querySelector(`.history-card[data-run-id="${workflowRunId}"]`),
      "typed Workflow did not enter durable history",
    );

    sessionStorage.setItem(PHASE_KEY, "workflow-reopen");
    sessionStorage.setItem(RUN_KEY, incidentRunId);
    sessionStorage.setItem(WORKFLOW_RUN_KEY, workflowRunId);
    global.location.reload();
  }

  async function reopenedWorkflowJourney(incidentRunId, workflowRunId) {
    await waitFor(
      () => document.querySelector(`.history-card[data-run-id="${incidentRunId}"]`) &&
        document.querySelector(`.history-card[data-run-id="${workflowRunId}"]`) &&
        workflowCard(),
      "cold page load did not restore both durable runs",
    );
    document.querySelector(`.history-card[data-run-id="${workflowRunId}"]`).click();
    await waitFor(
      () => document.querySelector("#active-run-label").textContent === workflowRunId &&
        document.querySelector("#run-state").dataset.state === "completed" &&
        document.querySelector("#event-count").textContent === "17" &&
        document.querySelector("#message-stack").textContent.includes('"workflow_id":"incident-mechanism"'),
      "cold Workflow reopen did not reconstruct final output and events",
    );
    assert(await actionCount() === 2, "cold Workflow reopen replayed a side effect");
    assertWorkflowSurface(["completed", "completed"]);
    document.querySelector("#artifacts-tab").click();
    await waitFor(
      () => document.querySelector(".artifact-card") &&
        document.querySelector("#artifact-list").textContent.includes(`${workflowRunId}-result.md`),
      "cold Workflow reopen did not restore its durable artifact",
    );
    assertWorkflowSurface(["completed", "completed"]);

    sessionStorage.removeItem(PHASE_KEY);
    sessionStorage.removeItem(RUN_KEY);
    sessionStorage.removeItem(WORKFLOW_RUN_KEY);
    result.dataset.result = "passed";
    result.dataset.runId = incidentRunId;
    result.dataset.workflowRunId = workflowRunId;
    result.dataset.events = "34";
    result.dataset.effects = "2";
    result.dataset.studio = "preflight-passed";
    result.textContent = "PASS:static-workflow-studio-preflight,real-incident-lifecycle,artifact-preview-download,typed-workflow-lifecycle";
    document.title = "Sasori real journey passed";
  }

  async function run() {
    const phase = sessionStorage.getItem(PHASE_KEY);
    const runId = sessionStorage.getItem(RUN_KEY);
    const workflowRunId = sessionStorage.getItem(WORKFLOW_RUN_KEY);
    if (phase === "incident-reopen") {
      assert(runId, "real journey reload lost its run ID");
      await reopenedIncidentJourney(runId);
      return;
    }
    if (phase === "workflow-reopen") {
      assert(runId && workflowRunId, "Workflow reload lost a durable run ID");
      await reopenedWorkflowJourney(runId, workflowRunId);
      return;
    }
    sessionStorage.removeItem(PHASE_KEY);
    sessionStorage.removeItem(RUN_KEY);
    sessionStorage.removeItem(WORKFLOW_RUN_KEY);
    await initialJourney();
  }

  function reportFailure(error) {
    sessionStorage.removeItem(PHASE_KEY);
    sessionStorage.removeItem(RUN_KEY);
    sessionStorage.removeItem(WORKFLOW_RUN_KEY);
    result.dataset.result = "failed";
    result.textContent = `FAIL:${error && error.stack ? error.stack : String(error)}`;
    document.title = "Sasori real journey failed";
  }

  global.addEventListener("error", (event) => reportFailure(event.error || event.message));
  global.addEventListener("unhandledrejection", (event) => reportFailure(event.reason));
  global.addEventListener("load", () => run().catch(reportFailure), { once: true });
})(window);
