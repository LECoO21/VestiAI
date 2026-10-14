const $ = (selector) => document.querySelector(selector);
const state = {
  looks: [],
  garments: [],
  tryons: [],
  personImages: [],
  selectedLookId: null,
  lookFilter: "all",
  selectedGarments: { upper: null, lower: null, dress: null },
  outfitSource: "wardrobe",
  tryonStep: "compose",
  draftLook: null,
  wardrobeFilter: { type: "all", filter: "all", search: "", sort: "recent", scopeIds: null, scopeLabel: "" },
  tryonFilter: { type: "upper", filter: "all", search: "", sort: "used", page: 1, pageSize: 18 },
  resultMode: "result",
  selectedPersonImageId: null,
  personSourceTryonId: null,
  savedOutfitKey: null,
  currentTryonId: null,
  selectedGarmentId: null,
  pendingGeneration: null,
  trackedJobs: {},
  ownerKey: null,
  usage: null,
  auth: null,
  failedLook: null,
  failedGarmentJob: null,
  currentView: "wardrobe",
};

const typeNames = { upper: "上装", lower: "下装", dress: "套装/连衣裙" };
const lookSourceNames = { photo: "人物穿搭照", composition: "衣橱搭配", tryon: "AI 试穿保存" };
const qualityNames = { basic: "基础试穿", plus: "高质量试穿" };
const filterNames = { all: "", favorite: "收藏", spring_autumn: "春秋", summer: "夏", winter: "冬" };
const stageNames = {
  queued: "等待后台任务",
  starting: "启动任务",
  analyzing: "识别风格与单品",
  analyzing_garment: "识别单品类型与季节",
  parsing: "分割服装区域",
  downloading_garments: "保存衣物到本机",
  uploading: "上传临时素材",
  generating: "AI 正在生成试穿图",
  downloading_result: "把结果保存到本机",
  done: "完成",
  submitting: "正在提交，请勿重复生成",
  unknown: "提交结果待核实",
  waiting_result: "等待继续查询",
  recovering: "正在查询原任务",
};

const taskStatusNames = { queued: "等待处理", running: "处理中", unknown: "待核实", waiting_result: "等待查询", failed: "未完成", succeeded: "已完成" };
const activePolls = new Map();

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function heartIcon(filled = false) {
  return `<svg class="heart-icon" aria-hidden="true" viewBox="0 0 24 24"><path ${filled ? 'fill="currentColor"' : ""} d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z"/></svg>`;
}

function garmentSeasonCategory(item) {
  if (item.seasons.includes("summer")) return "夏装";
  if (item.seasons.includes("winter")) return "冬装";
  if (item.seasons.some((season) => season === "spring" || season === "autumn")) return "春秋装";
  return "四季装";
}

function seasonLabels(seasons) {
  const labels = [];
  if (seasons.some((season) => season === "spring" || season === "autumn")) labels.push("春秋");
  if (seasons.includes("summer")) labels.push("夏");
  if (seasons.includes("winter")) labels.push("冬");
  return labels;
}

function matchesSeason(seasons, filter) {
  return filter === "spring_autumn"
    ? seasons.some((season) => season === "spring" || season === "autumn")
    : seasons.includes(filter);
}

async function api(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    let code = "request_failed";
    try {
      const detail = (await response.json()).error || {};
      message = detail.message || message;
      code = detail.code || code;
    } catch (_) { /* response is not JSON */ }
    if (code === "auth_required") $("#accessGate").hidden = false;
    const error = new Error(message);
    error.code = code;
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function money(cents) {
  return typeof cents === "number" && Number.isFinite(cents) ? `¥${(cents / 100).toFixed(2)}` : "尚未配置";
}

function workStorageKey() {
  return state.ownerKey ? `vestiai:work:${encodeURIComponent(state.ownerKey)}` : null;
}

function saveWork() {
  const key = workStorageKey();
  if (!key) return;
  try { localStorage.setItem(key, JSON.stringify({ pendingGeneration: state.pendingGeneration, jobs: state.trackedJobs })); }
  catch (_) { /* A restricted browser may disable local storage; the server still keeps tasks. */ }
  renderTaskRecovery();
}

function restoreWork(ownerKey) {
  if (state.ownerKey === ownerKey) return;
  state.ownerKey = ownerKey;
  state.pendingGeneration = null;
  state.trackedJobs = {};
  state.usage = null;
  const key = workStorageKey();
  if (key) {
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "{}");
      const pending = saved.pendingGeneration;
      if (pending?.authorization_id && pending.request_id) state.pendingGeneration = pending;
      for (const [id, job] of Object.entries(saved.jobs || {})) {
        if (job?.jobId === id && typeof job.context === "string" && typeof job.entityId === "string") state.trackedJobs[id] = job;
      }
    } catch (_) { /* Invalid saved UI state does not affect server-side tasks. */ }
  }
  renderTaskRecovery();
}

function trackJob(jobId, context, entityId, job = {}) {
  if (!jobId) return;
  state.trackedJobs[jobId] = { ...state.trackedJobs[jobId], jobId, context, entityId, status: job.status || "queued", stage: job.stage || "queued", canResume: job.can_resume === true, message: job.error?.message || job.error_message || "" };
  saveWork();
}

function forgetJob(jobId) {
  delete state.trackedJobs[jobId];
  saveWork();
}

function renderTaskRecovery() {
  const panel = $("#taskRecovery");
  if (!panel) return;
  const pending = state.pendingGeneration?.submissionAttempted;
  const jobs = Object.values(state.trackedJobs);
  panel.hidden = !pending && jobs.length === 0;
  if (panel.hidden) return;
  panel.innerHTML = `<h2>继续查看之前的任务</h2>${pending ? '<div class="recovery-row"><p><strong>试穿提交结果暂未收到</strong><span>这次提交已经保存，继续查看会复用原请求。</span></p><button class="secondary-button" data-pending-submission>查看这次提交</button></div>' : ""}${jobs.map((job) => `<div class="recovery-row"><p><strong>${job.context === "tryon" ? "试穿" : "衣物整理"} · ${taskStatusNames[job.status] || "处理中"}</strong><span>${escapeHtml(job.status === "unknown" ? "可能已经提交，请先核实，勿重复生成。" : job.message || stageNames[job.stage] || "可以继续查看原任务。")}</span></p><button class="secondary-button" data-recovery-job="${escapeHtml(job.jobId)}">${job.canResume || job.status === "waiting_result" ? "继续查询" : "查看状态"}</button>${job.status === "failed" ? `<button class="text-button" data-dismiss-job="${escapeHtml(job.jobId)}">收起</button>` : ""}</div>`).join("")}`;
  panel.querySelector("[data-pending-submission]")?.addEventListener("click", retryPendingSubmission);
  panel.querySelectorAll("[data-recovery-job]").forEach((button) => button.addEventListener("click", () => recoverTrackedJob(button.dataset.recoveryJob)));
  panel.querySelectorAll("[data-dismiss-job]").forEach((button) => button.addEventListener("click", () => forgetJob(button.dataset.dismissJob)));
}

async function loadUsage() {
  const usage = await api("/api/usage");
  state.usage = usage;
  const basic = usage.operations?.tryon_basic;
  $("#basicPriceLabel").textContent = `基础试穿 · ${money(basic)}`;
  const notice = $("#usageNotice");
  notice.hidden = false;
  const missing = usage.operations?.recognition == null || usage.operations?.segmentation == null;
  notice.textContent = `本次可用预算 ${money(usage.remaining_cents)}。${missing ? "识别或分割价格尚未配置，请先由服务端设置费用上限后再导入图片。" : `导入一张图片会调用识别和分割，约 ${money(usage.operations.recognition + usage.operations.segmentation)}；重新识别轮廓约 ${money(usage.operations.segmentation)}。`}试穿会另行确认费用，实际收费以百炼账单为准。${usage.unknown_cents > 0 ? `另有 ${money(usage.unknown_cents)} 费用待核实，仍计入预算。` : ""}`;
  return usage;
}

async function checkImportCosts(recognition = true) {
  const usage = await loadUsage();
  if (usage.operations?.segmentation == null || (recognition && usage.operations?.recognition == null)) {
    throw new Error("这次需要的识别或分割价格尚未配置，请先由服务端设置费用上限。图片尚未提交。");
  }
}

function contextForKind(kind) {
  return ({ tryon_draft: "draft", look: "look", parse: "parse", garment: "garment", garment_parse: "garment_parse", tryon: "tryon" })[kind] || "look";
}

async function recoverSavedWork() {
  // The server owns the identity; local storage only remembers work for that identity.
  const ownerKey = state.ownerKey;
  try {
    const active = await api("/api/jobs?active=1");
    if (state.ownerKey !== ownerKey) return;
    for (const job of active.items || []) trackJob(job.id, contextForKind(job.kind), job.entity_id, job);
  } catch (_) { /* Stored jobs and try-on history remain a recovery path if listing fails. */ }
  for (const item of state.tryons) {
    if (item.job_id && ["queued", "running", "unknown", "waiting_result"].includes(item.status)) {
      trackJob(item.job_id, "tryon", item.id, item);
    }
  }
  for (const job of Object.values(state.trackedJobs)) {
    try {
      const current = await api(`/api/jobs/${job.jobId}`);
      if (state.ownerKey !== ownerKey) return;
      trackJob(job.jobId, job.context, job.entityId, current);
      if (current.status === "succeeded") forgetJob(job.jobId);
      else if (["queued", "running"].includes(current.status)) void pollJob(job.jobId, job.context, job.entityId).catch((error) => toast(error.message, true));
    } catch (error) {
      if (error.status === 404) forgetJob(job.jobId);
    }
  }
  if (state.pendingGeneration?.submissionAttempted) showPendingSubmission();
}

