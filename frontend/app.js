/* jev-convert 前端逻辑：无依赖，全部用 fetch + 轮询。 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"]/g,
  (ch) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[ch]));

let currentTask = null;
let pollTimer = null;
let selectedToken = "";

// 流程阶段文案与 jev_convert/cli.py 的 _emit 标记一一对应（▶ 开始 / ✓ 完成 / - 跳过 / ✗ 失败）。
const FLOWS = {
  "compile": ["S0 读取导图", "S1 识别用例", "S2-S6 逐用例编译", "计划库写盘", "确认表导出"],
  "confirm": ["解析导图", "读回确认表", "更新计划", "沉淀与回写"],
  "kb-build": ["抽取路由与组件", "合并生成知识卡"],
};

function renderFlow(kind, log, status) {
  const flow = $("flow");
  const stages = FLOWS[kind];
  if (!stages) { flow.classList.add("hidden"); return; }
  flow.classList.remove("hidden");
  const started = {}, finished = {}, skipped = {}, failed = new Set();
  for (const line of (log || "").split("\n")) {
    const mark = line.match(/^([▶✓\-✗])\s(.+)$/);
    if (!mark) continue;
    const name = mark[2].split("（")[0].trim();
    const chip = stages.find((stage) => name === stage || name.startsWith(stage));
    if (!chip) continue;
    if (mark[1] === "▶") started[chip] = true;
    else if (mark[1] === "✓") finished[chip] = true;
    else if (mark[1] === "-") skipped[chip] = true;
    else failed.add(chip);
  }
  const startedStages = stages.filter((name) => started[name]);
  const lastStarted = startedStages.length ? startedStages[startedStages.length - 1] : null;
  flow.innerHTML = stages.map((name) => {
    let state = "pending";
    if (failed.has(name)) state = "failed";
    else if (finished[name]) state = "done";
    else if (skipped[name] && !started[name]) state = "skip";
    else if (started[name]) {
      if (name !== lastStarted) state = "done";          // 后续阶段已开始：隐式完成
      else if (status === "running") state = "active";
      else state = status === "done" ? "done" : "failed";
    }
    return `<span class="chip ${state}">${esc(name)}</span>`;
  }).join('<span class="arrow">→</span>');
}

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, data };
}

/* ---------- 总览 ---------- */
async function refreshOverview() {
  const { data } = await api("/api/overview");
  if (data.error) { $("ov-kb").textContent = "配置缺失"; return; }
  $("ov-kb").textContent = data.kb_version || "未构建";
  const totals = (data.plans || []).reduce(
    (acc, item) => {
      acc.mindnotes += 1;
      acc.total += item.total;
      acc.confirmed += item.counts.confirmed || 0;
      acc.draft += item.counts.draft || 0;
      acc.stale += item.counts.stale || 0;
      return acc;
    }, { mindnotes: 0, total: 0, confirmed: 0, draft: 0, stale: 0 });
  $("ov-plans").textContent = totals.total
    ? `${totals.total} 条 / ${totals.mindnotes} 份导图（confirmed ${totals.confirmed} · draft ${totals.draft} · stale ${totals.stale}）`
    : "空";
  const llm = data.llm || {};
  $("ov-cache").textContent = `${llm.cache_entries ?? 0} 条`;
  $("ov-hit").textContent = llm.hit_rate == null ? "—" : `${Math.round(llm.hit_rate * 100)}%`;
  renderPlanTokens(data.plans || []);
}

/* ---------- 任务 ---------- */
function submitTask(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify(body) });
}

async function startTask(path, body) {
  const { ok, data } = await submitTask(path, body);
  if (!ok || !data.task) { alert(data.error || "提交失败"); return; }
  currentTask = data.task;
  $("task-log").textContent = "任务已提交…";
  renderTaskTabs();
  pollTask();
}

async function renderTaskTabs() {
  const { data } = await api("/api/tasks");
  const list = Array.isArray(data) ? data : [];
  let needPoll = false;
  if (!currentTask && list.length) { currentTask = list[0].id; needPoll = true; }
  $("task-list").innerHTML = list.map((task) =>
    `<span class="task-tab ${task.id === currentTask ? "active" : ""} ${task.status === "running" ? "running" : ""}"
       data-id="${esc(task.id)}">${esc(task.id)} · ${esc(task.kind)}
      <span class="badge ${esc(task.status)}">${esc(task.status)}</span></span>`).join("");
  for (const node of $("task-list").querySelectorAll("[data-id]")) {
    node.addEventListener("click", () => {
      if (currentTask !== node.dataset.id) { currentTask = node.dataset.id; pollTask(); }
      else { renderTaskTabs(); }
    });
  }
  if (needPoll) pollTask();
}

