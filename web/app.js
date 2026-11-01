const state = {
  courses: [],
  selected: new Set(),
  summary: null,
  action: null,
  sawActiveAction: false,
  lastNotifiedState: null,
  processOpen: false,
  processSignature: "",
  llm: null,
};

const elements = {
  connectionStatus: document.querySelector("#connectionStatus"),
  refreshButton: document.querySelector("#refreshButton"),
  compactRefresh: document.querySelector("#compactRefresh"),
  heroSyncButton: document.querySelector("#heroSyncButton"),
  emptySyncButton: document.querySelector("#emptySyncButton"),
  syncButton: document.querySelector("#syncButton"),
  switchAccountButton: document.querySelector("#switchAccountButton"),
  versionLabel: document.querySelector("#versionLabel"),
  pythonLabel: document.querySelector("#pythonLabel"),
  heroCourseCount: document.querySelector("#heroCourseCount"),
  heroRing: document.querySelector("#heroRing"),
  heroRingText: document.querySelector("#heroRingText"),
  heroCourses: document.querySelector("#heroCourses"),
  lastSyncLabel: document.querySelector("#lastSyncLabel"),
  statCourses: document.querySelector("#statCourses"),
  statCoursesHint: document.querySelector("#statCoursesHint"),
  statLogin: document.querySelector("#statLogin"),
  statCookies: document.querySelector("#statCookies"),
  statView: document.querySelector("#statView"),
  statBars: document.querySelector("#statBars"),
  statActivity: document.querySelector("#statActivity"),
  statActivityHint: document.querySelector("#statActivityHint"),
  courseList: document.querySelector("#courseList"),
  courseEmpty: document.querySelector("#courseEmpty"),
  courseSearch: document.querySelector("#courseSearch"),
  selectAllButton: document.querySelector("#selectAllButton"),
  taskForm: document.querySelector("#taskForm"),
  taskStateBadge: document.querySelector("#taskStateBadge"),
  selectedSummary: document.querySelector("#selectedSummary"),
  selectedCount: document.querySelector("#selectedCount"),
  speedInput: document.querySelector("#speedInput"),
  limitInput: document.querySelector("#limitInput"),
  thresholdInput: document.querySelector("#thresholdInput"),
  manualCourses: document.querySelector("#manualCourses"),
  homeworkPanel: document.querySelector("#homeworkPanel"),
  homeworkList: document.querySelector("#homeworkList"),
  homeworkScanButton: document.querySelector("#homeworkScanButton"),
  llmSettings: document.querySelector("#llmSettings"),
  llmStatus: document.querySelector("#llmStatus"),
  llmProvider: document.querySelector("#llmProvider"),
  llmBase: document.querySelector("#llmBase"),
  llmModel: document.querySelector("#llmModel"),
  llmKey: document.querySelector("#llmKey"),
  llmSaveButton: document.querySelector("#llmSaveButton"),
  llmTestButton: document.querySelector("#llmTestButton"),
  llmClearButton: document.querySelector("#llmClearButton"),
  llmFeedback: document.querySelector("#llmFeedback"),
  homeworkWait: document.querySelector("#homeworkWait"),
  homeworkSubmit: document.querySelector("#homeworkSubmit"),
  homeworkHint: document.querySelector("#homeworkHint"),
  courseFields: document.querySelector("#courseFields"),
  startTaskLabel: document.querySelector("#startTaskLabel"),
  startTaskButton: document.querySelector("#startTaskButton"),
  stopTaskButton: document.querySelector("#stopTaskButton"),
  qrPanel: document.querySelector("#qrPanel"),
  qrImage: document.querySelector("#qrImage"),
  actionStatus: document.querySelector("#actionStatus"),
  actionStatusTitle: document.querySelector("#actionStatusTitle"),
  actionStatusMessage: document.querySelector("#actionStatusMessage"),
  actionStatusTime: document.querySelector("#actionStatusTime"),
  actionProgressBar: document.querySelector("#actionProgressBar"),
  actionProgressText: document.querySelector("#actionProgressText"),
  actionCaptchaHint: document.querySelector("#actionCaptchaHint"),
  processButton: document.querySelector("#processButton"),
  processOverlay: document.querySelector("#processOverlay"),
  processBackdrop: document.querySelector("#processBackdrop"),
  processCloseButton: document.querySelector("#processCloseButton"),
  processStateLabel: document.querySelector("#processStateLabel"),
  processStateMessage: document.querySelector("#processStateMessage"),
  processUpdatedAt: document.querySelector("#processUpdatedAt"),
  processLogCount: document.querySelector("#processLogCount"),
  processLogList: document.querySelector("#processLogList"),
  processEmpty: document.querySelector("#processEmpty"),
  activityList: document.querySelector("#activityList"),
  activityEmpty: document.querySelector("#activityEmpty"),
  quitButton: document.querySelector("#quitButton"),
  safetyNoteText: document.querySelector("#safetyNoteText"),
  toast: document.querySelector("#toast"),
  toastMessage: document.querySelector("#toastMessage"),
  footerYear: document.querySelector("#footerYear"),
};