async function recoverTrackedJob(jobId) {
  const tracked = state.trackedJobs[jobId];
  if (!tracked) return;
  if (tracked.context === "tryon") { switchView("tryon"); state.currentTryonId = tracked.entityId; }
  else switchView(tracked.context.startsWith("garment") ? "wardrobe" : tracked.context === "draft" ? "tryon" : "looks");
  try {
    if (tracked.canResume || tracked.status === "waiting_result") {
      await api(`/api/jobs/${jobId}/resume`, { method: "POST" });
    }
    await pollJob(jobId, tracked.context, tracked.entityId);
  } catch (error) { toast(error.message, true); }
}

function applyAuthState(auth) {
  state.auth = auth;
  restoreWork(auth.authenticated && auth.owner_key ? auth.owner_key : null);
  $("#accessGate").hidden = !auth.required || auth.authenticated;
  $("#accountButton").hidden = !auth.required || !auth.authenticated;
  const visitor = auth.role === "visitor";
  $("#demoQuota").hidden = !visitor;
  if (visitor) $("#demoQuota").textContent = `剩余 ${auth.quota?.remaining_tryons ?? 0} 次基础试穿`;
  document.querySelectorAll(".admin-only").forEach((element) => { element.hidden = visitor; });
}

async function loadAuthStatus() {
  const auth = await api("/api/auth/status");
  applyAuthState(auth);
  return auth;
}

async function login(event) {
  event.preventDefault();
  const button = $("#accessSubmit");
  const errorText = $("#accessError");
  button.disabled = true;
  errorText.hidden = true;
  try {
    const auth = await api("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("#accessPassword").value }),
    });
    $("#accessPassword").value = "";
    applyAuthState({ ...auth, required: true });
    await loadAuthStatus();
    await loadData();
    await loadUsage();
    await recoverSavedWork();
  } catch (error) {
    errorText.textContent = error.message;
    errorText.hidden = false;
  } finally { button.disabled = false; }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST" });
  location.reload();
}

let toastTimer;
function toast(message, error = false) {
  const element = $("#toast");
  $("#toastMessage").textContent = message;
  element.setAttribute("role", error ? "alert" : "status");
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, error ? 8000 : 4200);
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const activeMotions = new WeakMap();
function playMotion(element, keyframes, reducedKeyframes, duration) {
  if (!element?.animate) return Promise.resolve();
  const reduced = prefersReducedMotion();
  activeMotions.get(element)?.cancel();
  const animation = element.animate(reduced ? reducedKeyframes : keyframes, {
    duration: reduced ? 80 : duration,
    easing: reduced ? "linear" : "cubic-bezier(0.16, 1, 0.3, 1)",
  });
  activeMotions.set(element, animation);
  return animation.finished.catch(() => {}).finally(() => {
    if (activeMotions.get(element) === animation) activeMotions.delete(element);
  });
}

function openDialog(dialog) {
  dialog.classList.remove("is-closing");
  dialog.showModal();
  const surface = dialog.querySelector(".dialog-motion-surface");
  void playMotion(surface, [{ opacity: 0, transform: "translateY(6px) scale(.985)" }, { opacity: 1, transform: "translateY(0) scale(1)" }], [{ opacity: 0 }, { opacity: 1 }], 250);
}

function closeDialog(dialog) {
  if (!dialog?.open || dialog.classList.contains("is-closing")) return;
  dialog.classList.add("is-closing");
  const surface = dialog.querySelector(".dialog-motion-surface");
  void playMotion(surface, [{ opacity: 1, transform: "translateY(0) scale(1)" }, { opacity: 0, transform: "translateY(6px) scale(.985)" }], [{ opacity: 1 }, { opacity: 0 }], 180).then(() => {
    dialog.close();
    dialog.classList.remove("is-closing");
  });
}

function setActiveButtons(selector, activeButton) {
  document.querySelectorAll(selector).forEach((button) => {
    const active = button === activeButton;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function renderCurrentView() {
  if (state.currentView === "looks") renderLooks();
  else if (state.currentView === "tryon") {
    renderTryonPicker();
    renderPersonLibrary();
    renderTryonHistory();
    setTryonStep(state.tryonStep);
  } else renderWardrobeGrid();
}

function switchView(view) {
  const button = document.querySelector(`.tab[data-view="${view}"]`);
  if (!button) return;
  state.currentView = view;
  document.querySelectorAll(".tab").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-selected", String(active));
  });
  $("#looksView").hidden = view !== "looks";
  $("#wardrobeView").hidden = view !== "wardrobe";
  $("#tryonView").hidden = view !== "tryon";
  renderCurrentView();
  window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? "auto" : "smooth" });
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    const badge = $("#healthBadge");
    const banner = $("#credentialBanner");
    $("#settingsButton").hidden = !health.desktop_mode;
    if (health.credentials_configured) {
      badge.className = "health-badge ready";
      badge.innerHTML = `<span></span> ${health.auth_enabled ? "演示服务与凭证就绪" : "本地服务与凭证就绪"}`;
      banner.hidden = true;
    } else {
      badge.className = "health-badge missing";
      badge.innerHTML = "<span></span> 需要配置百炼";
      banner.hidden = false;
      if (health.desktop_mode) {
        banner.textContent = "百炼凭证尚未配置。点击右上角“API 设置”即可开始。";
        if (!$("#settingsDialog").open) await openSettings();
      }
    }
  } catch (error) {
    $("#healthBadge").innerHTML = "<span></span> 服务异常";
    toast(error.message, true);
  }
}

async function openSettings() {
  try {
    const settings = await api("/api/settings");
    $("#settingsWorkspaceId").value = settings.workspace_id;
    $("#settingsApiKey").value = "";
    $("#settingsApiKey").required = !settings.api_key_configured;
    $("#settingsApiKey").placeholder = settings.api_key_configured ? "留空则保留现有 API Key" : "请输入 API Key";
    $("#settingsKeyHint").textContent = settings.api_key_configured ? "API Key 已保存；留空即可继续使用。" : "首次配置时需要填写 API Key。";
    if (!$("#settingsDialog").open) openDialog($("#settingsDialog"));
  } catch (error) { toast(error.message, true); }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = $("#saveSettings");
  button.disabled = true;
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: $("#settingsWorkspaceId").value.trim(), api_key: $("#settingsApiKey").value.trim() }),
    });
    $("#settingsApiKey").value = "";
    closeDialog($("#settingsDialog"));
    await loadHealth();
    toast("百炼设置已保存到这台 Mac");
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

async function loadData() {
  const [looks, garments, tryons, personImages] = await Promise.all([api("/api/looks"), api("/api/garments"), api("/api/tryons"), api("/api/person-images")]);
  state.looks = looks.items;
  state.garments = garments.items;
  state.tryons = tryons.items;
  state.personImages = personImages.items;
  if (state.selectedPersonImageId && !state.personImages.some((item) => item.id === state.selectedPersonImageId)) state.selectedPersonImageId = null;
  if (!state.selectedLookId && state.looks.length) state.selectedLookId = state.looks[0].id;
  if (state.selectedLookId && !state.looks.some((item) => item.id === state.selectedLookId)) state.selectedLookId = state.looks[0]?.id || null;
  renderCurrentView();
}

function selectedLook() {
  return state.looks.find((item) => item.id === state.selectedLookId);
}

function filteredLooks() {
  return state.looks.filter((look) =>
    state.lookFilter === "all" || (state.lookFilter === "favorite" ? look.favorite : matchesSeason(look.seasons, state.lookFilter))
  );
}

function lookDisplayTitle(look) {
  const style = String(look.style_zh || "").trim();
  if (!/[\u3400-\u9fff]/.test(style)) return lookSourceNames[look.source_kind] || "未命名穿搭";
  const base = style.split(/[，,。；;]/)[0].replace(/\s+/g, "").slice(0, 8);
  return /(穿搭|搭配)$/.test(base) ? base : `${base.replace(/风格$/, "风")}穿搭`;
}

function renderLooks() {
  const looks = filteredLooks();
  setActiveButtons("[data-look-filter]", document.querySelector(`[data-look-filter="${state.lookFilter}"]`));
  if (!looks.some((look) => look.id === state.selectedLookId)) state.selectedLookId = looks[0]?.id || null;
  $("#lookCount").textContent = looks.length;
  $("#lookHistory").innerHTML = looks.length ? looks.map((look) => `
    <button class="look-item ${look.id === state.selectedLookId ? "active" : ""}" data-look-id="${look.id}">
      <img src="${look.thumbnail_url || look.source_url}" loading="lazy" decoding="async" alt="">
      <span><strong>${escapeHtml(lookDisplayTitle(look))}</strong><small>${look.favorite ? "已收藏 · " : ""}${escapeHtml(lookSourceNames[look.source_kind] || "穿搭")}</small></span>
    </button>`).join("") : state.looks.length
      ? '<div class="history-empty"><p>当前筛选下没有穿搭。</p><button class="text-button" data-clear-look-filter>查看全部穿搭</button></div>'
      : '<div class="history-empty"><p>还没有穿搭记录。</p><button class="text-button" data-file-target="lookInput">导入完整穿搭</button></div>';
  $("#lookHistory").querySelectorAll("[data-look-id]").forEach((button) => button.addEventListener("click", () => {
    state.selectedLookId = button.dataset.lookId;
    state.failedLook = null;
    renderLooks();
  }));
  $("#lookHistory").querySelector("[data-clear-look-filter]")?.addEventListener("click", () => { state.lookFilter = "all"; renderLooks(); });
  bindFileButtons($("#lookHistory"));
  const look = selectedLook();
  $("#emptyCanvas").hidden = Boolean(look);
  $("#lookStage").hidden = !look;
  $("#favoriteLook").hidden = !look;
  $("#deleteLook").hidden = !look;
  if (look) {
    $("#lookImage").src = look.source_url;
    $("#favoriteLook").textContent = look.favorite ? "已收藏" : "收藏";
    $("#favoriteLook").classList.toggle("active", look.favorite);
    $("#favoriteLook").setAttribute("aria-pressed", String(look.favorite));
  }
  renderLookDetails(look);
}

