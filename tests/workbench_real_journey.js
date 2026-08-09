(function runRealWorkbenchJourney(global) {
  const INPUT = "browser lifecycle incident";
  const PHASE_KEY = "sasori.real-journey.phase";
  const RUN_KEY = "sasori.real-journey.run-id";
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

  async function initialJourney() {
    await waitFor(
      () => document.querySelector('.worker-card[data-app-id="incident"]') &&
        !document.querySelector("#run-button").disabled,
      "production Workbench did not load the real Incident application",
    );
    assertCapabilitySurface();

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

    sessionStorage.setItem(PHASE_KEY, "reopen");
    sessionStorage.setItem(RUN_KEY, runId);
    global.location.reload();
  }

  async function reopenedJourney(runId) {
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

    sessionStorage.removeItem(PHASE_KEY);
    sessionStorage.removeItem(RUN_KEY);
    result.dataset.result = "passed";
    result.dataset.runId = runId;
    result.dataset.events = "17";
    result.dataset.effects = "1";
    result.textContent = "PASS:real-incident-lifecycle,artifact-preview-download";
    document.title = "Sasori real journey passed";
  }

  async function run() {
    const phase = sessionStorage.getItem(PHASE_KEY);
    const runId = sessionStorage.getItem(RUN_KEY);
    if (phase === "reopen") {
      assert(runId, "real journey reload lost its run ID");
      await reopenedJourney(runId);
      return;
    }
    sessionStorage.removeItem(PHASE_KEY);
    sessionStorage.removeItem(RUN_KEY);
    await initialJourney();
  }

  function reportFailure(error) {
    sessionStorage.removeItem(PHASE_KEY);
    sessionStorage.removeItem(RUN_KEY);
    result.dataset.result = "failed";
    result.textContent = `FAIL:${error && error.stack ? error.stack : String(error)}`;
    document.title = "Sasori real journey failed";
  }

  global.addEventListener("error", (event) => reportFailure(event.error || event.message));
  global.addEventListener("unhandledrejection", (event) => reportFailure(event.reason));
  global.addEventListener("load", () => run().catch(reportFailure), { once: true });
})(window);