const stateLabels = {
  idle: "等待操作",
  starting: "正在启动",
  waiting_login: "等待扫码",
  syncing: "同步课程",
  fetching: "读取课程",
  running: "学习中",
  captcha: "需要验证码",
  manual: "等待检查并提交",
  stopping: "正在停止",
  done: "已完成",
  failed: "需要处理",
  stopped: "已停止",
};

let toastTimer = 0;
let lastQrVersion = "";

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    headers: { Accept: "application/json", ...(options.headers || {}) },
    cache: "no-store",
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || payload.message || `请求失败 (${response.status})`);
  }
  return payload;
}

async function postJson(path, payload = {}) {
  return requestJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  elements.toastMessage.textContent = message;
  elements.toast.classList.add("is-visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 2600);
}

function formatDate(value) {
  if (!value) return "尚未同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function relativeTime(value) {
  if (!value) return "暂无记录";
  const difference = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(difference) || difference < 0) return formatDate(value);
  const minutes = Math.floor(difference / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

function taskMode() {
  return elements.taskForm.querySelector('input[name="taskMode"]:checked')?.value || "all";
}

function taskKind() {
  return elements.taskForm.querySelector('input[name="taskKind"]:checked')?.value || "course";
}

function applyTaskKind() {
  const homeworkMode = taskKind() === "homework";
  elements.homeworkPanel.hidden = !homeworkMode;
  elements.courseFields.hidden = homeworkMode;
  elements.startTaskLabel.textContent = homeworkMode ? "开始答题" : "开始学习";
  elements.safetyNoteText.textContent = homeworkMode
    ? "开始后会弹出浏览器窗口：验证码由你点选，其余由程序完成。"
    : state.summary?.desktop
      ? "任务由桌面程序管理；关闭窗口会结束程序并停止正在运行的任务。"
      : "任务在后台运行；关闭页面不会中止任务，可随时回到此页查看。";
}

function selectedCourseIds() {
  return [...state.selected];
}

function updateSummary(summary) {
  state.summary = summary;
  const count = Number(summary.course_count) || 0;

  elements.versionLabel.textContent = summary.version === "unknown" ? "--" : `v${summary.version}`;
  elements.pythonLabel.textContent = summary.desktop
    ? "桌面版"
    : summary.packaged ? "便携版" : `Python ${summary.python}`;
  applyTaskKind();
  elements.heroCourseCount.textContent = String(count).padStart(2, "0");
  elements.heroRing.style.strokeDashoffset = String(count > 0 ? 0 : 333);
  elements.heroRingText.textContent = count > 0 ? "READY" : "00%";
  elements.lastSyncLabel.textContent = formatDate(summary.courses_updated_at);
  elements.statCourses.textContent = String(count).padStart(2, "0");
  const aiCount = Number(summary.ai_course_count) || 0;
  elements.statCoursesHint.textContent = !summary.course_list_ready
    ? "等待首次同步"
    : aiCount > 0
      ? "其中 " + aiCount + " 门 AI 课程可自动答题"
      : "课程清单已载入";
  elements.statLogin.textContent = summary.login_mode || "--";
  elements.statCookies.textContent = summary.cookies_ready ? "已保存，可直接同步" : "首次使用需要扫码";
  elements.statView.textContent = summary.tree_view ? "树状视图" : "简洁列表";
  elements.statBars.textContent = summary.progressbar_view ? "已启用进度条" : "进度条已关闭";
  elements.statActivity.textContent = relativeTime(summary.latest_activity_at);
  elements.statActivityHint.textContent = summary.latest_activity_at ? "最近一次任务记录" : "尚未发现运行记录";
  elements.footerYear.textContent = String(new Date().getFullYear());

  if (count > 0) {
    elements.heroCourses.innerHTML = state.courses.slice(0, 3).map((course, index) => `
      <div class="preview-course">
        <span class="course-number">${String(index + 1).padStart(2, "0")}</span>
        <span class="course-name">${escapeHtml(course.name)}</span>
        <span class="course-tag">ID ${escapeHtml(course.id)}</span>
      </div>
    `).join("");
  }
}

function courseInitial(name) {
  const cleaned = name.replace(/[\s《》“”"'·:：\-—]/g, "");
  return Array.from(cleaned || "课").slice(0, 1).join("");
}

function visibleCourses() {
  const query = elements.courseSearch.value.trim().toLowerCase();
  if (!query) return state.courses;
  return state.courses.filter((course) =>
    course.name.toLowerCase().includes(query) || course.id.toLowerCase().includes(query)
  );
}

function renderCourses() {
  const courses = visibleCourses();
  elements.courseList.querySelectorAll(".skeleton-row").forEach((node) => node.remove());

  if (!state.courses.length) {
    elements.courseList.innerHTML = "";
    elements.courseEmpty.hidden = false;
    updateCourseSelectionUi();
    return;
  }

  elements.courseEmpty.hidden = true;
  if (!courses.length) {
    elements.courseList.innerHTML = `
      <div class="empty-state">
        <h3>没有匹配的课程</h3>
        <p>换一个课程名称或 ID 试试。</p>
      </div>
    `;
    updateCourseSelectionUi();
    return;
  }

  elements.courseList.innerHTML = courses.map((course) => {
    const checked = state.selected.has(course.id) ? "checked" : "";
    return `
      <label class="course-row">
        <span class="course-check">
          <input type="checkbox" value="${escapeHtml(course.id)}" ${checked}>
          <span aria-hidden="true"></span>
        </span>
        <span class="course-avatar" aria-hidden="true">${escapeHtml(courseInitial(course.name))}</span>
        <span class="course-info">
          <strong>${escapeHtml(course.name)}</strong>
          <small>COURSE ID · ${escapeHtml(course.id)}</small>
        </span>
        <span class="course-type ${course.type === "ai" ? "is-ai" : ""}">${escapeHtml(course.label || "本地课程")}</span>
      </label>
    `;
  }).join("");

  updateCourseSelectionUi();
}

function updateCourseSelectionUi() {
  elements.selectedCount.textContent = `${state.selected.size} 已选`;
  elements.selectAllButton.textContent =
    state.courses.length && state.selected.size === state.courses.length ? "清空" : "全选";
  const count = new Set([...state.selected, ...manualCourseIds()]).size;
  elements.selectedSummary.querySelector("span").textContent = taskMode() === "all"
    ? "将自动运行本地清单中的全部课程。"
    : count ? `已指定 ${count} 门课程，开始后将依次学习。` : "请勾选课程，或填写课程 ID。";
}

function manualCourseIds() {
  return elements.manualCourses.value
    .split(/[\s,，]+/)
    .map((id) => id.trim())
    .filter(Boolean);
}

function compactAction() {
  return {
    course_ids: taskMode() === "selected"
      ? [...new Set([...state.selected, ...manualCourseIds()])]
      : [],
    speed: elements.speedInput.value || null,
    limit: elements.limitInput.value || 0,
    threshold: elements.thresholdInput.value || null,
  };
}

function setTaskControls(active) {
  const busy = Boolean(active);
  elements.syncButton.disabled = busy;
  elements.switchAccountButton.disabled = busy;
  elements.heroSyncButton.disabled = busy;
  elements.emptySyncButton.disabled = busy;
  elements.startTaskButton.disabled = busy;
  elements.stopTaskButton.hidden = !busy || state.action?.state === "stopping";
  elements.taskForm.querySelectorAll("input, select").forEach((control) => {
    control.disabled = busy;
  });
}

function renderActionStatus(status) {
  state.action = status;
  const currentState = status.state || "idle";
  const active = Boolean(status.active);
  const label = stateLabels[currentState] || currentState;
  const progress = Math.max(0, Math.min(100, Number(status.progress) || 0));

  elements.taskStateBadge.textContent = active ? "RUNNING" : label.toUpperCase();
  elements.taskStateBadge.classList.toggle("is-captcha", ["captcha", "manual"].includes(currentState));
  elements.taskStateBadge.classList.toggle("is-running", active);
  elements.actionStatus.dataset.state = currentState;
  elements.actionStatusTitle.textContent = label;
  elements.actionStatusMessage.textContent = status.message || "等待操作";
  elements.actionStatusTime.textContent = status.updated_at
    ? status.updated_at.slice(11, 19)
    : "";
  elements.actionProgressBar.style.width = `${progress}%`;
  elements.actionProgressText.textContent = `${progress}%`;

  // 验证码：单独提示，告诉用户去哪一门课/哪一章处理
  const captcha = currentState === "captcha";
  elements.actionCaptchaHint.hidden = !captcha;
  if (captcha) {
    elements.actionCaptchaHint.textContent =
      `请到智慧树网页完成验证码（位置见上方），然后回来重新开始。`;
  }
  elements.processStateLabel.textContent = label;
  elements.processStateMessage.textContent = status.message || "暂时没有运行任务";
  elements.processUpdatedAt.textContent = status.updated_at
    ? status.updated_at.slice(11, 19)
    : "";
  elements.processStateLabel.dataset.state = currentState;
  elements.processOverlay.dataset.state = currentState;
  setTaskControls(active);

  elements.qrPanel.hidden = !status.qr_available;
  if (status.qr_available) {
    const version = status.updated_at || "qr";
    if (version !== lastQrVersion) {
      elements.qrImage.src = `/api/qr?v=${encodeURIComponent(version)}`;
      lastQrVersion = version;
    }
  } else {
    lastQrVersion = "";
  }

  if (active) {
    state.sawActiveAction = true;
  } else if (state.sawActiveAction && ["done", "failed", "stopped", "manual", "captcha"].includes(currentState)) {
    state.sawActiveAction = false;
    loadDashboard();
    if (currentState === "done") showToast("任务已完成");
    if (currentState === "manual") showToast("答案已填写，请在保留的浏览器窗口检查并提交");
    if (currentState === "failed" && state.lastNotifiedState !== status.updated_at) {
      showToast(status.message || "任务失败，请查看记录");
    }
  }
  state.lastNotifiedState = status.updated_at;
}

function openProcess() {
  state.processOpen = true;
  elements.processOverlay.hidden = false;
  document.body.classList.add("process-open");
  loadProcessLogs();
  window.setTimeout(() => elements.processCloseButton.focus(), 0);
}

function closeProcess() {
  state.processOpen = false;
  elements.processOverlay.hidden = true;
  document.body.classList.remove("process-open");
  elements.processButton.focus();
}

function processLevelLabel(level) {
  if (level === "error") return "错误";
  if (level === "warn") return "警告";
  return "过程";
}

async function loadProcessLogs() {
  if (!state.processOpen) return;
  try {
    const payload = await requestJson("/api/process?limit=500");
    const entries = Array.isArray(payload.entries) ? payload.entries : [];
    elements.processLogCount.textContent = `${entries.length} 条记录`;
    elements.processEmpty.hidden = entries.length > 0;

    const signature = entries.map((entry) =>
      `${entry.time}:${entry.level}:${entry.message}`
    ).join("\n");
    if (signature === state.processSignature) return;
    state.processSignature = signature;

    const nearBottom = (() => {
      const list = elements.processLogList;
      return list.scrollHeight - list.scrollTop - list.clientHeight < 90;
    })();
    elements.processLogList.innerHTML = entries.map((entry) => {
      const level = ["warn", "error"].includes(entry.level) ? entry.level : "info";
      return `
        <article class="process-log-row" data-level="${level}">
          <time>${escapeHtml(entry.time.slice(11))}</time>
          <span class="process-log-level">${processLevelLabel(level)}</span>
          <p>${escapeHtml(entry.message)}</p>
        </article>
      `;
    }).join("");

    if (nearBottom && entries.length) {
      window.requestAnimationFrame(() => {
        elements.processLogList.scrollTop = elements.processLogList.scrollHeight;
      });
    }
  } catch (error) {
    elements.processLogCount.textContent = "读取失败";
    elements.processStateMessage.textContent = error.message || "无法读取运行日志";
  }
}

async function pollAction() {
  try {
    const status = await requestJson("/api/status");
    renderActionStatus(status);
  } catch {
    // The dashboard poll and connection indicator cover service availability.
  }
}

async function startSync(options = {}) {
  try {
    const response = await postJson("/api/action/sync", options);
    if (options.force_login) {
      homeworkState.items = [];
      homeworkState.selected.clear();
      renderHomeworkList();
    }
    state.sawActiveAction = true;
    renderActionStatus(response.status);
    showToast("正在准备登录二维码");
    window.setTimeout(pollAction, 500);
  } catch (error) {
    showToast(error.message || "无法开始同步");
  }
}

async function startTask() {
  if (taskKind() === "homework") {
    await startHomeworkTask();
    return;
  }
  const options = compactAction();
  if (taskMode() === "selected" && !options.course_ids.length) {
    showToast("请先勾选课程，或填写课程 ID");
    return;
  }
  if (!state.summary?.cookies_ready) {
    showToast("请先扫码登录并同步课程");
    return;
  }
  try {
    const response = await postJson("/api/action/run", options);
    state.sawActiveAction = true;
    renderActionStatus(response.status);
    showToast("学习任务已开始");
    window.setTimeout(pollAction, 500);
  } catch (error) {
    showToast(error.message || "无法开始任务");
  }
}

const homeworkState = { items: [], selected: new Set(), loading: false };

function renderHomeworkList() {
  if (homeworkState.loading) {
    elements.homeworkList.innerHTML = '<p class="hint-line">正在扫描作业，请稍候…</p>';
    return;
  }
  if (!homeworkState.items.length) {
    elements.homeworkList.innerHTML =
      '<p class="hint-line">没有可做的作业。若刚做完，可能是已提交且未留下重做次数。</p>';
    return;
  }
  elements.homeworkList.innerHTML = homeworkState.items.map((item) => {
    const checked = homeworkState.selected.has(item.key) ? "checked" : "";
    const redo = item.redo_left > 0 ? `可重做 ${item.redo_left} 次` : "不可重做";
    const score = item.score ? `得分 ${item.score}/${item.total_score}` : redo;
    return `
      <label class="hw-row">
        <input type="checkbox" value="${escapeHtml(item.key)}" ${checked}>
        <span class="hw-info">
          <strong>${escapeHtml(item.name)}</strong>
          <small>${escapeHtml(item.course)} · ${item.questions} 题 · ${escapeHtml(score)}</small>
          <small class="hw-deadline">截止 ${escapeHtml(item.deadline || "--")}</small>
        </span>
      </label>
    `;
  }).join("");
}

async function scanHomeworks() {
  if (homeworkState.loading) return;
  homeworkState.loading = true;
  elements.homeworkScanButton.disabled = true;
  renderHomeworkList();
  try {
    const payload = await requestJson("/api/homework?force=1");
    homeworkState.items = Array.isArray(payload.items) ? payload.items : [];
    const valid = new Set(homeworkState.items.map((item) => item.key));
    homeworkState.selected = new Set([...homeworkState.selected].filter((key) => valid.has(key)));
    showToast(`扫描到 ${homeworkState.items.length} 份可做作业`);
  } catch (error) {
    homeworkState.items = [];
    showToast(error.message || "扫描作业失败，请先扫码登录");
  } finally {
    homeworkState.loading = false;
    elements.homeworkScanButton.disabled = false;
    renderHomeworkList();
  }
}

const LLM_PRESETS = {
  deepseek: { base: "https://api.deepseek.com", model: "deepseek-chat" },
  openai: { base: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  moonshot: { base: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
  siliconflow: { base: "https://api.siliconflow.cn/v1", model: "deepseek-ai/DeepSeek-V3" },
  zhipu: { base: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
  dashscope: { base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  ollama: { base: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" },
  custom: { base: "", model: "" },
};

function renderLlmStatus(llm) {
  const configured = Boolean(llm?.has_key);
  if (elements.llmStatus) {
    elements.llmStatus.textContent = configured
      ? llm.key_source === "env"
        ? "已配置（环境变量 " + llm.env_var + "）"
        : "已配置 " + (llm.api_key_masked || "")
      : "未配置";
    elements.llmStatus.dataset.state = configured ? "ok" : "";
  }
  state.llm = llm || null;
  return configured;
}

async function loadLlmSettings() {
  try {
    const data = await requestJson("/api/settings");
    const llm = data.llm || {};
    if (elements.llmBase) elements.llmBase.value = llm.base_url || "";
    if (elements.llmModel) elements.llmModel.value = llm.model || "";
    if (elements.llmProvider) elements.llmProvider.value = llm.provider || "deepseek";
    if (elements.llmKey) {
      elements.llmKey.value = "";
      elements.llmKey.placeholder = llm.api_key_masked
        ? "已保存 " + llm.api_key_masked + "（留空则不修改）"
        : "sk-…";
    }
    renderLlmStatus(llm);
  } catch (error) {
    if (elements.llmStatus) elements.llmStatus.textContent = "读取失败";
  }
}

async function saveLlmSettings() {
  elements.llmSaveButton.disabled = true;
  try {
    const data = await postJson("/api/settings", {
      provider: elements.llmProvider.value,
      base_url: elements.llmBase.value.trim(),
      model: elements.llmModel.value.trim(),
      api_key: elements.llmKey.value.trim(),
    });
    elements.llmKey.value = "";
    renderLlmStatus(data.llm);
    elements.llmFeedback.textContent = "已保存到本机 config.json";
    elements.llmFeedback.dataset.state = "ok";
    showToast("答题模型已保存");
    await loadLlmSettings();
  } catch (error) {
    elements.llmFeedback.textContent = error.message || "保存失败";
    elements.llmFeedback.dataset.state = "error";
  } finally {
    elements.llmSaveButton.disabled = false;
  }
}

async function testLlmSettings() {
  elements.llmTestButton.disabled = true;
  elements.llmFeedback.textContent = "正在请求…";
  elements.llmFeedback.dataset.state = "";
  try {
    const data = await postJson("/api/settings/test", {
      base_url: elements.llmBase.value.trim(),
      model: elements.llmModel.value.trim(),
      api_key: elements.llmKey.value.trim(),
    });
    elements.llmFeedback.textContent =
      "连接成功 · " + data.model + " · " + data.seconds + "s";
    elements.llmFeedback.dataset.state = "ok";
  } catch (error) {
    elements.llmFeedback.textContent = error.message || "连接失败";
    elements.llmFeedback.dataset.state = "error";
  } finally {
    elements.llmTestButton.disabled = false;
  }
}

async function clearLlmKey() {
  if (!window.confirm("清除本机保存的 API Key？")) return;
  try {
    const data = await postJson("/api/settings", { clear_key: true });
    renderLlmStatus(data.llm);
    elements.llmFeedback.textContent = "已清除";
    elements.llmFeedback.dataset.state = "";
    await loadLlmSettings();
  } catch (error) {
    elements.llmFeedback.textContent = error.message || "清除失败";
    elements.llmFeedback.dataset.state = "error";
  }
}

async function startHomeworkTask() {
  const keys = [...homeworkState.selected];
  if (!keys.length) {
    showToast("请先扫描并勾选要做的作业");
    return;
  }
  if (!state.summary?.cookies_ready) {
    showToast("请先扫码登录");
    return;
  }
  if (!state.llm?.has_key) {
    elements.llmSettings.open = true;
    elements.llmSettings.scrollIntoView({ block: "center", behavior: "smooth" });
    showToast("还没配置答题模型，先在下面填 API Key");
    return;
  }

  elements.startTaskButton.disabled = true;
  try {
    const response = await postJson("/api/action/homework", {
      keys,
      submit: elements.homeworkSubmit.value === "1",
      wait: Number(elements.homeworkWait.value) || 300,
    });
    state.sawActiveAction = true;
    renderActionStatus(response.status);
    showToast("已启动，浏览器窗口马上弹出");
    window.setTimeout(pollAction, 800);
  } catch (error) {
    showToast(error.message || "无法启动作业答题");
  } finally {
    elements.startTaskButton.disabled = Boolean(state.action?.active);
  }
}

async function stopTask() {
  elements.stopTaskButton.disabled = true;
  try {
    await postJson("/api/action/stop");
    showToast("正在停止任务");
    await pollAction();
  } catch (error) {
    showToast(error.message || "无法停止任务");
  } finally {
    elements.stopTaskButton.disabled = false;
  }
}

function renderLogs(entries) {
  elements.activityList.querySelectorAll(".activity-skeleton").forEach((node) => node.remove());
  if (!entries.length) {
    elements.activityList.innerHTML = "";
    elements.activityEmpty.hidden = false;
    return;
  }

  elements.activityEmpty.hidden = true;
  elements.activityList.innerHTML = entries.slice(0, 6).map((entry) => {
    const level = ["warn", "error"].includes(entry.level) ? entry.level : "info";
    return `
      <div class="activity-row">
        <div>
          <span class="activity-dot ${level}" aria-hidden="true"></span>
          <p title="${escapeHtml(entry.message)}">${escapeHtml(entry.message)}</p>
        </div>
        <time datetime="${escapeHtml(entry.time)}">${escapeHtml(entry.time.slice(11))}</time>
      </div>
    `;
  }).join("");
}

function setConnection(online, text = online ? "本地已连接" : "连接失败") {
  elements.connectionStatus.classList.toggle("is-online", online);
  elements.connectionStatus.classList.toggle("is-offline", !online);
  elements.connectionStatus.querySelector("span:last-child").textContent = text;
}

function setLoading(loading) {
  [elements.refreshButton, elements.compactRefresh].forEach((button) => {
    button.disabled = loading;
    button.classList.toggle("is-loading", loading);
  });
}

async function loadDashboard({ announce = false } = {}) {
  setLoading(true);
  try {
    const [summary, coursePayload, logPayload, actionStatus] = await Promise.all([
      requestJson("/api/summary"),
      requestJson("/api/courses"),
      requestJson("/api/logs"),
      requestJson("/api/status"),
    ]);
    state.courses = Array.isArray(coursePayload.courses) ? coursePayload.courses : [];
    const availableIds = new Set(state.courses.map((course) => course.id));
    state.selected = new Set([...state.selected].filter((id) => availableIds.has(id)));
    renderCourses();
    updateSummary(summary);
    renderLogs(Array.isArray(logPayload.entries) ? logPayload.entries : []);
    renderActionStatus(actionStatus);
    setConnection(true);
    if (announce) showToast("数据已刷新");
  } catch (error) {
    setConnection(false);
    showToast(error.message || "无法连接本地应用");
    if (!state.summary) {
      elements.courseList.innerHTML = "";
      elements.courseEmpty.hidden = false;
    }
  } finally {
    setLoading(false);
  }
}

elements.taskForm.addEventListener("submit", (event) => {
  event.preventDefault();
  startTask();
});

elements.taskForm.addEventListener("change", (event) => {
  if (event.target.matches('input[name="taskKind"]')) {
    applyTaskKind();
  } else if (event.target.matches('input[name="taskMode"]')) {
    updateCourseSelectionUi();
  }
});

elements.courseList.addEventListener("change", (event) => {
  if (!event.target.matches('input[type="checkbox"]')) return;
  if (event.target.checked) {
    state.selected.add(event.target.value);
  } else {
    state.selected.delete(event.target.value);
  }
  elements.taskForm.querySelector('input[name="taskMode"][value="selected"]').checked = true;
  updateCourseSelectionUi();
});

elements.courseSearch.addEventListener("input", renderCourses);
elements.selectAllButton.addEventListener("click", () => {
  const allSelected = state.courses.length && state.selected.size === state.courses.length;
  state.selected = allSelected ? new Set() : new Set(state.courses.map((course) => course.id));
  elements.taskForm.querySelector('input[name="taskMode"][value="selected"]').checked = true;
  renderCourses();
});

elements.manualCourses.addEventListener("input", () => {
  if (elements.manualCourses.value.trim()) {
    elements.taskForm.querySelector('input[name="taskMode"][value="selected"]').checked = true;
  }
  updateCourseSelectionUi();
});

[elements.syncButton, elements.heroSyncButton, elements.emptySyncButton].forEach((button) => {
  button.addEventListener("click", () => startSync());
});

elements.switchAccountButton.addEventListener("click", () => {
  if (state.action?.active) return;
  if (!window.confirm("切换账号会退出当前登录，并重新生成登录二维码。\n当前账号的课程清单会被新账号覆盖，确定继续？")) {
    return;
  }
  startSync({ force_login: true });
});
elements.stopTaskButton.addEventListener("click", stopTask);
elements.homeworkScanButton.addEventListener("click", scanHomeworks);
elements.llmSaveButton.addEventListener("click", saveLlmSettings);
elements.llmTestButton.addEventListener("click", testLlmSettings);
elements.llmClearButton.addEventListener("click", clearLlmKey);
elements.llmProvider.addEventListener("change", () => {
  const preset = LLM_PRESETS[elements.llmProvider.value];
  if (!preset) return;
  if (preset.base) elements.llmBase.value = preset.base;
  if (preset.model) elements.llmModel.value = preset.model;
  elements.llmFeedback.textContent = "";
  elements.llmFeedback.dataset.state = "";
});
elements.homeworkList.addEventListener("change", (event) => {
  if (!event.target.matches('input[type="checkbox"]')) return;
  if (event.target.checked) {
    homeworkState.selected.add(event.target.value);
  } else {
    homeworkState.selected.delete(event.target.value);
  }
});
elements.processButton.addEventListener("click", openProcess);
elements.processCloseButton.addEventListener("click", closeProcess);
elements.processBackdrop.addEventListener("click", closeProcess);
elements.refreshButton.addEventListener("click", () => loadDashboard({ announce: true }));
elements.compactRefresh.addEventListener("click", () => loadDashboard({ announce: true }));

async function quitApp() {
  try {
    await postJson("/api/quit");
    showToast("程序已退出");
    window.setTimeout(() => window.close(), 350);
  } catch (error) {
    showToast(error.message || "无法退出程序");
  }
}

elements.quitButton.addEventListener("click", async () => {
  if (!window.confirm("确定退出程序？正在运行的学习任务也会停止。")) return;
  await quitApp();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.processOpen) closeProcess();
});

document.querySelectorAll(".main-nav a").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll(".main-nav a").forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  });
});

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    document.querySelectorAll(".main-nav a").forEach((link) => {
      link.classList.toggle("active", link.getAttribute("href") === `#${entry.target.id}`);
    });
  });
}, { rootMargin: "-30% 0px -60%", threshold: 0 });

["overview", "courses", "guide"].forEach((id) => {
  const section = document.getElementById(id);
  if (section) observer.observe(section);
});

elements.footerYear.textContent = String(new Date().getFullYear());
applyTaskKind();
renderHomeworkList();
loadLlmSettings();
loadDashboard();
window.setInterval(() => {
  pollAction();
  loadProcessLogs();
}, 1800);