function renderLookDetails(look) {
  const container = $("#lookDetails");
  if (!look) {
    container.className = "look-details empty-detail";
    container.innerHTML = "<h2>等待一套新穿搭</h2><p>导入完整穿搭后，分析结果会显示在这里。</p>";
    return;
  }
  const seasons = seasonLabels(look.seasons).map((item) => `<span class="chip">${item}</span>`).join("");
  const palette = look.palette.map((item) => `<div class="color-row"><span class="swatch" style="background:${item.hex}"></span><b>${escapeHtml(item.name_zh)}</b><span>${item.hex}</span></div>`).join("");
  const garments = look.garments.map((item) => `
    <article class="garment-card">
      <img src="${item.thumbnail_url || item.crop_url}" loading="lazy" decoding="async" alt="${escapeHtml(item.label_zh)}">
      <div><strong>${escapeHtml(item.label_zh)}</strong><small>${escapeHtml(item.description_zh)}</small><em>已独立存入衣橱</em></div>
    </article>`).join("");
  const failed = state.failedLook?.lookId === look.id ? `<div class="manual-retry"><p>${escapeHtml(state.failedLook.message)}<br>可手动指定衣物类型重新识别轮廓：</p><div class="chips"><button class="chip" data-parse="upper">上装</button><button class="chip" data-parse="lower">下装</button><button class="chip" data-parse="upper,lower">上装＋下装</button><button class="chip" data-parse="dress">套装/连衣裙</button></div></div>` : "";
  container.className = "look-details";
  container.innerHTML = `
    <span class="source-pill">${lookSourceNames[look.source_kind] || "穿搭"}</span>
    <h2>${escapeHtml(lookDisplayTitle(look))}</h2>
    ${look.title_en ? `<p class="look-en-title" lang="en">${escapeHtml(look.title_en)}</p>` : ""}
    ${look.seasons.length ? `<section class="detail-section"><h3>适合季节</h3><div class="chips">${seasons}</div></section>` : ""}
    ${look.palette.length ? `<section class="detail-section"><h3>主要配色</h3><div class="palette">${palette}</div></section>` : ""}
    <section class="detail-section"><div class="section-heading"><h3>识别到的单品 · ${look.garments.length}</h3><button data-view-look-garments>在衣橱中查看</button></div><p class="section-note">删除这条穿搭不会删除已经进入衣橱的单品。</p><div class="garment-list">${garments || "<p class='picker-empty'>尚未形成衣物条目。</p>"}</div></section>
    ${failed}`;
  container.querySelectorAll("[data-parse]").forEach((button) => button.addEventListener("click", () => manualParse(button.dataset.parse.split(","))));
  container.querySelector("[data-view-look-garments]")?.addEventListener("click", () => showGarmentScope(look.garments.map((item) => item.id), `来自“${lookDisplayTitle(look)}”`));
}

async function toggleLookFavorite() {
  const look = selectedLook();
  if (!look) return;
  try {
    await api(`/api/looks/${look.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ favorite: !look.favorite }),
    });
    await loadData();
    toast(look.favorite ? "已取消收藏穿搭" : "已收藏穿搭");
  } catch (error) { toast(error.message, true); }
}

async function toggleFavorite(button) {
  try {
    await api(`/api/garments/${button.dataset.favoriteId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ favorite: button.dataset.favorite !== "true" }),
    });
    await loadData();
    toast(button.dataset.favorite === "true" ? "已取消收藏" : "已加入收藏");
  } catch (error) { toast(error.message, true); }
}

function filteredGarments(filterState) {
  let items = state.garments.filter((item) =>
    (filterState.type === "all" || item.type === filterState.type) &&
    (filterState.filter === "all" || (filterState.filter === "favorite" ? item.favorite : matchesSeason(item.seasons, filterState.filter)))
  );
  if (filterState.scopeIds) items = items.filter((item) => filterState.scopeIds.includes(item.id));
  if (filterState.search) {
    const query = filterState.search.toLocaleLowerCase();
    items = items.filter((item) => `${item.label_zh} ${item.description_zh}`.toLocaleLowerCase().includes(query));
  }
  return items.sort({
    used: (a, b) => (b.last_used_at || "").localeCompare(a.last_used_at || "") || b.usage_count - a.usage_count,
    favorite: (a, b) => Number(b.favorite) - Number(a.favorite) || b.created_at.localeCompare(a.created_at),
    recent: (a, b) => b.created_at.localeCompare(a.created_at),
  }[filterState.sort || "recent"]);
}

function wardrobeHasFilters() {
  const filter = state.wardrobeFilter;
  return filter.type !== "all" || filter.filter !== "all" || Boolean(filter.search || filter.scopeIds) || filter.sort !== "recent";
}

function bindFileButtons(root = document) {
  root.querySelectorAll("[data-file-target]").forEach((button) => {
    if (button.dataset.fileBound) return;
    button.dataset.fileBound = "true";
    button.addEventListener("click", () => document.getElementById(button.dataset.fileTarget)?.click());
  });
}

function renderWardrobeGrid() {
  const garments = filteredGarments(state.wardrobeFilter);
  $("#garmentCount").textContent = `${garments.length} 件`;
  setActiveButtons("[data-wardrobe-type]", document.querySelector(`[data-wardrobe-type="${state.wardrobeFilter.type}"]`));
  setActiveButtons("[data-wardrobe-filter]", document.querySelector(`[data-wardrobe-filter="${state.wardrobeFilter.filter}"]`));
  $("#wardrobeFilterSummary").textContent = filterNames[state.wardrobeFilter.filter] ? `筛选 · ${filterNames[state.wardrobeFilter.filter]}` : "筛选";
  $("#clearWardrobeFilters").hidden = !wardrobeHasFilters();
  const scope = $("#wardrobeScope");
  scope.hidden = !state.wardrobeFilter.scopeIds;
  if (!scope.hidden) scope.innerHTML = `<span>${escapeHtml(state.wardrobeFilter.scopeLabel)} · ${garments.length} 件</span><button data-clear-scope>查看全部衣橱</button>`;
  scope.querySelector("[data-clear-scope]")?.addEventListener("click", () => { state.wardrobeFilter.scopeIds = null; state.wardrobeFilter.scopeLabel = ""; renderWardrobeGrid(); });
  $("#garmentGrid").innerHTML = garments.length ? garments.map((item) => `
    <article class="library-card ${item.duplicate_of ? "possible-duplicate" : ""}">
      <button class="library-image" data-garment-detail="${item.id}"><img src="${item.thumbnail_url || item.crop_url}" loading="lazy" decoding="async" alt="${escapeHtml(item.label_zh)}"><span>${item.source_kind === "direct" ? "直接添加" : "穿搭提取"}</span>${item.duplicate_of ? '<b title="系统检测到衣橱中可能已有相同单品">疑似重复</b>' : ""}</button>
      <div class="library-card-body"><div><small>${typeNames[item.type]} · ${garmentSeasonCategory(item)}</small><strong>${escapeHtml(item.label_zh)}</strong></div><button class="favorite ${item.favorite ? "active" : ""}" data-favorite-id="${item.id}" data-favorite="${item.favorite}" aria-label="${item.favorite ? "取消收藏" : "收藏"}">${heartIcon(item.favorite)}</button></div>
      <p>${escapeHtml(item.description_zh)}</p>
      <div class="usage-line">${item.usage_count ? `试穿过 ${item.usage_count} 次` : "还没有用于试穿"}</div>
      <div class="library-card-footer"><div class="season-dots">${seasonLabels(item.seasons).map((season) => `<span>${season}</span>`).join("") || "<span>四季</span>"}</div><div><button class="text-button" data-try-garment="${item.id}">去试穿</button></div></div>
    </article>`).join("") : state.garments.length
      ? `<div class="library-empty"><h2>没有找到符合条件的单品</h2><p>当前筛选：${escapeHtml([typeNames[state.wardrobeFilter.type], filterNames[state.wardrobeFilter.filter], state.wardrobeFilter.search && `“${state.wardrobeFilter.search}”`].filter(Boolean).join(" · ") || "自定义范围")}</p><button class="secondary-button" data-empty-clear>清除筛选</button></div>`
      : `<div class="library-empty"><h2>添加第一件衣服</h2><p>上传单件衣服，或上传人物穿搭照一次拆分多件衣服。</p><button class="primary-button" data-file-target="garmentInput">添加衣服</button></div>`;
  const grid = $("#garmentGrid");
  grid.querySelectorAll("[data-favorite-id]").forEach((button) => button.addEventListener("click", () => toggleFavorite(button)));
  grid.querySelectorAll("[data-garment-detail]").forEach((button) => button.addEventListener("click", () => openGarmentDetail(button.dataset.garmentDetail)));
  grid.querySelectorAll("[data-try-garment]").forEach((button) => button.addEventListener("click", () => useGarmentForTryon(button.dataset.tryGarment)));
  grid.querySelector("[data-empty-clear]")?.addEventListener("click", clearWardrobeFilters);
  bindFileButtons(grid);
}

