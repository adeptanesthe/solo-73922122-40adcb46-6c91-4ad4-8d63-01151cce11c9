/* 技术编辑选题推荐系统前端（原生 JS）
 *
 * 双窗口草稿保护：
 * - 轮询（含慢重算期间）绝不覆盖本窗口未提交的兴趣/配额草稿；
 * - 表单提交携带“开始编辑时的条件版本”(base_version)，服务器原子核对；
 *   其他窗口已修改条件或重置队列时提交被 409 拒绝，草稿保留并展示新条件。
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { data: null, pollTimer: null };

/* 本窗口条件表单的草稿状态 */
const draft = {
  dirty: false, // 有未提交的兴趣/配额修改
  baseVersion: null, // 表单内容对应的条件版本（开始编辑时的版本）
  conflict: null, // 最近一次 409 冲突的 detail（含服务器当前条件）
};

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const d = body.detail;
    const err = new Error(
      typeof d === "string" ? d : (d && d.message) || `请求失败 (${resp.status})`
    );
    err.status = resp.status;
    err.detail = d;
    throw err;
  }
  return body;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatQuotas(quotas) {
  return state.data.topics.map((t) => `${t} ${quotas[t]}`).join(" · ");
}

function articleMap() {
  const m = new Map();
  state.data.articles.forEach((a) => m.set(a.id, a));
  return m;
}

/* ---------------- 草稿与版本 ---------------- */

function markDirty() {
  if (!state.data) return;
  draft.dirty = true;
  renderDraftBar();
}

/* 用服务器状态同步表单（仅在值变化时写 DOM，避免稳态轮询打断输入焦点） */
function syncFormFromServer() {
  const st = state.data.state;
  const interestEl = $("#interest");
  if (interestEl.value !== st.interest) interestEl.value = st.interest;
  const current = readQuotas();
  const same = state.data.topics.every((t) => current[t] === st.quotas[t]);
  if (!same) renderQuotas();
  draft.dirty = false;
  draft.baseVersion = st.condition_version;
  draft.conflict = null;
  renderDraftBar();
}

/* 草稿提示条：未提交修改 / 其他窗口已改条件 / 提交被拒冲突 */
function renderDraftBar() {
  const bar = $("#draft-bar");
  if (!state.data) {
    bar.className = "draft-bar hidden";
    bar.innerHTML = "";
    return;
  }
  const st = state.data.state;
  const serverVersion = st.condition_version;
  const staleByOther =
    draft.dirty && draft.baseVersion !== null && serverVersion !== draft.baseVersion;

  const serverSummary = `
    <div class="db-server"><b>服务器当前兴趣：</b>${escapeHtml(st.interest)}</div>
    <div class="db-server"><b>服务器当前配额：</b>${escapeHtml(formatQuotas(st.quotas))}</div>`;
  const actions = `
    <div class="db-actions">
      <button id="btn-load-latest" class="btn" type="button">放弃草稿，载入最新条件</button>
      <button id="btn-keep-draft" class="btn" type="button">已查看新条件，继续编辑我的草稿</button>
    </div>`;

  if (draft.conflict) {
    bar.className = "draft-bar conflict";
    bar.innerHTML = `
      <div class="db-title">⚠ 提交被拒绝：你编辑期间条件已被其他窗口修改或队列已重置</div>
      <div class="db-body">草稿未生效、已保留在表单中（你的版本 v${draft.conflict.base_version}，
        当前版本 v${serverVersion}）。原条件、已揭示文章与重算任务均未受影响。</div>
      ${serverSummary}${actions}`;
  } else if (staleByOther) {
    bar.className = "draft-bar warn";
    bar.innerHTML = `
      <div class="db-title">⚠ 条件已被其他窗口更新（你的草稿基于 v${draft.baseVersion}，当前 v${serverVersion}）</div>
      <div class="db-body">草稿已保留，轮询不会覆盖；现在提交会被拒绝，请先查看最新条件。</div>
      ${serverSummary}${actions}`;
  } else if (draft.dirty) {
    bar.className = "draft-bar info";
    bar.innerHTML = `<div class="db-title">有未提交的修改（基于条件版本 v${draft.baseVersion}），轮询不会覆盖你的草稿。</div>`;
  } else {
    bar.className = "draft-bar hidden";
    bar.innerHTML = "";
    return;
  }

  const loadBtn = $("#btn-load-latest");
  if (loadBtn) {
    loadBtn.addEventListener("click", () => {
      syncFormFromServer();
      showMsg($("#condition-msg"), "已载入服务器最新条件", true);
    });
  }
  const keepBtn = $("#btn-keep-draft");
  if (keepBtn) {
    keepBtn.addEventListener("click", () => {
      // 用户已查看新条件并明确保留草稿：版本基线跟进，之后提交即生效
      draft.baseVersion = state.data.state.condition_version;
      draft.conflict = null;
      renderDraftBar();
      showMsg($("#condition-msg"), "已保留草稿并基于最新条件继续编辑", true);
    });
  }
}

