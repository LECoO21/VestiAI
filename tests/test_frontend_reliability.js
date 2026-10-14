// Exercise the actual page functions with local HTTP and DOM substitutes.
// No browser, user data, or model service is contacted.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const appSource = fs.readFileSync(path.join(root, "fitmix/static/app.js"), "utf8");
const bindings = appSource.lastIndexOf('\ndocument.querySelectorAll(".tab").forEach');
assert.ok(bindings > 0, "page event bindings must be found");

function harness(storage = new Map()) {
  const elements = new Map();
  const calls = [];
  const server = {
    status: "succeeded", canResume: false, loseResponse: false, loseConfirmationResponse: false, active: [],
    operations: { tryon_basic: 20, tryon_plus: 50, recognition: 1, segmentation: 2 },
    quote: { authorization_id: "auth-1", request_id: "request-1", estimated_cents: 20, max_cents: 20, expires_at: Date.now() + 60000 },
  };
  const auth = { required: false, authenticated: true, role: "admin", owner_key: "owner-a" };
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      hidden: true, disabled: false, open: false, textContent: "", innerHTML: "", dataset: {}, value: "", style: {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      querySelectorAll() { return []; }, querySelector(child) { return element(selector + child); },
      setAttribute() {}, addEventListener() {}, showModal() { this.open = true; }, close() { this.open = false; },
      decode() { return Promise.resolve(); },
    });
    return elements.get(selector);
  }
  const context = vm.createContext({
    FormData, console,
    document: { querySelector: element, querySelectorAll: () => [] },
    window: { scrollTo() {}, matchMedia: () => ({ matches: false }) },
    localStorage: { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    setTimeout: (callback, delay) => delay === 2500 ? setTimeout(callback, 0) : 1, clearTimeout() {},
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      let payload;
      if (url === "/api/tryons" && options.method === "POST") {
        if (server.loseResponse) { server.loseResponse = false; throw new TypeError("response lost after acceptance"); }
        payload = { tryon_id: "tryon-1", job_id: "job-1" };
      } else if (url === "/api/tryon-authorizations") payload = server.quote;
      else if (url === "/api/tryon-authorizations/auth-1/confirm") {
        if (server.loseConfirmationResponse) { server.loseConfirmationResponse = false; throw new TypeError("confirmation response lost"); }
        payload = { authorization_id: "auth-1", request_id: "request-1", confirmed: true };
      }
      else if (url === "/api/auth/status") payload = auth;
      else if (url === "/api/usage") payload = { operations: server.operations, remaining_cents: 500, global_remaining_cents: 5000, unknown_cents: 0 };
      else if (url === "/api/jobs?active=1") payload = { items: server.active };
      else if (url === "/api/jobs/job-1/resume") { server.status = "succeeded"; payload = { job_id: "job-1", entity_id: "tryon-1", kind: "tryon" }; }
      else if (url === "/api/jobs/job-1") payload = { id: "job-1", entity_id: "tryon-1", kind: "tryon", status: server.status, stage: server.status, can_resume: server.canResume, error: server.status === "failed" ? { message: "下载失败" } : null };
      else if (url === "/api/tryons") payload = { items: [{ id: "tryon-1", job_id: "job-1", status: server.status, quality: "basic", result_url: "/files/result.png", person_url: "/files/person.jpg", upper_url: "/files/top.png" }] };
      else if (url === "/api/person-images") payload = { items: [{ id: "person-1", name: "形象照", url: "/files/person.jpg" }] };
      else payload = { items: [] };
      return { ok: true, status: 200, json: async () => payload };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(root, "fitmix/static/pagination.js"), "utf8"), context);
  vm.runInContext(appSource.slice(0, bindings), context);
  const run = (code) => vm.runInContext(code, context);
  run(`applyAuthState(${JSON.stringify(auth)}); state.garments = [{ id: 'top-1', type: 'upper', label_zh: '上衣', crop_url: '/files/top.png', seasons: [], created_at: '', usage_count: 0 }]; state.personImages = [{ id: 'person-1', name: '形象照', url: '/files/person.jpg' }]; state.selectedPersonImageId = 'person-1'; state.selectedGarments.upper = 'top-1';`);
  return { run, element, calls, server, storage, creates: () => calls.filter((call) => call.url === "/api/tryons" && call.options.method === "POST") };
}