function showGarmentScope(ids, label) {
  state.wardrobeFilter.scopeIds = ids;
  state.wardrobeFilter.scopeLabel = label;
  switchView("wardrobe");
  renderWardrobeGrid();
}

function clearWardrobeFilters() {
  state.wardrobeFilter = { type: "all", filter: "all", search: "", sort: "recent", scopeIds: null, scopeLabel: "" };
  $("#garmentSearch").value = "";
  $("#garmentSort").value = "recent";
  $("#wardrobeMoreFilters").open = false;
  renderWardrobeGrid();
}

async function openGarmentDetail(garmentId) {
  try {
    const garment = await api(`/api/garments/${garmentId}`);
    state.selectedGarmentId = garmentId;
    $("#garmentDialogImage").src = garment.crop_url;
    $("#garmentName").value = garment.label_zh;
    $("#garmentType").value = garment.type;
    $("#garmentDescription").value = garment.description_zh;
    document.querySelectorAll('[name="garmentSeason"]').forEach((input) => {
      input.checked = input.value === "spring_autumn"
        ? garment.seasons.some((season) => season === "spring" || season === "autumn")
        : garment.seasons.includes(input.value);
    });
    $("#garmentMeta").innerHTML = `<span>来源：${garment.source_look ? `穿搭“${escapeHtml(lookDisplayTitle(garment.source_look))}”` : garment.source_kind === "direct" ? "直接添加" : "原穿搭已删除"}</span><span>添加时间：${escapeHtml(garment.created_at)}</span>`;
    $("#garmentUsage").innerHTML = `<h3>试穿使用记录 · ${garment.used_tryons.length}</h3>${garment.used_tryons.length ? `<div>${garment.used_tryons.map((item, index) => `<button type="button" data-dialog-tryon="${item.id}" aria-label="查看第 ${index + 1} 次试穿${item.status === "succeeded" ? "结果" : "状态"}"><img src="${item.result_thumbnail_url || item.result_url || item.person_url}" loading="lazy" decoding="async" alt=""><span>${item.status === "succeeded" ? "查看结果" : taskStatusNames[item.status] || "查看状态"}</span></button>`).join("")}</div>` : "<p>这件单品还没有用于试穿。</p>"}`;
    $("#garmentUsage").querySelectorAll("[data-dialog-tryon]").forEach((button) => button.addEventListener("click", () => { closeDialog($("#garmentDialog")); switchView("tryon"); showTryon(state.tryons.find((item) => item.id === button.dataset.dialogTryon)); }));
    openDialog($("#garmentDialog"));
  } catch (error) { toast(error.message, true); }
}