async function pollTask() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (!currentTask) return;
  const { data } = await api(`/api/task/${currentTask}`);
  if (data.error) { $("task-log").textContent = data.error; $("flow").classList.add("hidden"); return; }
  $("task-log").textContent = data.log || "（运行中，尚无输出）";
  $("task-log").scrollTop = $("task-log").scrollHeight;
  renderFlow(data.kind, data.log, data.status);
  renderTaskTabs();
  if (data.status === "running") {
    pollTimer = setTimeout(pollTask, 1000);
  } else {
    refreshOverview();
    if (selectedToken) loadPlans(selectedToken);
  }
}

/* ---------- 计划库 ---------- */
function renderPlanTokens(plans) {
  const select = $("plan-token");
  const keep = selectedToken;
  select.innerHTML = plans.length
    ? plans.map((item) => `<option value="${esc(item.token)}">${esc(item.token)}（${item.total} 条）</option>`).join("")
    : '<option value="">（计划库为空）</option>';
  if (keep && plans.some((item) => item.token === keep)) select.value = keep;
  if (select.value && select.value !== selectedToken) {
    selectedToken = select.value;
    loadPlans(selectedToken);
  }
  if (!plans.length) {
    $("plan-table").querySelector("tbody").innerHTML =
      '<tr><td colspan="4" class="hint">编译一份导图后这里会出现计划</td></tr>';
    $("plan-detail").classList.add("hidden");
  }
}

async function loadPlans(token) {
  selectedToken = token;
  const { data } = await api(`/api/plans?token=${encodeURIComponent(token)}`);
  const rows = Object.entries(data || {}).sort(([a], [b]) => a.localeCompare(b));
  $("plan-summary").textContent = `${rows.length} 条`;
  $("plan-table").querySelector("tbody").innerHTML = rows.map(([caseId, item]) =>
    `<tr data-case="${esc(caseId)}">
      <td>${esc(caseId)}</td>
      <td><span class="badge ${esc(item.status)}">${esc(item.status)}</span></td>
      <td>${esc(item.node_id || "")}</td>
      <td>${esc(item.kb_version || "")}</td>
    </tr>`).join("") || '<tr><td colspan="4" class="hint">这份导图还没有计划</td></tr>';
  for (const tr of $("plan-table").querySelectorAll("tr[data-case]")) {
    tr.addEventListener("click", () => showPlan(tr.dataset.case));
  }
}

async function showPlan(caseId) {
  const { data } = await api(`/api/plan?token=${encodeURIComponent(selectedToken)}&case=${encodeURIComponent(caseId)}`);
  if (data.error) { alert(data.error); return; }
  for (const tr of $("plan-table").querySelectorAll("tr[data-case]")) {
    tr.classList.toggle("selected", tr.dataset.case === caseId);
  }
  $("plan-detail").classList.remove("hidden");
  $("detail-title").textContent = `${data.id} ${data.title}`;
  const entry = data.entry || {};
  $("detail-meta").textContent =
    `状态 ${data.status} · 入口 ${entry.node || "—"}（${entry.route || "—"}）· kb ${data.kb_version || "—"}`
    + (data.destructive ? " · 会改数据" : "");
  const steps = [...(data.steps || []), ...(data.teardown || []).map((step) => ({ ...step, teardown: true }))];
  $("detail-steps").innerHTML = steps.map((step) =>
    `<tr>
      <td>${esc(step.seq)}</td>
      <td>${esc(step.kind)}${step.teardown ? "（teardown）" : ""}</td>
      <td>${esc(step.page || "")}</td>
      <td>${esc(step.target && step.target.name ? step.target.name : "（待确认）")}</td>
      <td>${step.target ? esc(Number(step.target.confidence || 0).toFixed(2)) : ""}</td>
      <td>${esc((step.expect || []).join("；"))}</td>
    </tr>`).join("");
}

/* ---------- 表单 ---------- */
$("form-compile").addEventListener("submit", (event) => {
  event.preventDefault();
  startTask("/api/compile", { url: $("compile-url").value.trim(), until: $("compile-until").value });
});
$("form-confirm").addEventListener("submit", (event) => {
  event.preventDefault();
  startTask("/api/confirm", { url: $("confirm-url").value.trim() });
});
$("form-kb").addEventListener("submit", (event) => {
  event.preventDefault();
  startTask("/api/kb-build", { webui: $("kb-webui").value.trim() });
});
$("plan-token").addEventListener("change", () => loadPlans($("plan-token").value));

/* ---------- 启动 ---------- */
refreshOverview();
renderTaskTabs();