/* 本窗口操作成功后统一接收响应状态。
 * prevVersion/expectedBump 用于判断操作期间是否有其他窗口介入：
 * 条件版本严格单调递增，差值等于本操作预期推进数 ⟺ 无其他窗口变更，
 * 此时本窗口草稿的版本基线可安全跟进，避免“自己与自己冲突”。 */
function adoptServerState(data, { syncForm = false, prevVersion = null, expectedBump = 0 } = {}) {
  state.data = data;
  const newVersion = state.data.state.condition_version;
  if (syncForm || !draft.dirty) {
    syncFormFromServer();
  } else if (
    prevVersion !== null &&
    draft.baseVersion === prevVersion &&
    newVersion - prevVersion === expectedBump
  ) {
    draft.baseVersion = newVersion;
  }
  renderDraftBar();
  renderQueue();
  schedulePoll();
}

/* ---------------- 渲染 ---------------- */

function renderQuotas() {
  const quotas = state.data.state.quotas;
  const grid = $("#quota-grid");
  grid.innerHTML = "";
  state.data.topics.forEach((topic) => {
    const wrap = document.createElement("div");
    wrap.className = "quota-item";
    wrap.innerHTML = `<label>${topic}</label>`;
    const input = document.createElement("input");
    input.type = "number";
    input.min = 0;
    input.max = 6;
    input.value = quotas[topic];
    input.dataset.topic = topic;
    input.addEventListener("input", () => {
      renderQuotaSum();
      markDirty();
    });
    wrap.appendChild(input);
    grid.appendChild(wrap);
  });
  renderQuotaSum();
}

function renderQuotaSum() {
  const sum = [...document.querySelectorAll("#quota-grid input")]
    .reduce((acc, el) => acc + (parseInt(el.value, 10) || 0), 0);
  const el = $("#quota-sum");
  el.innerHTML = `当前合计：<b>${sum}</b> / 6`;
  el.className = `quota-sum ${sum === 6 ? "valid" : "invalid"}`;
}

function renderExclusions() {
  const excluded = new Set(state.data.state.excluded_ids);
  const select = $("#exclude-select");
  const revealedIds = new Set(state.data.state.revealed.map((r) => r.article_id));
  select.innerHTML = "";
  state.data.articles.forEach((a) => {
    if (revealedIds.has(a.id)) return;
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = `#${a.id} ${a.title}（${a.topic}/${a.author}）${excluded.has(a.id) ? " — 已排除" : ""}`;
    if (excluded.has(a.id)) opt.disabled = true;
    select.appendChild(opt);
  });

  const list = $("#exclusion-list");
  list.innerHTML = "";
  if (excluded.size === 0) {
    list.innerHTML = `<span class="msg" style="color:#64748b">暂无排除文章</span>`;
    return;
  }
  const amap = articleMap();
  excluded.forEach((id) => {
    const a = amap.get(id);
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `#${id} ${a ? a.title : "未知"} <button title="取消排除" data-id="${id}">×</button>`;
    chip.querySelector("button").addEventListener("click", () => removeExclusion(id));
    list.appendChild(chip);
  });
}