async function saveGarmentDetails(event) {
  event.preventDefault();
  try {
    await api(`/api/garments/${state.selectedGarmentId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label_zh: $("#garmentName").value,
        description_zh: $("#garmentDescription").value,
        type: $("#garmentType").value,
        seasons: [...document.querySelectorAll('[name="garmentSeason"]:checked')].flatMap((input) => input.value === "spring_autumn" ? ["spring", "autumn"] : [input.value]),
      }),
    });
    closeDialog($("#garmentDialog"));
    await loadData();
    toast("单品信息已更新");
  } catch (error) { toast(error.message, true); }
}

async function reparseSelectedGarment() {
  if (!state.selectedGarmentId || !confirm("将按当前分类重新识别衣服轮廓，并替换当前单品图片。继续吗？")) return;
  closeDialog($("#garmentDialog"));
  switchView("wardrobe");
  showGarmentProgress(true, { stage: "queued", progress: 2 });
  try {
    await checkImportCosts(false);
    const created = await api(`/api/garments/${state.selectedGarmentId}/parse`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type: $("#garmentType").value }) });
    await pollJob(created.job_id, "garment_parse", created.garment_id);
  } catch (error) { showGarmentProgress(false); toast(error.message, true); }
}

function showLookProgress(show, job) {
  $("#lookProgress").hidden = !show;
  if (job) { $("#lookProgressStage").textContent = stageNames[job.stage] || job.stage; $("#lookProgressBar").style.width = `${job.progress || 2}%`; }
}

function showTryonProgress(show, job) {
  setTryonStep(show ? "progress" : "person");
  if (job) { $("#tryonProgressStage").textContent = stageNames[job.stage] || job.stage; $("#tryonProgressBar").style.width = `${job.progress || 2}%`; }
}

function showTryonError(message, job = {}) {
  setTryonStep("error");
  $("#tryonErrorTitle").textContent = job.status === "unknown" ? "这次提交还需要核实" : job.status === "waiting_result" || job.status === "running" ? "任务还未结束" : "这次没有完成";
  $("#tryonErrorMessage").textContent = job.status === "unknown" ? `${message} 可能已经产生费用，请勿重复生成。` : message;
  const resume = $("#resumeTryonJob");
  resume.hidden = !job.jobId || (!job.canResume && !["queued", "running", "waiting_result"].includes(job.status));
  resume.dataset.jobId = job.jobId || "";
  resume.textContent = job.canResume || job.status === "waiting_result" ? "继续查询这次任务" : "查看这次任务的进度";
  $("#retryPendingSubmission").hidden = true;
}

function showPendingSubmission() {
  switchView("tryon");
  showTryonError("暂时没有收到提交结果。点击“查看这次提交”会复用同一份请求，不会另建一次试穿。", { status: "unknown" });
  $("#tryonErrorTitle").textContent = "提交结果暂未收到";
  $("#retryPendingSubmission").hidden = false;
}

function showGarmentProgress(show, job) {
  $("#garmentProgress").hidden = !show;
  if (job) { $("#garmentProgressStage").textContent = stageNames[job.stage] || job.stage; $("#garmentProgressBar").style.width = `${job.progress || 2}%`; }
}

function renderFailedGarmentRetry() {
  const panel = $("#garmentRetry");
  panel.hidden = !state.failedGarmentJob;
  if (!state.failedGarmentJob) return;
  panel.innerHTML = `<p>${escapeHtml(state.failedGarmentJob.message)}<br>可以手动指定类型重新识别：</p><div class="chips"><button class="chip" data-retry-garment="upper">上装</button><button class="chip" data-retry-garment="lower">下装</button><button class="chip" data-retry-garment="dress">套装/连衣裙</button></div>`;
  panel.querySelectorAll("[data-retry-garment]").forEach((button) => button.addEventListener("click", () => retryFailedGarment(button.dataset.retryGarment)));
}

function pollJob(jobId, context, entityId) {
  if (activePolls.has(jobId)) return activePolls.get(jobId);
  const promise = pollJobLoop(jobId, context, entityId).finally(() => activePolls.delete(jobId));
  activePolls.set(jobId, promise);
  return promise;
}

async function pollJobLoop(jobId, context, entityId) {
  const ownerKey = state.ownerKey;
  trackJob(jobId, context, entityId);
  const deadline = Date.now() + 210000;
  while (Date.now() < deadline) {
    const job = await api(`/api/jobs/${jobId}`);
    if (state.ownerKey !== ownerKey) return;
    trackJob(jobId, context, entityId, job);
    if (context === "tryon") showTryonProgress(true, job);
    else if (context === "draft") showDraftOutfitProgress(true, job);
    else if (context.startsWith("garment")) showGarmentProgress(true, job);
    else showLookProgress(true, job);
    if (job.status === "succeeded") {
      forgetJob(jobId);
      if (context === "draft") {
        state.draftLook = await api(`/api/tryon-drafts/${entityId}`);
        state.outfitSource = "upload";
        selectDraftOutfit();
        showDraftOutfitProgress(false);
        renderTryonPicker();
      } else if (context.startsWith("garment")) showGarmentProgress(false);
      else if (context !== "tryon") showLookProgress(false);
      state.failedGarmentJob = null;
      renderFailedGarmentRetry();
      if (context !== "draft") await loadData();
      if (context === "tryon") showTryon(state.tryons.find((item) => item.id === entityId));
      else if (!context.startsWith("garment") && context !== "draft") { state.selectedLookId = entityId; state.failedLook = null; renderLooks(); }
      toast({ tryon: "试穿图已保存到本机", draft: "搭配图已识别，尚未写入衣橱", garment: "识别出的衣服已加入我的衣橱", garment_parse: "单品轮廓已重新识别" }[context] || "穿搭与识别单品已保存");
      return;
    }
    if (["unknown", "waiting_result"].includes(job.status)) {
      if (context === "tryon") showTryonError(job.error?.message || (job.status === "unknown" ? "服务端尚未确认这次提交结果。" : "查询暂时停止，可以继续查询原任务。"), { status: job.status, jobId, canResume: job.can_resume });
      else {
        if (context === "draft") showDraftOutfitProgress(false);
        else if (context.startsWith("garment")) showGarmentProgress(false);
        else showLookProgress(false);
        toast(job.status === "unknown" ? "这次任务待核实，请勿重新提交。" : "可以在页面上方继续查询原任务。", true);
      }
      if (context !== "draft") await loadData();
      return;
    }
    if (job.status === "failed") {
      if (context === "tryon") showTryonProgress(false);
      else if (context === "draft") showDraftOutfitProgress(false);
      else if (context.startsWith("garment")) showGarmentProgress(false);
      else showLookProgress(false);
      if (context !== "draft") await loadData();
      if (context === "garment" && !job.can_resume) {
        state.failedGarmentJob = { jobId, message: job.error?.message || "单品识别失败" };
        renderFailedGarmentRetry();
      } else if (!job.can_resume && !context.startsWith("garment") && context !== "tryon" && context !== "draft") {
        state.selectedLookId = entityId;
        state.failedLook = { lookId: entityId, message: job.error?.message || "任务失败" };
        renderLooks();
      } else if (context === "tryon") showTryonError(job.error?.message || "试穿没有完成。", { status: job.status, jobId, canResume: job.can_resume });
      throw new Error(job.error?.message || "任务失败");
    }
    await new Promise((resolve) => setTimeout(resolve, 2500));
  }
  const message = "页面等待已结束，后台任务可能仍在运行。可以继续查看这次任务。";
  if (context === "tryon") showTryonError(message, { status: "running", jobId });
  else toast(message);
}

async function uploadLook(file) {
  if (!file) return;
  const form = new FormData();
  form.append("image", file);
  form.append("source_kind", "photo");
  switchView("looks");
  showLookProgress(true, { stage: "queued", progress: 2 });
  try {
    await checkImportCosts();
    const created = await api("/api/looks", { method: "POST", body: form });
    state.selectedLookId = created.look_id;
    await loadData();
    await pollJob(created.job_id, "look", created.look_id);
  } catch (error) { showLookProgress(false); toast(error.message, true); }
  finally { $("#lookInput").value = ""; }
}

async function uploadGarment(file) {
  if (!file) return;
  const form = new FormData();
  form.append("image", file);
  state.failedGarmentJob = null;
  renderFailedGarmentRetry();
  showGarmentProgress(true, { stage: "queued", progress: 2 });
  try {
    await checkImportCosts();
    const created = await api("/api/garments", { method: "POST", body: form });
    await pollJob(created.job_id, "garment", created.garment_id);
  } catch (error) { showGarmentProgress(false); toast(error.message, true); }
  finally { $("#garmentInput").value = ""; }
}

async function retryFailedGarment(type) {
  if (!state.failedGarmentJob) return;
  showGarmentProgress(true, { stage: "queued", progress: 2 });
  try {
    await checkImportCosts(false);
    const created = await api(`/api/jobs/${state.failedGarmentJob.jobId}/retry-garment`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type }) });
    await pollJob(created.job_id, "garment", created.garment_id);
  } catch (error) { showGarmentProgress(false); toast(error.message, true); }
}

async function manualParse(types) {
  if (!state.selectedLookId) return;
  showLookProgress(true, { stage: "queued", progress: 2 });
  try {
    await checkImportCosts(false);
    const created = await api(`/api/looks/${state.selectedLookId}/parse`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ types }) });
    await pollJob(created.job_id, "parse", state.selectedLookId);
  } catch (error) { showLookProgress(false); toast(error.message, true); }
}

function selectableGarments() {
  return state.outfitSource === "upload" ? (state.draftLook?.garments || []) : state.garments;
}

function setTryonStep(step) {
  state.tryonStep = step;
  const panels = { compose: "#tryonCompose", person: "#tryonEmpty", progress: "#tryonProgress", error: "#tryonError", result: "#tryonResult" };
  Object.entries(panels).forEach(([name, selector]) => { $(selector).hidden = name !== step; });
  const indicatorStep = step === "compose" ? "compose" : step === "person" || step === "error" ? "person" : "result";
  const order = ["compose", "person", "result"];
  document.querySelectorAll("[data-tryon-step-indicator]").forEach((item) => {
    const current = order.indexOf(item.dataset.tryonStepIndicator);
    const active = item.dataset.tryonStepIndicator === indicatorStep;
    item.classList.toggle("active", active);
    item.classList.toggle("complete", current < order.indexOf(indicatorStep));
  });
  if (step === "person") {
    renderGenerationOutfit();
    renderPersonLibrary();
    updateGenerateButton();
  }
}

function setOutfitSource(source) {
  if (!['wardrobe', 'upload'].includes(source)) return;
  const changed = state.outfitSource !== source;
  state.outfitSource = source;
  if (changed) state.selectedGarments = { upper: null, lower: null, dress: null };
  setActiveButtons("[data-outfit-source]", document.querySelector(`[data-outfit-source="${source}"]`));
  $("#wardrobeOutfitSource").hidden = source !== "wardrobe";
  $("#uploadOutfitSource").hidden = source !== "upload";
  if (source === "upload" && state.draftLook) selectDraftOutfit();
  renderTryonPicker();
}

function selectDraftOutfit() {
  const items = state.draftLook?.garments || [];
  const dress = items.find((item) => item.type === "dress");
  state.selectedGarments = dress
    ? { upper: null, lower: null, dress: dress.id }
    : { upper: items.find((item) => item.type === "upper")?.id || null, lower: items.find((item) => item.type === "lower")?.id || null, dress: null };
  state.savedOutfitKey = null;
}

function renderDraftOutfit() {
  const preview = $("#draftOutfitPreview");
  const look = state.draftLook;
  preview.hidden = !look;
  if (!look) { preview.innerHTML = ""; return; }
  preview.innerHTML = `<img src="${look.thumbnail_url || look.source_url}" alt="已上传的搭配图"><div><strong>${escapeHtml(look.style_zh || "已识别搭配")}</strong><span>识别出 ${look.garments.length} 件衣服</span><div>${look.garments.map((item) => `<span>${escapeHtml(item.label_zh)}</span>`).join("")}</div><button class="text-button" data-file-target="draftOutfitInput">更换搭配图</button></div>`;
  bindFileButtons(preview);
}

function renderTryonPicker() {
  const selectable = selectableGarments();
  for (const type of Object.keys(state.selectedGarments)) if (state.selectedGarments[type] && !selectable.some((item) => item.id === state.selectedGarments[type])) state.selectedGarments[type] = null;
  setActiveButtons("[data-outfit-source]", document.querySelector(`[data-outfit-source="${state.outfitSource}"]`));
  $("#wardrobeOutfitSource").hidden = state.outfitSource !== "wardrobe";
  $("#uploadOutfitSource").hidden = state.outfitSource !== "upload";
  setActiveButtons("[data-tryon-type]", document.querySelector(`[data-tryon-type="${state.tryonFilter.type}"]`));
  setActiveButtons("[data-tryon-filter]", document.querySelector(`[data-tryon-filter="${state.tryonFilter.filter}"]`));
  $("#tryonFilterSummary").textContent = filterNames[state.tryonFilter.filter] ? `筛选 · ${filterNames[state.tryonFilter.filter]}` : "筛选";
  const allMatches = filteredGarments({ ...state.tryonFilter, type: "all", scopeIds: null });
  const counts = Object.fromEntries(Object.keys(typeNames).map((type) => [type, allMatches.filter((item) => item.type === type).length]));
  $("#tryonUpperCount").textContent = counts.upper;
  $("#tryonLowerCount").textContent = counts.lower;
  $("#tryonDressCount").textContent = counts.dress;
  const garments = allMatches.filter((item) => item.type === state.tryonFilter.type);
  const page = paginateItems(garments, state.tryonFilter.page, state.tryonFilter.pageSize);
  state.tryonFilter.page = page.page;
  $("#tryonResultCount").textContent = state.outfitSource === "upload" ? `${selectable.length} 件` : `${garments.length} 件`;
  $("#pickerRange").textContent = garments.length ? `${page.start + 1}–${page.start + page.items.length} / ${garments.length}` : "0 件";
  $("#pickerPrev").disabled = page.page === 1;
  $("#pickerNext").disabled = page.page === page.pages;
  $("#wardrobePicker").innerHTML = page.items.length ? page.items.map((item) => `<button class="picker-card ${state.selectedGarments[item.type] === item.id ? "selected" : ""}" aria-pressed="${state.selectedGarments[item.type] === item.id}" data-pick-type="${item.type}" data-pick-id="${item.id}">${item.favorite ? `<span class="picker-heart">${heartIcon(true)}</span>` : ""}<img src="${item.thumbnail_url || item.crop_url}" loading="lazy" decoding="async" alt="${escapeHtml(item.label_zh)}"><strong>${escapeHtml(item.label_zh)}</strong><small>${seasonLabels(item.seasons).join(" · ") || "四季"}</small></button>`).join("") : state.garments.length ? `<div class="picker-empty"><strong>没有符合条件的${typeNames[state.tryonFilter.type]}</strong><span>可以调整搜索或筛选条件。</span></div>` : `<div class="picker-empty"><strong>衣橱里还没有可以选择的衣服</strong><span>也可以直接上传一张搭配图。</span><button class="secondary-button" data-empty-upload-outfit>上传搭配图</button></div>`;
  $("#wardrobePicker").querySelectorAll("[data-pick-id]").forEach((button) => button.addEventListener("click", () => selectTryonGarment(button.dataset.pickType, button.dataset.pickId)));
  $("#wardrobePicker").querySelector("[data-empty-upload-outfit]")?.addEventListener("click", () => setOutfitSource("upload"));
  renderDraftOutfit();
  renderSelectedOutfit();
  updateGenerateButton();
}

function selectTryonGarment(type, id) {
  const next = state.selectedGarments[type] === id ? null : id;
  if (type === "dress") state.selectedGarments = { upper: null, lower: null, dress: next };
  else { state.selectedGarments[type] = next; state.selectedGarments.dress = null; }
  if (type === "upper" && next) { state.tryonFilter.type = "lower"; state.tryonFilter.page = 1; }
  state.savedOutfitKey = null;
  renderTryonPicker();
}

function selectedOutfitItems() {
  const selectable = selectableGarments();
  return Object.entries(state.selectedGarments).map(([type, id]) => id ? { type, item: selectable.find((garment) => garment.id === id) } : null).filter((entry) => entry?.item);
}

function renderSelectedOutfit() {
  const items = selectedOutfitItems();
  const tray = $("#selectedOutfit");
  const previousIds = new Set([...tray.querySelectorAll("[data-outfit-id]")].map((button) => button.dataset.outfitId));
  tray.innerHTML = items.length ? items.map(({ type, item }) => `<button data-clear-type="${type}" data-outfit-id="${item.id}" aria-label="移除${escapeHtml(item.label_zh)}"><img src="${item.thumbnail_url || item.crop_url}" loading="lazy" decoding="async" alt=""><b>${escapeHtml(item.label_zh)}</b><i aria-hidden="true"></i></button>`).join("") : "<small>还没有选择衣服</small>";
  tray.querySelectorAll("[data-clear-type]").forEach((button) => {
    if (!previousIds.has(button.dataset.outfitId)) void playMotion(button, [{ opacity: 0, transform: "translateX(-6px) scale(.98)" }, { opacity: 1, transform: "translateX(0) scale(1)" }], [{ opacity: 0 }, { opacity: 1 }], 180);
    button.addEventListener("click", async () => {
      button.disabled = true;
      await playMotion(button, [{ opacity: 1, transform: "translateX(0) scale(1)" }, { opacity: 0, transform: "translateX(-6px) scale(.98)" }], [{ opacity: 1 }, { opacity: 0 }], 150);
      state.selectedGarments[button.dataset.clearType] = null;
      state.savedOutfitKey = null;
      renderTryonPicker();
    });
  });
  const key = items.map(({ item }) => item.id).sort().join(":");
  const hasPair = Boolean(state.selectedGarments.upper && state.selectedGarments.lower);
  const hasDress = Boolean(state.selectedGarments.dress);
  const canSave = state.outfitSource === "upload" ? items.length > 0 : hasPair || hasDress;
  const saved = canSave && state.savedOutfitKey === key;
  $("#saveOutfit").disabled = !canSave || saved;
  $("#saveOutfit").textContent = saved ? "已保存到穿搭库" : "保存到穿搭库";
  $("#goTryon").disabled = items.length === 0;
  $("#saveRequirement").textContent = saved ? "这套搭配已进入穿搭库。" : state.outfitSource === "upload" && items.length ? "搭配图已就绪，可直接保存或试穿。" : hasPair ? "上装和下装已选好。" : hasDress ? "套装/连衣裙可直接保存或试穿。" : state.selectedGarments.upper ? "再选择一件下装即可保存。" : state.selectedGarments.lower ? "再选择一件上装即可保存。" : "选择一件上装和一件下装，或一件套装/连衣裙。";
}

async function saveSelectedOutfit() {
  const items = selectedOutfitItems();
  if (!items.length || (state.outfitSource === "wardrobe" && items.length < 2 && !state.selectedGarments.dress)) return;
  const key = items.map(({ item }) => item.id).sort().join(":");
  $("#saveOutfit").disabled = true;
  try {
    const look = state.outfitSource === "upload"
      ? await api(`/api/tryon-drafts/${state.draftLook.id}/save`, { method: "POST" })
      : await api("/api/looks/from-garments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ garment_ids: items.map(({ item }) => item.id) }),
      });
    if (state.outfitSource === "upload") {
      state.draftLook = null;
      state.outfitSource = "wardrobe";
      state.selectedGarments = { upper: null, lower: null, dress: null };
      look.garments.forEach((item) => { state.selectedGarments[item.type] = item.id; });
    }
    state.savedOutfitKey = look.garments.map((item) => item.id).sort().join(":");
    state.selectedLookId = look.id;
    await loadData();
    toast("搭配已保存到穿搭库");
  } catch (error) { toast(error.message, true); renderSelectedOutfit(); }
}

function renderGenerationOutfit() {
  const items = selectedOutfitItems();
  $("#generationOutfit").innerHTML = items.map(({ item }) => `<article><img src="${item.thumbnail_url || item.crop_url}" alt=""><div><strong>${escapeHtml(item.label_zh)}</strong><span>${typeNames[item.type]}</span></div></article>`).join("");
}

function showDraftOutfitProgress(show, job) {
  $("#draftOutfitProgress").hidden = !show;
  $("#draftOutfitPreview").hidden = show || !state.draftLook;
  if (job) {
    $("#draftOutfitProgressStage").textContent = stageNames[job.stage] || job.stage;
    $("#draftOutfitProgressBar").style.width = `${job.progress || 2}%`;
  }
}

async function uploadDraftOutfit(file) {
  if (!file) return;
  const form = new FormData();
  form.append("image", file);
  showDraftOutfitProgress(true, { stage: "queued", progress: 2 });
  try {
    await checkImportCosts();
    const created = await api("/api/tryon-drafts", { method: "POST", body: form });
    await pollJob(created.job_id, "draft", created.draft_id);
  } catch (error) {
    showDraftOutfitProgress(false);
    toast(error.message, true);
  } finally { $("#draftOutfitInput").value = ""; }
}

function useGarmentForTryon(garmentId) {
  const garment = state.garments.find((item) => item.id === garmentId);
  if (!garment) return;
  state.outfitSource = "wardrobe";
  state.tryonStep = "compose";
  state.tryonFilter.type = garment.type;
  selectTryonGarment(garment.type, garment.id);
  switchView("tryon");
  if ($("#garmentDialog").open) closeDialog($("#garmentDialog"));
}

function selectedPersonUrl() {
  const saved = state.personImages.find((item) => item.id === state.selectedPersonImageId);
  const reused = state.tryons.find((item) => item.id === state.personSourceTryonId);
  return saved?.url || reused?.person_url || "";
}

function renderPersonLibrary() {
  $("#personCount").textContent = `${state.personImages.length} / 6`;
  $("#personLibrary").innerHTML = state.personImages.length ? state.personImages.map((item) => `
    <article class="person-card ${item.id === state.selectedPersonImageId ? "selected" : ""}">
      <button class="person-select" data-person-image="${item.id}" aria-pressed="${item.id === state.selectedPersonImageId}"><img src="${item.thumbnail_url || item.url}" loading="lazy" decoding="async" alt="${escapeHtml(item.name)}"><strong>${escapeHtml(item.name)}</strong></button>
      <button class="person-delete" data-delete-person="${item.id}" aria-label="删除${escapeHtml(item.name)}">删除</button>
    </article>`).join("") : '<p class="person-empty">还没有保存的形象照</p>';
  $("#personLibrary").querySelectorAll("[data-person-image]").forEach((button) => button.addEventListener("click", () => {
    state.selectedPersonImageId = state.selectedPersonImageId === button.dataset.personImage ? null : button.dataset.personImage;
    state.personSourceTryonId = null;
    renderPersonLibrary();
    updateGenerateButton();
  }));
  $("#personLibrary").querySelectorAll("[data-delete-person]").forEach((button) => button.addEventListener("click", () => deletePersonImage(button.dataset.deletePerson)));
  const url = selectedPersonUrl();
  $("#personPreview").hidden = !url;
  $("#personPlaceholder").hidden = Boolean(url);
  if (url) $("#personPreview").src = url;
  const full = state.personImages.length >= 6;
  $("#personInput").disabled = full;
  $("#personDrop").disabled = full;
  $("#personDrop").classList.toggle("full", full);
  $("#personDrop").title = full ? "已保存 6 张，请先删除一张再添加" : "添加全身正面形象照";
  $("#personSourceNote").hidden = !state.personSourceTryonId;
  if (!url) {
    $("#personPlaceholder strong").textContent = full ? "形象库已满" : "添加形象照";
    $("#personPlaceholder small").textContent = full ? "删除后可继续添加" : "最多保存 6 张";
  }
}

async function uploadPersonImage(file) {
  if (!file) return;
  const form = new FormData();
  form.append("image", file);
  try {
    const image = await api("/api/person-images", { method: "POST", body: form });
    state.selectedPersonImageId = image.id;
    state.personSourceTryonId = null;
    await loadData();
    toast(`${image.name}已保存并选中`);
  } catch (error) { toast(error.message, true); }
  finally { $("#personInput").value = ""; }
}

async function deletePersonImage(imageId) {
  const image = state.personImages.find((item) => item.id === imageId);
  if (!image || !confirm(`删除“${image.name}”？历史试穿结果不会受影响。`)) return;
  try {
    await api(`/api/person-images/${imageId}`, { method: "DELETE" });
    if (state.selectedPersonImageId === imageId) state.selectedPersonImageId = null;
    await loadData();
    toast(`${image.name}已删除`);
  } catch (error) { toast(error.message, true); }
}

function updateGenerateButton() {
  const hasPerson = Boolean(state.selectedPersonImageId || state.personSourceTryonId);
  const hasGarment = Object.values(state.selectedGarments).some(Boolean);
  $("#generateTryon").disabled = !(hasPerson && hasGarment);
  $("#generateRequirement").textContent = hasGarment && hasPerson ? "衣服和形象照已就绪。" : !hasGarment && !hasPerson ? "还需选择衣服和全身正面形象照。" : !hasGarment ? "还需选择至少一件衣服。" : "还需选择一张全身正面形象照。";
}

function authorizationExpired(pending) {
  if (!pending?.expires_at) return false;
  const raw = pending.expires_at;
  const expires = typeof raw === "number" ? raw * (raw < 1e12 ? 1000 : 1) : Date.parse(raw);
  return Number.isFinite(expires) && expires <= Date.now();
}

function generationInput(quality, action) {
  if (action === "plus") return { source_tryon_id: state.currentTryonId, quality: "plus", action: "plus" };
  return {
    quality,
    person_image_id: state.selectedPersonImageId || "",
    source_tryon_id: state.personSourceTryonId || "",
    draft_look_id: state.outfitSource === "upload" ? state.draftLook?.id || "" : "",
    ...Object.fromEntries(Object.entries(state.selectedGarments).map(([type, id]) => [`${type}_id`, id || ""])),
  };
}

async function openGenerationConfirm(quality = "basic", action = "new") {
  if (state.pendingGeneration?.submissionAttempted) { showPendingSubmission(); return; }
  if (action === "new" && (!Object.values(state.selectedGarments).some(Boolean) || !(state.selectedPersonImageId || state.personSourceTryonId))) return;
  const selected = selectedOutfitItems().map(({ item }) => item);
  const current = state.tryons.find((item) => item.id === state.currentTryonId);
  if (action === "plus" && !current) return;
  const cards = action === "plus" ? [current?.dress_url ? { crop_url: current.dress_url, label_zh: current.dress_label } : null, current?.upper_url ? { crop_url: current.upper_url, label_zh: current.upper_label } : null, current?.lower_url ? { crop_url: current.lower_url, label_zh: current.lower_label } : null].filter(Boolean) : selected;
  const input = generationInput(quality, action);
  const signature = JSON.stringify(input);
  const existing = state.pendingGeneration;
  const pending = existing?.signature === signature && existing.authorization_id && !authorizationExpired(existing) ? existing : { quality, action, signature };
  state.pendingGeneration = pending;
  $("#generationSummary").innerHTML = `<div class="confirm-person"><img src="${escapeHtml(action === "plus" ? current.person_url : selectedPersonUrl())}" alt=""><span>全身正面形象照</span></div><div class="confirm-plus">搭配</div><div class="confirm-garments">${cards.map((item) => `<div><img src="${escapeHtml(item.crop_url)}" alt=""><span>${escapeHtml(item.label_zh || "单品")}</span></div>`).join("")}</div>`;
  $("#generationPrice").textContent = "正在查询费用…";
  $("#generationError").hidden = true;
  $("#generationBudget").textContent = "本次只生成 1 张，费用由服务端核对。";
  $("#confirmGeneration").disabled = true;
  $("#confirmGeneration").textContent = quality === "plus" ? "确认生成高质量试穿" : "确认生成基础试穿";
  if (!$("#generationDialog").open) openDialog($("#generationDialog"));
  try {
    const usage = await loadUsage();
    if (usage.operations?.[quality === "plus" ? "tryon_plus" : "tryon_basic"] == null) throw new Error("这项试穿价格尚未配置，请先由服务端设置费用上限。");
    if (!pending.authorization_id) {
      const form = new FormData();
      Object.entries(input).forEach(([name, value]) => { if (value) form.append(name, value); });
      const quote = await api("/api/tryon-authorizations", { method: "POST", body: form });
      if (!quote.authorization_id || !quote.request_id || typeof quote.estimated_cents !== "number" || typeof quote.max_cents !== "number") throw new Error("费用确认信息不完整，请稍后重新查看。");
      Object.assign(pending, quote);
    }
    if (state.pendingGeneration !== pending) return;
    saveWork();
    $("#generationPrice").textContent = `${qualityNames[quality]} · 约 ${money(pending.estimated_cents)}`;
    $("#generationBudget").textContent = `本次上限 ${money(pending.max_cents)}；个人可用 ${money(usage.remaining_cents)}，服务总可用 ${money(usage.global_remaining_cents)}。`;
    $("#confirmGeneration").disabled = false;
  } catch (error) {
    if (state.pendingGeneration !== pending) return;
    $("#generationPrice").textContent = "暂时无法确认费用";
    $("#generationError").textContent = error.message;
    $("#generationError").hidden = false;
  }
}

async function confirmGeneration() {
  const pending = state.pendingGeneration;
  if (!pending?.authorization_id || !pending.request_id || $("#confirmGeneration").disabled) return;
  if (!pending.submissionAttempted && authorizationExpired(pending)) {
    $("#generationError").textContent = "本次确认已过期，请返回后重新查看费用。";
    $("#generationError").hidden = false;
    $("#confirmGeneration").disabled = true;
    return;
  }
  $("#confirmGeneration").disabled = true;
  closeDialog($("#generationDialog"));
  await submitAuthorizedTryon();
}

async function retryPendingSubmission() {
  if (!state.pendingGeneration?.submissionAttempted) return;
  await submitAuthorizedTryon();
}

let submissionInFlight = false;
async function submitAuthorizedTryon() {
  const pending = state.pendingGeneration;
  if (!pending?.authorization_id || !pending.request_id || submissionInFlight) return;
  submissionInFlight = true;
  const ownerKey = state.ownerKey;
  pending.submissionAttempted = true;
  saveWork();
  switchView("tryon");
  showTryonProgress(true, { stage: "queued", progress: 2 });
  let created;
  try {
    // This host-only confirmation happens only after an explicit user click.
    // Repeating it after a lost response preserves the same authorization.
    await api(`/api/tryon-authorizations/${encodeURIComponent(pending.authorization_id)}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: pending.request_id }) });
    created = await api("/api/tryons", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ authorization_id: pending.authorization_id, request_id: pending.request_id }) });
    if (!created.tryon_id || !created.job_id) throw new Error("暂时没有收到完整任务信息。");
  } catch (error) {
    if (state.ownerKey !== ownerKey) return;
    if (error.status >= 400 && error.status < 500 && error.code !== "auth_required") {
      state.pendingGeneration = null;
      saveWork();
      showTryonError(`${error.message} 衣服和形象照会保留；调整后可重新查看费用。`);
    } else { saveWork(); showPendingSubmission(); }
    toast(error.message, true);
    return;
  } finally { submissionInFlight = false; }
  if (state.ownerKey !== ownerKey) return;
  state.pendingGeneration = null;
  state.currentTryonId = created.tryon_id;
  trackJob(created.job_id, "tryon", created.tryon_id);
  try {
    await loadAuthStatus();
    await loadUsage();
  } catch (_) { /* The generated task remains available if a status refresh fails. */ }
  try { await pollJob(created.job_id, "tryon", created.tryon_id); }
  catch (error) {
    const tracked = state.trackedJobs[created.job_id];
    showTryonError(error.message, { status: tracked?.status || "running", jobId: created.job_id, canResume: tracked?.canResume });
    toast(error.message, true);
  }
}