async function main() {
  let passed = 0;
  async function test(name, work) { await work(); passed += 1; console.log(`PASS ${name}`); }

  await test("server quote precedes explicit confirmation", async () => {
    const h = harness();
    await h.run("openGenerationConfirm()");
    assert.equal(h.creates().length, 0);
    assert.equal(h.calls.filter((call) => call.url.endsWith("/confirm")).length, 0);
    assert.match(h.element("#generationPrice").textContent, /0\.20/);
    assert.match(h.element("#generationBudget").textContent, /本次上限/);
    await h.run("confirmGeneration()");
    const confirmIndex = h.calls.findIndex((call) => call.url.endsWith("/confirm"));
    const createIndex = h.calls.findIndex((call) => call.url === "/api/tryons" && call.options.method === "POST");
    assert.ok(confirmIndex >= 0 && confirmIndex < createIndex, "explicit server confirmation must precede creation");
    assert.deepEqual(JSON.parse(h.calls[confirmIndex].options.body), { request_id: "request-1" });
    assert.deepEqual(JSON.parse(h.creates()[0].options.body), { authorization_id: "auth-1", request_id: "request-1" });
  });

  await test("lost response survives reload and retries the original request", async () => {
    const first = harness();
    await first.run("openGenerationConfirm()");
    first.server.loseResponse = true;
    await first.run("confirmGeneration()");
    assert.equal(first.run("state.pendingGeneration.submissionAttempted"), true);
    const reloaded = harness(first.storage);
    reloaded.run("restoreWork('owner-b')");
    assert.equal(reloaded.run("state.pendingGeneration"), null);
    reloaded.run("restoreWork('owner-a')");
    await reloaded.run("retryPendingSubmission()");
    assert.equal(reloaded.creates()[0].options.body, first.creates()[0].options.body);
    assert.equal(reloaded.calls.filter((call) => call.url === "/api/tryon-authorizations").length, 0);
    assert.equal(reloaded.calls.filter((call) => call.url === "/api/tryon-authorizations/auth-1/confirm").length, 1);
    assert.equal(reloaded.run("state.pendingGeneration"), null);
  });

  await test("lost confirmation response retries the same consent before creating", async () => {
    const h = harness();
    await h.run("openGenerationConfirm()");
    h.server.loseConfirmationResponse = true;
    await h.run("confirmGeneration()");
    assert.equal(h.creates().length, 0, "creation must wait for server confirmation");
    assert.equal(h.run("state.pendingGeneration.request_id"), "request-1");
    await h.run("retryPendingSubmission()");
    const confirmations = h.calls.filter((call) => call.url.endsWith("/confirm"));
    assert.equal(confirmations.length, 2);
    assert.equal(confirmations[0].options.body, confirmations[1].options.body);
    assert.equal(h.creates().length, 1);
    assert.equal(h.calls.filter((call) => call.url === "/api/tryon-authorizations").length, 1);
  });

  await test("unknown without remote ID cannot resume, known ID can", async () => {
    const h = harness();
    h.server.status = "unknown";
    await h.run("pollJob('job-1', 'tryon', 'tryon-1')");
    assert.equal(h.element("#resumeTryonJob").hidden, true);
    await h.run("recoverTrackedJob('job-1')");
    assert.equal(h.calls.filter((call) => call.url.endsWith("/resume")).length, 0);
    h.server.canResume = true;
    await h.run("pollJob('job-1', 'tryon', 'tryon-1')");
    assert.equal(h.element("#resumeTryonJob").hidden, false);
    await h.run("recoverTrackedJob('job-1')");
    assert.equal(h.calls.filter((call) => call.url.endsWith("/resume")).length, 1);
    assert.equal(h.creates().length, 0);
  });

  await test("waiting or cached download failure resumes without generating", async () => {
    for (const status of ["waiting_result", "failed"]) {
      const h = harness();
      h.server.status = status;
      h.server.canResume = true;
      try { await h.run("pollJob('job-1', 'tryon', 'tryon-1')"); } catch (error) { assert.equal(error.message, "下载失败"); }
      await h.run("recoverTrackedJob('job-1')");
      assert.equal(h.calls.filter((call) => call.url.endsWith("/resume")).length, 1);
      assert.equal(h.creates().length, 0);
    }
  });

  await test("unknown import pricing blocks model submission", async () => {
    const h = harness();
    h.server.operations.recognition = null;
    await h.run("uploadGarment('placeholder-image')");
    assert.equal(h.calls.filter((call) => call.url === "/api/garments" && call.options.method === "POST").length, 0);
    assert.match(h.element("#toastMessage").textContent, /尚未配置/);
  });

  await test("Plus uses the same server authorization and generation entry", async () => {
    const h = harness();
    h.run("state.tryons = [{id:'tryon-1',status:'succeeded',quality:'basic',person_url:'/files/person.jpg',upper_url:'/files/top.png'}];state.currentTryonId='tryon-1';");
    h.server.quote.estimated_cents = h.server.quote.max_cents = 50;
    await h.run("openGenerationConfirm('plus', 'plus')");
    const quote = h.calls.find((call) => call.url === "/api/tryon-authorizations");
    assert.deepEqual(Object.fromEntries(quote.options.body.entries()), { source_tryon_id: "tryon-1", quality: "plus", action: "plus" });
    await h.run("confirmGeneration()");
    assert.equal(h.creates().length, 1);
    assert.equal(h.calls.some((call) => /\/api\/tryons\/.+\/retry/.test(call.url)), false);
  });

  await test("server active jobs recover without saved browser state", async () => {
    const h = harness();
    h.server.status = "unknown";
    h.server.active = [{ id: "job-1", kind: "tryon", entity_id: "tryon-1", status: "unknown", stage: "unknown", can_resume: false }];
    await h.run("recoverSavedWork()");
    assert.equal(h.run("state.trackedJobs['job-1'].status"), "unknown");
    assert.equal(h.creates().length, 0);
    assert.match(h.element("#taskRecovery").innerHTML, /待核实/);
  });
  console.log(`${passed} front-end reliability scenarios passed.`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