function renderQueue() {
  const { state: st, plan, job } = state.data;
  const jobEl = $("#job-status");
  const revealedIds = new Set(st.revealed.map((r) => r.article_id));
  const amap = articleMap();

  if (job) {
    const label = {
      pending: "等待计算",
      running: "正在重算（慢重算期间仍可修改条件，迟到结果不会揭示旧推荐）",
      done: "重算完成",
      failed: `重算失败：${job.error || "未知错误"}（进度未推进）`,
      stale: "该结果已过期（条件已变更）",
    }[job.status] || job.status;
    jobEl.className = `job-status ${job.status}`;
    jobEl.textContent = `世代 ${job.generation} · ${label}`;
  }

  const queueEl = $("#queue");
  queueEl.innerHTML = "";
  const activePlan = plan && job.status === "done" && job.generation === st.generation ? plan : null;

  for (let pos = 1; pos <= 6; pos++) {
    const li = document.createElement("li");
    const revealedItem = st.revealed.find((r) => r.position === pos);
    if (revealedItem) {
      const a = amap.get(revealedItem.article_id);
      li.className = "revealed";
      li.innerHTML = `
        <span class="pos">${pos}</span>
        <div class="q-main">
          <div class="q-title">${a ? a.title : "文章 #" + revealedItem.article_id}</div>
          <div class="q-sub">
            <span class="tag">${revealedItem.topic}</span>
            <span class="tag author">${revealedItem.author}</span>
            已揭示 · 固定
          </div>
        </div>
        <div class="q-rel">相关度<br/><b>${revealedItem.relevance.toFixed(4)}</b><br/>权重 ${7 - pos}</div>`;
    } else if (activePlan && activePlan.feasible) {
      const item = activePlan.queue.find((q) => q.position === pos);
      li.className = "hidden-item";
      li.innerHTML = `
        <span class="pos">${pos}</span>
        <div class="q-main">
          <div class="q-title">未揭示位置 ${pos}（候选已锁定，等待揭示）</div>
          <div class="q-sub">主题：${item ? item.topic : "—"}</div>
        </div>
        <div class="q-rel">相关度<br/><b>${item ? item.relevance.toFixed(4) : "—"}</b><br/>权重 ${7 - pos}</div>`;
    } else {
      li.className = "hidden-item";
      const reason = activePlan && !activePlan.feasible ? activePlan.reason : "等待重算结果";
      li.innerHTML = `
        <span class="pos">${pos}</span>
        <div class="q-main"><div class="q-title">未揭示</div><div class="q-sub">${reason}</div></div>
        <div class="q-rel">权重 ${7 - pos}</div>`;
    }
    queueEl.appendChild(li);
  }

  $("#generation-line").textContent =
    `当前世代 ${st.generation} · 条件版本 v${st.condition_version} · 已揭示 ${st.revealed.length}/6 篇` +
    (activePlan && activePlan.feasible ? ` · 队列目标分 Σ(7−位置)×相关度 = ${activePlan.score.toFixed(4)}` : "");

  const revealBtn = $("#btn-reveal");
  const blocked = !activePlan || !activePlan.feasible || st.revealed.length >= 6;
  revealBtn.disabled = !!blocked;
  if (st.revealed.length >= 6) revealBtn.textContent = "队列已全部揭示";
  else revealBtn.textContent = "揭示下一组（两篇）";

  renderExclusions();
  renderArticleList();
}

function renderArticleList() {
  const filter = $("#article-filter").value.trim().toLowerCase();
  const excluded = new Set(state.data.state.excluded_ids);
  const revealedIds = new Set(state.data.state.revealed.map((r) => r.article_id));
  const list = $("#article-list");
  list.innerHTML = "";
  state.data.articles
    .filter((a) => {
      if (!filter) return true;
      return `${a.id} ${a.title} ${a.topic} ${a.author}`.toLowerCase().includes(filter);
    })
    .forEach((a) => {
      const card = document.createElement("div");
      card.className = `article-card ${excluded.has(a.id) ? "excluded" : ""}`;
      card.innerHTML = `
        <div class="t">#${a.id} ${a.title}</div>
        <div class="s">
          <span class="tag">${a.topic}</span>
          <span class="tag author">${a.author}</span>
          ${excluded.has(a.id) ? '<span class="tag excluded-tag">已排除</span>' : ""}
          ${revealedIds.has(a.id) ? '<span class="tag" style="background:#dcfce7;color:#166534">已揭示</span>' : ""}
        </div>`;
      card.addEventListener("click", () => openArticle(a.id));
      list.appendChild(card);
    });
}

async function openArticle(id) {
  const a = await api(`/api/articles/${id}`);
  $("#dlg-title").textContent = `#${a.id} ${a.title}`;
  $("#dlg-meta").innerHTML = `<span class="tag">${a.topic}</span><span class="tag author">${a.author}</span>`;
  $("#dlg-body").textContent = a.body;
  $("#article-dialog").showModal();
}

function readQuotas() {
  const quotas = {};
  document.querySelectorAll("#quota-grid input").forEach((el) => {
    quotas[el.dataset.topic] = parseInt(el.value, 10) || 0;
  });
  return quotas;
}