async function revealTryonResult(item) {
  const image = $("#resultImage");
  try { await image.decode(); } catch (_) { /* reveal the state even if decoding fails */ }
  if (state.currentTryonId !== item.id) return;
  const outgoing = [$("#tryonProgress"), $("#tryonError"), $("#tryonEmpty")].find((panel) => !panel.hidden);
  if (outgoing) await playMotion(outgoing, [{ opacity: 1 }, { opacity: 0 }], [{ opacity: 1 }, { opacity: 0 }], 180);
  setTryonStep("result");
  const result = $("#tryonResult");
  await playMotion(result, [{ opacity: 0, transform: "translateY(8px) scale(.99)" }, { opacity: 1, transform: "translateY(0) scale(1)" }], [{ opacity: 0 }, { opacity: 1 }], 280);
}

function showTryon(item) {
  if (!item) return;
  if (item.status !== "succeeded" || !item.result_url) {
    state.currentTryonId = item.id;
    if (item.job_id) {
      trackJob(item.job_id, "tryon", item.id, item);
      void recoverTrackedJob(item.job_id);
    } else showTryonError(item.error_message || "这条试穿还没有可查看的结果。");
    return;
  }
  state.currentTryonId = item.id;
  $("#resultPersonImage").src = item.person_url;
  $("#resultImage").src = item.result_url;
  $("#qualityTag").textContent = qualityNames[item.quality] || item.quality;
  setResultMode("result");
  $("#downloadResult").href = item.result_url;
  $("#downloadResult").setAttribute("download", `vestiai-${item.id.slice(0, 8)}.png`);
  $("#retryPlus").hidden = item.quality === "plus" || state.auth?.role === "visitor";
  $(".result-more").open = false;
  const saved = state.looks.some((look) => look.source_tryon_id === item.id);
  $("#saveLook").disabled = saved;
  $("#saveLook").textContent = saved ? "已保存到穿搭库" : "保存到穿搭库";
  void revealTryonResult(item);
}

function setResultMode(mode) {
  state.resultMode = mode === "compare" ? "compare" : "result";
  $("#resultMedia").classList.toggle("compare", state.resultMode === "compare");
  setActiveButtons("[data-result-mode]", document.querySelector(`[data-result-mode="${state.resultMode}"]`));
  $("#resultOriginalToggle").setAttribute("aria-label", state.resultMode === "compare" ? "只查看试穿结果" : "打开前后对比");
}

function renderTryonHistory() {
  const container = $("#tryonHistory");
  if (!state.tryons.length) { container.innerHTML = '<p class="picker-empty">还没有试穿结果。</p>'; return; }
  container.innerHTML = state.tryons.map((item, index) => `<button class="tryon-thumb${item.status !== "succeeded" ? " pending" : ""}" data-tryon-id="${item.id}" aria-label="查看第 ${index + 1} 次试穿${item.status === "succeeded" ? "结果" : "状态"}"><img src="${item.result_thumbnail_url || item.result_url || item.person_url}" loading="lazy" decoding="async" alt=""><span>${escapeHtml(item.status === "succeeded" ? qualityNames[item.quality] || item.quality : taskStatusNames[item.status] || "处理中")}</span>${item.status !== "succeeded" ? '<strong class="tryon-status-link">查看状态</strong>' : ""}</button>`).join("");
  container.querySelectorAll("[data-tryon-id]").forEach((button) => button.addEventListener("click", () => showTryon(state.tryons.find((item) => item.id === button.dataset.tryonId))));
}