function showMsg(el, text, ok = false) {
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : ""}`;
  if (text) setTimeout(() => { el.textContent = ""; el.className = "msg"; }, 5000);
}

/* ---------------- 轮询 ---------------- */

async function refresh() {
  state.data = await api("/api/status");
  if (draft.dirty) {
    // 本窗口有未提交草稿：绝不覆盖表单；若服务器版本已被其他窗口
    // 推进，提示条会展示新条件摘要，提醒提交前查看。
    renderDraftBar();
  } else {
    syncFormFromServer();
  }
  renderQueue();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  const j = state.data.job;
  if (j && (j.status === "pending" || j.status === "running")) {
    state.pollTimer = setTimeout(refresh, 600);
  }
}

/* ---------------- 操作 ---------------- */

async function applyCondition() {
  const msg = $("#condition-msg");
  try {
    const payload = {
      interest: $("#interest").value,
      quotas: readQuotas(),
      compute_delay_ms: parseInt($("#delay").value, 10) || 0,
      base_version: draft.baseVersion, // 开始编辑时的条件版本，服务器原子核对
    };
    $("#btn-apply").disabled = true;
    const data = await api("/api/condition", { method: "PUT", body: JSON.stringify(payload) });
    adoptServerState(data, { syncForm: true });
    showMsg(msg, "条件已更新，新世代重算已开始", true);
  } catch (err) {
    if (err.status === 409 && err.detail && err.detail.code === "condition_conflict") {
      // 过期提交被拒绝：草稿保留在表单中，拉取最新状态并展示冲突条
      draft.conflict = err.detail;
      await refresh();
      showMsg(msg, "提交被拒绝：条件已被其他窗口修改，草稿已保留，请查看上方新条件");
    } else {
      showMsg(msg, err.message);
    }
  } finally {
    $("#btn-apply").disabled = false;
  }
}

async function revealNext() {
  const msg = $("#reveal-msg");
  try {
    const prevVersion = state.data ? state.data.state.condition_version : null;
    const data = await api("/api/reveal", { method: "POST" });
    adoptServerState(data, { prevVersion, expectedBump: 0 });
    const outcome = data.reveal_outcome;
    if (outcome.advanced) {
      showMsg(msg, `已揭示位置 ${outcome.new_positions.join("、")}`, true);
    } else {
      showMsg(msg, outcome.reason || "没有推进");
    }
  } catch (err) {
    showMsg(msg, err.message);
  }
}

async function addExclusion() {
  const msg = $("#condition-msg");
  const id = parseInt($("#exclude-select").value, 10);
  if (!id) return;
  try {
    const prevVersion = state.data ? state.data.state.condition_version : null;
    const data = await api("/api/exclusions", {
      method: "POST",
      body: JSON.stringify({ article_id: id, compute_delay_ms: parseInt($("#delay").value, 10) || 0 }),
    });
    adoptServerState(data, { prevVersion, expectedBump: 1 });
    showMsg(msg, `已排除文章 #${id}，触发新世代重算`, true);
  } catch (err) {
    showMsg(msg, err.message);
  }
}

async function removeExclusion(id) {
  const msg = $("#condition-msg");
  try {
    const prevVersion = state.data ? state.data.state.condition_version : null;
    const delay = parseInt($("#delay").value, 10) || 0;
    const data = await api(`/api/exclusions/${id}?compute_delay_ms=${delay}`, {
      method: "DELETE",
    });
    adoptServerState(data, { prevVersion, expectedBump: 1 });
    showMsg(msg, `已取消排除 #${id}`, true);
  } catch (err) {
    showMsg(msg, err.message);
  }
}

async function resetDemo() {
  if (!confirm("确定重置全部演示数据？已揭示前缀将被清空。")) return;
  const data = await api("/api/reset", { method: "POST" });
  // 本窗口主动重置：表单草稿失去意义，强制同步为默认条件
  adoptServerState(data, { syncForm: true });
}

$("#btn-apply").addEventListener("click", applyCondition);
$("#btn-reveal").addEventListener("click", revealNext);
$("#btn-exclude").addEventListener("click", addExclusion);
$("#btn-reset").addEventListener("click", resetDemo);
$("#article-filter").addEventListener("input", renderArticleList);
$("#interest").addEventListener("input", markDirty);
$("#dlg-close").addEventListener("click", () => $("#article-dialog").close());

refresh().catch((err) => {
  $("#job-status").textContent = `加载失败：${err.message}`;
});