async function saveCurrentTryonAsLook() {
  if (!state.currentTryonId) return;
  try {
    const look = await api(`/api/tryons/${state.currentTryonId}/save-look`, { method: "POST" });
    state.draftLook = null;
    state.outfitSource = "wardrobe";
    await loadData();
    state.selectedLookId = look.id;
    showTryon(state.tryons.find((item) => item.id === state.currentTryonId));
    toast("试穿结果已保存到穿搭库");
  } catch (error) { toast(error.message, true); }
}

function reuseCurrentTryon(keepPerson) {
  const item = state.tryons.find((tryon) => tryon.id === state.currentTryonId);
  if (!item) return;
  state.selectedGarments = { upper: null, lower: null, dress: null };
  const draftItems = state.draftLook?.garments || [];
  const ids = [item.upper_id, item.lower_id, item.dress_id].filter(Boolean);
  state.outfitSource = ids.some((id) => draftItems.some((garment) => garment.id === id)) ? "upload" : "wardrobe";
  ids.forEach((id) => {
    const garment = state.garments.find((candidate) => candidate.id === id) || draftItems.find((candidate) => candidate.id === id);
    if (garment) state.selectedGarments[garment.type] = garment.id;
  });
  state.selectedPersonImageId = null;
  state.personSourceTryonId = keepPerson ? item.id : null;
  renderPersonLibrary();
  renderTryonPicker();
  switchView("tryon");
  setTryonStep(keepPerson ? "compose" : "person");
  toast(keepPerson ? "形象照已保留，请换一件衣服" : "衣服已保留，请选择另一张形象照");
}

function viewCurrentTryonGarments() {
  const item = state.tryons.find((tryon) => tryon.id === state.currentTryonId);
  if (!item) return;
  const ids = [item.upper_id, item.lower_id, item.dress_id].filter((id) => id && state.garments.some((garment) => garment.id === id));
  if (!ids.length) { toast("所用单品已从衣橱删除，试穿结果仍然保留"); return; }
  showGarmentScope(ids, "当前试穿使用的单品");
}

async function deleteSelectedLook() {
  const look = selectedLook();
  if (!look || !confirm(`删除“${lookDisplayTitle(look)}”？已进入衣橱的单品和历史试穿结果都会保留。`)) return;
  try { await api(`/api/looks/${look.id}`, { method: "DELETE" }); state.selectedLookId = null; await loadData(); toast("穿搭已删除，衣橱单品和试穿结果已保留"); }
  catch (error) { toast(error.message, true); }
}

async function deleteGarment(garmentId) {
  const garment = state.garments.find((item) => item.id === garmentId);
  if (!garment || !confirm(`删除“${garment.label_zh}”？历史试穿图会保留，但之后不能再使用这件单品生成。`)) return;
  try { await api(`/api/garments/${garmentId}`, { method: "DELETE" }); closeDialog($("#garmentDialog")); state.selectedGarmentId = null; await loadData(); toast("单品已删除，历史试穿结果已保留"); }
  catch (error) { toast(error.message, true); }
}

function initTilt() {
  const card = $("#tiltCard");
  let dragging = false;
  let origin = null;
  let rotation = { x: 0, y: 0 };
  const paint = () => { card.style.transform = `rotateX(${rotation.x}deg) rotateY(${rotation.y}deg)`; };
  card.addEventListener("pointerdown", (event) => { dragging = true; origin = { x: event.clientX, y: event.clientY, rx: rotation.x, ry: rotation.y }; card.setPointerCapture(event.pointerId); });
  card.addEventListener("pointermove", (event) => { if (!dragging) return; rotation.y = Math.max(-15, Math.min(15, origin.ry + (event.clientX - origin.x) / 12)); rotation.x = Math.max(-15, Math.min(15, origin.rx - (event.clientY - origin.y) / 12)); paint(); });
  card.addEventListener("pointerup", () => { dragging = false; });
  card.addEventListener("dblclick", () => { rotation = { x: 0, y: 0 }; paint(); });
  card.addEventListener("keydown", (event) => { const changes = { ArrowLeft: [0, -2], ArrowRight: [0, 2], ArrowUp: [2, 0], ArrowDown: [-2, 0] }[event.key]; if (!changes) return; event.preventDefault(); rotation.x = Math.max(-15, Math.min(15, rotation.x + changes[0])); rotation.y = Math.max(-15, Math.min(15, rotation.y + changes[1])); paint(); });
}

document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
$(".view-tabs").addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = [...document.querySelectorAll(".tab")];
  const current = tabs.indexOf(document.activeElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next].focus();
  switchView(tabs[next].dataset.view);
});
document.querySelectorAll("[data-look-filter]").forEach((button) => button.addEventListener("click", () => { state.lookFilter = button.dataset.lookFilter; renderLooks(); }));
document.querySelectorAll("[data-wardrobe-type]").forEach((button) => button.addEventListener("click", () => { state.wardrobeFilter.type = button.dataset.wardrobeType; renderWardrobeGrid(); }));
document.querySelectorAll("[data-wardrobe-filter]").forEach((button) => button.addEventListener("click", () => { state.wardrobeFilter.filter = button.dataset.wardrobeFilter; $("#wardrobeMoreFilters").open = false; renderWardrobeGrid(); }));
document.querySelectorAll("[data-tryon-type]").forEach((button) => button.addEventListener("click", () => { state.tryonFilter.type = button.dataset.tryonType; state.tryonFilter.page = 1; renderTryonPicker(); }));
document.querySelectorAll("[data-tryon-filter]").forEach((button) => button.addEventListener("click", () => { state.tryonFilter.filter = button.dataset.tryonFilter; state.tryonFilter.page = 1; $("#tryonMoreFilters").open = false; renderTryonPicker(); }));
document.querySelectorAll("[data-outfit-source]").forEach((button) => button.addEventListener("click", () => setOutfitSource(button.dataset.outfitSource)));
document.querySelectorAll("[data-result-mode]").forEach((button) => button.addEventListener("click", () => setResultMode(button.dataset.resultMode)));
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => closeDialog(button.closest("dialog"))));
document.querySelectorAll(".app-dialog").forEach((dialog) => dialog.addEventListener("cancel", (event) => { event.preventDefault(); closeDialog(dialog); }));
bindFileButtons();
$("#lookInput").addEventListener("change", (event) => uploadLook(event.target.files[0]));
$("#garmentInput").addEventListener("change", (event) => uploadGarment(event.target.files[0]));
$("#garmentSearch").addEventListener("input", (event) => { state.wardrobeFilter.search = event.target.value.trim(); renderWardrobeGrid(); });
$("#garmentSort").addEventListener("change", (event) => { state.wardrobeFilter.sort = event.target.value; renderWardrobeGrid(); });
$("#clearWardrobeFilters").addEventListener("click", clearWardrobeFilters);
$("#personInput").addEventListener("change", (event) => uploadPersonImage(event.target.files[0]));
$("#draftOutfitInput").addEventListener("change", (event) => uploadDraftOutfit(event.target.files[0]));
$("#tryonSearch").addEventListener("input", (event) => { state.tryonFilter.search = event.target.value.trim(); state.tryonFilter.page = 1; renderTryonPicker(); });
$("#tryonSort").addEventListener("change", (event) => { state.tryonFilter.sort = event.target.value; state.tryonFilter.page = 1; renderTryonPicker(); });
$("#saveOutfit").addEventListener("click", saveSelectedOutfit);
$("#goTryon").addEventListener("click", () => setTryonStep("person"));
$("#modifyOutfit").addEventListener("click", () => setTryonStep("compose"));
$("#generateTryon").addEventListener("click", () => openGenerationConfirm("basic", "new"));
$("#confirmGeneration").addEventListener("click", confirmGeneration);
$("#retryPlus").addEventListener("click", () => openGenerationConfirm("plus", "plus"));
$("#saveLook").addEventListener("click", saveCurrentTryonAsLook);
$("#retryPerson").addEventListener("click", () => reuseCurrentTryon(false));
$("#reusePerson").addEventListener("click", () => reuseCurrentTryon(true));
$("#viewTryonGarments").addEventListener("click", viewCurrentTryonGarments);
$("#resultOriginalToggle").addEventListener("click", () => setResultMode(state.resultMode === "compare" ? "result" : "compare"));
$("#dismissTryonError").addEventListener("click", () => setTryonStep("person"));
$("#resumeTryonJob").addEventListener("click", () => recoverTrackedJob($("#resumeTryonJob").dataset.jobId));
$("#retryPendingSubmission").addEventListener("click", retryPendingSubmission);
$("#toastClose").addEventListener("click", () => { clearTimeout(toastTimer); $("#toast").className = "toast"; });
$("#favoriteLook").addEventListener("click", toggleLookFavorite);
$("#deleteLook").addEventListener("click", deleteSelectedLook);
$("#garmentForm").addEventListener("submit", saveGarmentDetails);
$("#deleteGarment").addEventListener("click", () => deleteGarment(state.selectedGarmentId));
$("#reparseGarment").addEventListener("click", reparseSelectedGarment);
$("#dialogTryonGarment").addEventListener("click", () => useGarmentForTryon(state.selectedGarmentId));
$("#pickerPrev").addEventListener("click", () => { state.tryonFilter.page -= 1; renderTryonPicker(); });
$("#pickerNext").addEventListener("click", () => { state.tryonFilter.page += 1; renderTryonPicker(); });
$("#settingsButton").addEventListener("click", openSettings);
$("#settingsForm").addEventListener("submit", saveSettings);
$("#accessForm").addEventListener("submit", login);
$("#accountButton").addEventListener("click", logout);
initTilt();
Promise.all([loadHealth(), loadAuthStatus()]).then(async ([, auth]) => {
  if (auth.authenticated) {
    await loadData();
    await loadUsage();
    await recoverSavedWork();
  }
}).catch((error) => toast(error.message, true));
