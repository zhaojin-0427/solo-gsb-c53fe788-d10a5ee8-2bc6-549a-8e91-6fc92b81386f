/* 无人机航迹冲突消解台 — vanilla JS front-end */
"use strict";

const COLORS = [
  "#4da3ff", "#38d39f", "#ffb020", "#ff5d6c", "#b48bff",
  "#33e0d0", "#f57fb6", "#9dcc4a", "#e08a4a", "#6aa2ff",
];

const state = {
  scenario: null,        // full scenario DTO from server
  scenarioId: null,
  sorties: [],
  pairConstraints: [],
  earliestConflicts: [],
  solution: null,        // THIS run's solve result (may be status "unknown")
  priorSolution: null,   // retained old table (original thresholds) after an unknown re-solve
  unverifiedSolve: null, // persisted marker: most recent solve was unknown (survives reload)
  history: [],
  locks: {},             // sortieId -> offset (only when checked)
  time: 0,
  maxTime: 1,
  colorsById: {},
  batches: [],
  batch: null,           // currently open batch summary + warning
  warning: null,
  warningSorties: {},    // latest warning rows keyed by sortie id
  reviewNodes: [],       // accepted report nodes (incl. baseline) for the open batch
  review: null,          // last warning-change review result
  workzone: null,        // last temporary work-zone start-window result
};

const $ = (id) => document.getElementById(id);

function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.add("hidden"), ms);
}

function colorFor(id) {
  if (!(id in state.colorsById)) {
    state.colorsById[id] = COLORS[Object.keys(state.colorsById).length % COLORS.length];
  }
  return state.colorsById[id];
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

async function importScenario(payload) {
  const data = await api("/api/import", { method: "POST", body: JSON.stringify(payload) });
  loadScenarioDto(data);
  state.history = await fetchHistory();
  renderAll();
}

function loadScenarioDto(dto) {
  state.scenario = dto;
  state.scenarioId = dto.scenario_id;
  state.sorties = dto.sorties;
  state.pairConstraints = dto.pair_constraints || [];
  state.earliestConflicts = dto.earliest_conflicts || [];
  state.solution = dto.solution || null;
  state.priorSolution = dto.prior_solution || null;
  state.unverifiedSolve = dto.unverified_solve || null;
  // After a page reload the stored result itself is the OLD feasible table,
  // not the unproven run; synthesise an "unknown" view of the most recent
  // (persisted) unproven solve so every panel renders exactly like right after
  // the timed-out POST.
  if (state.unverifiedSolve && !(state.solution && state.solution.status === "unknown")) {
    state.solution = {
      feasible: false,
      status: "unknown",
      locked: state.unverifiedSolve.locks || {},
      weighted_delay: null,
      offsets: {},
      solved_conflicts: [],
    };
  }
  // Keep the locks this run actually applied (they stay checked after a
  // re-solve — in particular after an unknown one — instead of vanishing).
  state.locks = { ...((state.solution && state.solution.locked) || {}) };
  state.colorsById = {};
  state.batch = null;
  state.warning = null;
  state.warningSorties = {};
  state.review = null;
  state.reviewNodes = [];
  state.workzone = null;
  state.sorties.forEach((s) => colorFor(s.id));
  // The inputs show the most recent run's attempted thresholds: the solve
  // response echoes THIS run's values; after a reload with a persisted unknown
  // marker those live on unverified_solve (the top level carries the retained
  // old table's original values).
  const inputR = state.unverifiedSolve
    ? state.unverifiedSolve.safety_radius : dto.safety_radius;
  const inputH = state.unverifiedSolve
    ? state.unverifiedSolve.safety_height : dto.safety_height;
  $("safetyRadius").value = inputR;
  $("safetyHeight").value = inputH;
  $("newSolveBtn").disabled = false;
  updateCreateBatchBtn();
  // Naming/saving the retained old table stays possible after an unknown run,
  // but neither it nor the rename clears the persisted unknown marker.
  $("savePlanBtn").disabled = !displayedSolution();
  updateMaxTime();
  state.time = 0;
  $("timeScrub").value = 0;
  renderResultPanel();
  renderSortiePanel();
  fetchBatches();
}

// The schedule the page draws: this run's table when feasible, otherwise the
// retained old table (so an unknown re-solve never makes it vanish) — always
// marked as such wherever it is shown.
function displayedSolution() {
  if (state.solution && state.solution.feasible) return state.solution;
  if (state.priorSolution && state.priorSolution.feasible) return state.priorSolution;
  return null;
}

// The retained old table is the only drawable/freezable table exactly when the
// most recent solve was UNVERIFIED (the persisted unknown marker, also set
// straight after the timed-out POST) AND a feasible old table is retained.
function usingPriorTable() {
  return !!state.unverifiedSolve
    && !(state.solution && state.solution.feasible)
    && !!(state.priorSolution && state.priorSolution.feasible);
}

function updateCreateBatchBtn() {
  const btn = $("createBatchBtn");
  if (state.solution && state.solution.feasible) {
    btn.disabled = false;
    btn.textContent = "从当前方案创建执行批次";
    btn.title = "冻结本次求解得到的起飞表与当前安全阈值";
  } else if (usingPriorTable()) {
    // Explicit old-table selection: the new constraints were NOT verified
    // (unknown), so the action names the old table and its original
    // thresholds instead of pretending the current run succeeded.
    btn.disabled = false;
    btn.textContent = `选用已保存旧表创建批次（原阈值 R=${state.priorSolution.safety_radius} / H=${state.priorSolution.safety_height}）`;
    btn.title = "本次重算未验证新约束（unknown）；显式选用 SQLite 中保留的旧表及其原阈值冻结为批次";
  } else {
    btn.disabled = true;
    btn.textContent = "从当前方案创建执行批次";
    btn.title = "";
  }
}

async function fetchHistory() {
  const d = await api("/api/scenarios");
  state.history = d.scenarios;
  renderHistory();
  return state.history;
}

async function solve(extraLocks) {
  const body = {
    safety_radius: parseFloat($("safetyRadius").value),
    safety_height: parseFloat($("safetyHeight").value),
    time_limit: parseFloat($("timeLimit").value) || 20,
    locks: extraLocks !== undefined ? extraLocks : collectLocks(),
  };
  try {
    const dto = await api(`/api/scenarios/${state.scenarioId}/solve`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    loadScenarioDto(dto);
    state.history = await fetchHistory();
    renderAll();
    if (dto.solution.feasible) toast("求解成功，已生成无冲突起飞时刻");
    else if (dto.solution.status === "unknown") {
      toast(dto.prior_solution
        ? "本次求解超时（unknown）：新锁定/阈值未经验证；下方仍展示已保留的旧表（原阈值），可显式选用它创建批次"
        : "求解超时：暂未找到可行表，也未证明无解；已有方案保持不变", 4600);
    } else toast("当前条件无解，已列出极小无解架次集合", 4000);
  } catch (e) {
    toast("求解失败：" + e.message, 4200);
  }
}

function collectLocks() {
  return Object.entries(state.locks).map(([sortie_id, offset]) => ({ sortie_id, offset }));
}

// ---------------------------------------------------------------------------
// Geometry interpolation (mirrors the back-end model)
// ---------------------------------------------------------------------------

function segsOf(s) {
  const out = [];
  for (let i = 0; i < s.waypoints.length - 1; i++) {
    const a = s.waypoints[i], b = s.waypoints[i + 1];
    out.push({ a, b });
  }
  return out;
}

function positionAt(s, takeoff, tGlobal) {
  const tau = tGlobal - takeoff;
  if (tau < -1e-9 || tau > s.flight_end + 1e-9) return null;
  const wps = s.waypoints;
  for (let i = 0; i < wps.length - 1; i++) {
    const a = wps[i], b = wps[i + 1];
    if (tau >= a.t - 1e-9 && tau <= b.t + 1e-9) {
      const k = b.t === a.t ? 0 : Math.min(1, Math.max(0, (tau - a.t) / (b.t - a.t)));
      return {
        x: a.x + (b.x - a.x) * k,
        y: a.y + (b.y - a.y) * k,
        z: a.z + (b.z - a.z) * k,
      };
    }
  }
  const last = wps[wps.length - 1];
  return { x: last.x, y: last.y, z: last.z };
}

function updateMaxTime() {
  let m = 0;
  const off = currentOffsets();
  for (const s of state.sorties) {
    m = Math.max(m, (off[s.id] || 0) + s.flight_end);
  }
  // earliest track also matters
  for (const s of state.sorties) m = Math.max(m, s.flight_end);
  state.maxTime = Math.max(1, Math.ceil(m));
}

function currentOffsets() {
  const o = {};
  const shown = displayedSolution();
  for (const s of state.sorties) {
    o[s.id] = shown ? (shown.offsets[s.id] ?? 0) : 0;
  }
  return o;
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function renderSortiePanel() {
  const box = $("sortiesList");
  box.innerHTML = "";
  const shown = displayedSolution();
  const solvedOff = shown ? shown.offsets : null;
  const stale = usingPriorTable();
  for (const s of state.sorties) {
    const c = colorFor(s.id);
    const div = document.createElement("div");
    div.className = "sortie-item";
    const locked = s.id in state.locks;
    const lockVal = state.locks[s.id] ?? 0;
    div.innerHTML = `
      <div class="si-head">
        <span class="swatch" style="background:${c}"></span>
        <span class="si-name">${escapeHtml(s.id)}</span>
        <span class="si-meta">w=${s.weight} · 窗[0,${s.window_hi}] · 长${s.flight_end}s</span>
      </div>
      <div class="si-body">
        <span class="${solvedOff ? "solved-tag" : ""}">${stale ? "旧表偏移" : "求解偏移"}: <b>${solvedOff ? solvedOff[s.id] : "—"}</b></span>
        <label class="lock-wrap">
          <input type="checkbox" ${locked ? "checked" : ""} data-lock-check="${s.id}" />
          锁定
          <input type="number" min="0" max="${s.window_hi}" step="1" value="${lockVal}"
                 data-lock-val="${s.id}" style="${locked ? "" : "opacity:.5"}" />
        </label>
      </div>`;
    box.appendChild(div);
  }
  box.querySelectorAll("[data-lock-check]").forEach((el) => {
    el.addEventListener("change", () => {
      const id = el.getAttribute("data-lock-check");
      if (el.checked) state.locks[id] = parseInt(el.parentElement.querySelector("[data-lock-val]").value, 10) || 0;
      else delete state.locks[id];
      renderSortiePanel();
    });
  });
  box.querySelectorAll("[data-lock-val]").forEach((el) => {
    el.addEventListener("input", () => {
      const id = el.getAttribute("data-lock-val");
      if (id in state.locks) state.locks[id] = parseInt(el.value, 10) || 0;
    });
  });
}

function renderHistory() {
  const box = $("historyList");
  box.innerHTML = "";
  if (!state.history.length) {
    box.innerHTML = `<div class="hint">暂无保存的方案。</div>`;
    return;
  }
  for (const h of state.history) {
    const div = document.createElement("div");
    div.className = "history-item";
    const title = h.name || `场景 #${h.id}`;
    div.innerHTML = `<span>#${h.id} ${escapeHtml(title)} ${h.has_result ? "✅" : "⭕"}</span>
      <span class="h-del" title="删除">🗑</span>`;
    div.addEventListener("click", async (e) => {
      if (e.target.classList.contains("h-del")) {
        if (!confirm(`删除场景 #${h.id}？`)) return;
        await api(`/api/scenarios/${h.id}`, { method: "DELETE" });
        toast("已删除");
        await fetchHistory();
        return;
      }
      try {
        const dto = await api(`/api/scenarios/${h.id}`);
        loadScenarioDto(dto);
        renderAll();
        toast("已载入历史方案（保留输入快照）");
      } catch (err) {
        toast("载入失败：" + err.message);
      }
    });
    box.appendChild(div);
  }
}

// ---------------------------------------------------------------------------
// Result panel
// ---------------------------------------------------------------------------

function renderResultPanel() {
  const panel = $("resultPanel");
  const body = $("resultBody");
  const title = $("resultTitle");
  const sol = state.solution;
  if (!sol) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  if (sol.status === "unknown") {
    title.textContent = "⏳ 本次求解超时 — 可行与无解均未证明（unknown）";
    const prior = usingPriorTable() ? state.priorSolution : null;
    const att = state.unverifiedSolve || {};
    const attR = att.safety_radius ?? state.scenario.safety_radius;
    const attH = att.safety_height ?? state.scenario.safety_height;
    let html = `
      <div class="kv bad"><span>本次状态</span><b>${sol.status}</b></div>
      <div class="hint" style="margin-top:6px">
        在给定时限内 CP-SAT 既没有找到可行起飞表，也没有证明无解。这与“确实无解”不同：
        本次送入的锁定与安全阈值（R=${attR}m / H=${attH}m）
        <b>未经验证</b>，已保留在左侧输入中，可加大求解时限后点“重算”重试。
        该 unknown 状态已持久记录；改名/保存旧表不会解除批次创建限制，只有重新求解得到
        可行解（或确证无解）才会改变。</div>`;
    if (prior) {
      const seq = Object.keys(prior.offsets).sort()
        .map((k) => `${escapeHtml(k)}=${prior.offsets[k]}`).join("，");
      html += `
      <div class="prior-box">
        <div class="prior-title">📦 已保留的旧表（SQLite 中的上一可行方案，未受本次重算影响）</div>
        <div class="kv"><span>旧表原约束</span><b>R=${prior.safety_radius}m / H=${prior.safety_height}m</b></div>
        <div class="kv"><span>旧表状态</span><b>${prior.status}</b></div>
        <div class="kv"><span>加权总延误 Σw·d</span><b>${prior.weighted_delay}</b></div>
        <div class="conf-line" style="margin-top:6px">旧表起飞时刻（秒）：</div>
        <div style="margin-top:2px">${seq}</div>
        <div class="hint" style="margin-top:6px">地图/时间轴中的实线仍是该旧表；
          如需用它冻结执行批次，请在“执行批次”卡片点
          <b>“选用已保存旧表创建批次”</b>（将沿用旧表原阈值）。</div>
      </div>`;
    } else {
      html += `<div class="hint" style="margin-top:6px">当前没有可回退的已保存可行方案；已建执行批次不受影响。</div>`;
    }
    body.innerHTML = html;
  } else if (!sol.feasible) {
    title.textContent = "⚠️ 无解 — 极小无解架次集合";
    let html = `<div class="kv bad"><span>状态</span><b>${sol.status}</b></div>`;
    html += `<div class="hint" style="margin-top:6px">下列每个集合都是包含关系极小的：删去其中任意一架次，其余架次即可消解。互斥时差 Δ = 后者起飞 − 前者起飞（秒）。</div>`;
    for (const core of sol.cores) {
      html += `<div class="core-box">
        <div class="ids">${core.sorties.map(escapeHtml).join("、")}</div>`;
      for (const m of core.mutex_offsets) {
        const iv = m.forbidden_offsets.map(([a, b]) => (a === b ? `${a}` : `[${a}, ${b}]`)).join("，");
        html += `<div class="mutex-line">${escapeHtml(m.sortie_a)} ↔ ${escapeHtml(m.sortie_b)}：禁止 Δ ∈ {${iv}} 秒</div>`;
      }
      html += `</div>`;
    }
    if (sol.truncated) html += `<div class="hint">（无解核可能未列全，受求解时限/数量上限影响）</div>`;
    body.innerHTML = html;
  } else {
    title.textContent = "✅ 无冲突方案";
    const seq = Object.keys(sol.offsets).sort().map((k) => `${escapeHtml(k)}=${sol.offsets[k]}`).join("，");
    body.innerHTML = `
      <div class="kv ok"><span>状态</span><b>${sol.status}</b></div>
      <div class="kv"><span>加权总延误 Σw·d</span><b>${sol.weighted_delay}</b></div>
      <div class="kv"><span>剩余冲突</span><b>${sol.solved_conflicts.length}</b></div>
      <div class="kv"><span>耗时</span><b>${sol.elapsed_seconds} s</b></div>
      <div class="conf-line" style="margin-top:8px">起飞时刻（相对最早起飞，秒）：</div>
      <div style="margin-top:2px">${seq}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Canvas: map
// ---------------------------------------------------------------------------

function getCtx(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (canvas._w !== rect.width || canvas._h !== rect.height) {
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    canvas._w = rect.width; canvas._h = rect.height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: canvas._w, h: canvas._h };
}

function worldBounds() {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const s of state.sorties) {
    for (const p of s.waypoints) {
      minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
      minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
    }
  }
  if (!isFinite(minX)) return { minX: -100, maxX: 100, minY: -100, maxY: 100 };
  const pad = Math.max(20, 0.08 * Math.max(maxX - minX, maxY - minY, 1));
  return { minX: minX - pad, maxX: maxX + pad, minY: minY - pad, maxY: maxY + pad };
}

function projectors(w, h) {
  const b = worldBounds();
  const sx = w / (b.maxX - b.minX);
  const sy = h / (b.maxY - b.minY);
  const s = Math.min(sx, sy) * 0.92;
  const ox = (w - (b.maxX - b.minX) * s) / 2;
  const oy = (h - (b.maxY - b.minY) * s) / 2;
  return {
    xy: (x, y) => [ox + (x - b.minX) * s, h - (oy + (y - b.minY) * s)],
    scale: s,
  };
}

function drawGrid(ctx, w, h, proj) {
  ctx.strokeStyle = "#1c2740";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#5a6b8f";
  ctx.font = "10px monospace";
  const b = worldBounds();
  const step = niceStep((b.maxX - b.minX) / 8);
  for (let gx = Math.ceil(b.minX / step) * step; gx <= b.maxX; gx += step) {
    const [px] = proj.xy(gx, b.minY);
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
  }
  for (let gy = Math.ceil(b.minY / step) * step; gy <= b.maxY; gy += step) {
    const [, py] = proj.xy(b.minX, gy);
    ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(w, py); ctx.stroke();
  }
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / p;
  if (n < 1.5) return p;
  if (n < 3.5) return 2 * p;
  if (n < 7.5) return 5 * p;
  return 10 * p;
}

function drawTrack(ctx, proj, s, takeoff, dashed, time) {
  const c = colorFor(s.id);
  ctx.save();
  ctx.strokeStyle = c;
  ctx.lineWidth = 2;
  ctx.setLineDash(dashed ? [6, 5] : []);
  ctx.globalAlpha = dashed ? 0.5 : 1;
  ctx.beginPath();
  s.waypoints.forEach((p, i) => {
    const [px, py] = proj.xy(p.x, p.y);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
  ctx.stroke();
  // waypoint dots
  ctx.setLineDash([]);
  for (const p of s.waypoints) {
    const [px, py] = proj.xy(p.x, p.y);
    ctx.beginPath(); ctx.arc(px, py, 2.4, 0, Math.PI * 2); ctx.fillStyle = c; ctx.fill();
  }
  // label at first waypoint
  const p0 = s.waypoints[0];
  const [lx, ly] = proj.xy(p0.x, p0.y);
  ctx.fillStyle = c;
  ctx.font = "11px sans-serif";
  ctx.fillText(`${s.id}${takeoff ? " +" + takeoff + "s" : ""}`, lx + 4, ly - 5);
  // moving aircraft marker
  const pos = positionAt(s, takeoff, time);
  if (pos) {
    const [mx, my] = proj.xy(pos.x, pos.y);
    ctx.beginPath(); ctx.arc(mx, my, dashed ? 5 : 6, 0, Math.PI * 2);
    ctx.fillStyle = dashed ? "rgba(255,255,255,.35)" : c;
    ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke();
  }
  ctx.restore();
}

function drawConflictMarkers(ctx, proj, conflicts) {
  if (!$("showConflicts").checked) return;
  for (const cf of conflicts) {
    // witness positions are absolute (x,y,z); a_pos/b_pos at witness_time
    const [px, py] = proj.xy(cf.a_pos[0], cf.a_pos[1]);
    ctx.save();
    ctx.strokeStyle = "#ff5d6c";
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(px, py, 9, 0, Math.PI * 2); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(px - 13, py); ctx.lineTo(px + 13, py);
    ctx.moveTo(px, py - 13); ctx.lineTo(px, py + 13);
    ctx.stroke();
    ctx.restore();
  }
}

function drawSafetyRadius(ctx, proj, conflicts, R) {
  if (!$("showConflicts").checked) return;
  ctx.save();
  ctx.strokeStyle = "rgba(255,93,108,.55)";
  ctx.setLineDash([3, 3]);
  for (const cf of conflicts) {
    const [px, py] = proj.xy(cf.a_pos[0], cf.a_pos[1]);
    ctx.beginPath();
    ctx.arc(px, py, Math.max(2, R * proj.scale), 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.restore();
}

function renderMap() {
  const canvas = $("mapCanvas");
  const { ctx, w, h } = getCtx(canvas);
  ctx.clearRect(0, 0, w, h);
  if (!state.sorties.length) return;
  const proj = projectors(w, h);
  drawGrid(ctx, w, h, proj);

  const showE = $("showEarliest").checked;
  const showS = $("showSolved").checked;
  const shown = displayedSolution();
  const solvedOff = shown ? shown.offsets : null;

  for (const s of state.sorties) {
    if (showE) drawTrack(ctx, proj, s, 0, true, state.time);
  }
  for (const s of state.sorties) {
    if (showS && solvedOff) drawTrack(ctx, proj, s, solvedOff[s.id] || 0, false, state.time);
  }
  drawSafetyRadius(ctx, proj, state.earliestConflicts, state.scenario.safety_radius);
  drawConflictMarkers(ctx, proj, state.earliestConflicts);
}

// ---------------------------------------------------------------------------
// Canvas: timeline
// ---------------------------------------------------------------------------

function timelineGeometry(w, h) {
  const left = 92, right = 16, top = 16, rowH = (h - top - 12) / Math.max(1, state.sorties.length);
  return { left, right, top, rowH, plotW: w - left - right };
}

function tToX(t, g) {
  return g.left + (t / state.maxTime) * g.plotW;
}

function renderTimeline() {
  const canvas = $("timelineCanvas");
  const { ctx, w, h } = getCtx(canvas);
  ctx.clearRect(0, 0, w, h);
  if (!state.sorties.length) return;
  const g = timelineGeometry(w, h);
  const shownT = displayedSolution();
  const solvedOff = shownT ? shownT.offsets : null;

  // grid + axis
  ctx.strokeStyle = "#243049"; ctx.fillStyle = "#8092b5"; ctx.font = "10px monospace";
  const step = niceStep(state.maxTime / 10);
  for (let t = 0; t <= state.maxTime + 1e-9; t += step) {
    const x = tToX(t, g);
    ctx.beginPath(); ctx.moveTo(x, g.top - 4); ctx.lineTo(x, h - 12); ctx.stroke();
    ctx.fillText(`${t}`, x - 6, h - 1);
  }

  state.sorties.forEach((s, i) => {
    const y = g.top + i * g.rowH;
    const barH = Math.min(20, g.rowH - 8);
    const yc = y + g.rowH / 2;
    ctx.fillStyle = colorFor(s.id);
    ctx.font = "11px sans-serif";
    ctx.fillText(s.id, 8, yc + 4);

    // earliest bar (dashed outline)
    if ($("showEarliest").checked) {
      drawBar(ctx, tToX(0, g), yc - barH / 2, tToX(s.flight_end, g) - tToX(0, g), barH,
              colorFor(s.id), true, 0.45);
    }
    // solved bar
    if (solvedOff && $("showSolved").checked) {
      const t0 = solvedOff[s.id] || 0;
      drawBar(ctx, tToX(t0, g), yc - barH / 2,
              tToX(t0 + s.flight_end, g) - tToX(t0, g), barH,
              colorFor(s.id), false, 0.92);
    }

    // conflict ticks on earliest schedule
    if ($("showConflicts").checked) {
      for (const cf of state.earliestConflicts) {
        if (cf.sortie_a !== s.id && cf.sortie_b !== s.id) continue;
        const x1 = tToX(cf.time_start, g), x2 = tToX(cf.time_end, g);
        ctx.fillStyle = "rgba(255,93,108,.85)";
        ctx.fillRect(Math.min(x1, x2), yc - barH / 2 - 3, Math.max(2, Math.abs(x2 - x1) + 2), 3);
      }
    }
  });

  // playhead
  const px = tToX(state.time, g);
  ctx.strokeStyle = "#4da3ff"; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(px, g.top - 6); ctx.lineTo(px, h - 12); ctx.stroke();
}

function drawBar(ctx, x, y, w, h, color, dashed, alpha) {
  ctx.save();
  ctx.globalAlpha = alpha;
  if (dashed) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
    ctx.strokeRect(x, y, w, h);
  } else {
    ctx.fillStyle = color;
    roundRect(ctx, x, y, w, h, 4); ctx.fill();
  }
  ctx.restore();
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

// ---------------------------------------------------------------------------
// Canvas: altitude profile
// ---------------------------------------------------------------------------

function renderProfile() {
  const canvas = $("profileCanvas");
  const { ctx, w, h } = getCtx(canvas);
  ctx.clearRect(0, 0, w, h);
  if (!state.sorties.length) return;
  const g = timelineGeometry(w, h);
  let minZ = 0, maxZ = 1;
  for (const s of state.sorties) for (const p of s.waypoints) maxZ = Math.max(maxZ, p.z);
  const zy = (z) => h - 14 - (z / maxZ) * (h - 28);

  ctx.strokeStyle = "#243049"; ctx.fillStyle = "#8092b5"; ctx.font = "10px monospace";
  for (let z = 0; z <= maxZ + 1e-9; z += niceStep(maxZ / 4)) {
    const y = zy(z);
    ctx.beginPath(); ctx.moveTo(g.left, y); ctx.lineTo(w - g.right, y); ctx.stroke();
    ctx.fillText(`${z}m`, 6, y + 3);
  }

  const shownP = displayedSolution();
  const solvedOff = shownP ? shownP.offsets : null;
  for (const s of state.sorties) {
    const c = colorFor(s.id);
    if ($("showEarliest").checked) {
      drawProfileLine(ctx, s, 0, g, c, zy, true);
    }
    if (solvedOff && $("showSolved").checked) {
      drawProfileLine(ctx, s, solvedOff[s.id] || 0, g, c, zy, false);
    }
  }
}

function drawProfileLine(ctx, s, takeoff, g, c, zy, dashed) {
  ctx.save();
  ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.setLineDash(dashed ? [5, 4] : []);
  ctx.globalAlpha = dashed ? 0.45 : 1;
  ctx.beginPath();
  s.waypoints.forEach((p, i) => {
    const x = tToX(takeoff + p.t, g);
    if (i === 0) ctx.moveTo(x, zy(p.z)); else ctx.lineTo(x, zy(p.z));
  });
  ctx.stroke();
  const pos = positionAt(s, takeoff, state.time);
  if (pos) {
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(tToX(state.time, g), zy(pos.z), 4, 0, Math.PI * 2);
    ctx.fillStyle = c; ctx.fill();
  }
  ctx.restore();
}

// ---------------------------------------------------------------------------
// Legend / render orchestration
// ---------------------------------------------------------------------------

function renderLegend() {
  const box = $("mapLegend");
  box.innerHTML = "";
  for (const s of state.sorties) {
    const span = document.createElement("span");
    span.className = "lg";
    span.innerHTML = `<span class="swatch" style="background:${colorFor(s.id)}"></span>${escapeHtml(s.id)}`;
    box.appendChild(span);
  }
  const cf = document.createElement("span");
  cf.className = "lg";
  cf.innerHTML = `<span style="color:#ff5d6c">✕</span> 冲突见证点（最早起飞）`;
  box.appendChild(cf);
  if (usingPriorTable()) {
    const p = state.priorSolution;
    const note = document.createElement("span");
    note.className = "lg";
    note.innerHTML = `<span style="color:#ffb020">▬</span> 实线为已保留旧表（原阈值 R=${p.safety_radius}m / H=${p.safety_height}m），本次重算 unknown`;
    box.appendChild(note);
  }
}

function renderAll() {
  if (!state.scenario) return;
  updateMaxTime();
  renderLegend();
  renderMap();
  renderTimeline();
  renderProfile();
  renderSortiePanel();
  renderResultPanel();
}

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

// ---------------------------------------------------------------------------
// Execution batches & deviation early-warning
// ---------------------------------------------------------------------------

async function fetchBatches() {
  if (!state.scenarioId) return;
  const d = await api(`/api/batches?scenario_id=${state.scenarioId}`);
  state.batches = d.batches;
  renderBatchList();
}

function reportMap(batch) {
  const m = {};
  for (const r of batch.reports || []) m[r.sortie_id] = r;
  return m;
}

function nextRevision(sortieId) {
  const m = reportMap(state.batch || { reports: [] });
  const cur = m[sortieId];
  return cur ? cur.revision + 1 : 1;
}

async function createBatch() {
  if (!state.scenarioId) return;
  // After an unknown re-solve the only freezable plan is the retained old
  // table; selecting it must be an explicit, informed choice (the new locks/
  // thresholds were never verified).
  const prior = usingPriorTable() ? state.priorSolution : null;
  if (prior) {
    const seq = Object.keys(prior.offsets).sort()
      .map((k) => `${k}=${prior.offsets[k]}`).join("，");
    const ok = confirm(
      "本次重算返回 unknown，新锁定/阈值未经验证。\n\n" +
      "将显式选用 SQLite 中保留的旧表创建执行批次：\n" +
      `· 旧表起飞时刻（秒）：${seq}\n` +
      `· 旧表原阈值：R=${prior.safety_radius}m / H=${prior.safety_height}m\n\n` +
      "确定选用旧表及原阈值冻结为批次？");
    if (!ok) return;
  }
  const name = prompt("为执行批次命名（可留空）：", `批次 #${state.scenarioId}`);
  if (name === null) return;
  try {
    const body = { name };
    if (prior) {
      // Explicit, informed selection of the retained old table AND a
      // server-checked confirmation of the original thresholds it was solved
      // under; the server rejects anything else (omitted flag / false / a
      // different threshold) without creating a batch.
      body.use_stored_plan = true;
      body.confirmed_safety_radius = prior.safety_radius;
      body.confirmed_safety_height = prior.safety_height;
    }
    const b = await api(`/api/scenarios/${state.scenarioId}/batches`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    toast(prior
      ? "已用已保存旧表（原阈值）创建执行批次"
      : "已创建执行批次（输入航迹 / 安全阈值 / 计划偏移已冻结）");
    await fetchBatches();
    await openBatch(b.batch_id);
  } catch (e) {
    toast("创建批次被拒绝：" + e.message, 4600);
  }
}

async function openBatch(bid) {
  try {
    const b = await api(`/api/batches/${bid}`);
    // The page and HTTP API share the same batch rows; make sure the view is
    // looking at the scenario the batch was frozen from.
    if (!state.scenarioId || state.scenarioId !== b.scenario_id) {
      const dto = await api(`/api/scenarios/${b.scenario_id}`);
      loadScenarioDto(dto);
      renderAll();
    }
    state.batch = b;
    state.warning = null;
    state.warningSorties = {};
    state.review = null;
    state.workzone = null;
    renderBatchList();
    renderBatchDetail();
    renderWorkzone();
    await fetchReviewNodes();
  } catch (e) {
    toast("载入批次失败：" + e.message);
  }
}

async function fetchReviewNodes() {
  if (!state.batch) return;
  try {
    const d = await api(`/api/batches/${state.batch.batch_id}/report-nodes`);
    state.reviewNodes = d.nodes || [];
  } catch (e) {
    state.reviewNodes = [];
  }
  renderReviewNodes();
}

async function deleteBatch(bid) {
  if (!confirm("删除该执行批次及其全部上报？")) return;
  await api(`/api/batches/${bid}`, { method: "DELETE" });
  if (state.batch && state.batch.batch_id === bid) {
    state.batch = null;
    state.warning = null;
    state.warningSorties = {};
    state.review = null;
    state.reviewNodes = [];
    state.workzone = null;
  }
  toast("批次已删除");
  await fetchBatches();
  renderBatchDetail();
  renderWorkzone();
}

async function submitReport(sortieId) {
  const offEl = document.querySelector(`[data-off="${sortieId}"]`);
  const actual = parseInt(offEl.value, 10);
  if (!Number.isInteger(actual) || actual < 0) {
    toast("实际起飞偏移必须是非负整数秒");
    return;
  }
  const revision = nextRevision(sortieId);
  const t = parseInt($("warnT").value, 10);
  const hadWorkzoneQuery = state.workzone !== null;
  try {
    const r = await api(`/api/batches/${state.batch.batch_id}/reports`, {
      method: "PUT",
      body: JSON.stringify({ sortie_id: sortieId, actual_offset: actual, revision }),
    });
    toast(r.idempotent ? "重复上报（幂等），内容未变" : `已接受修订号 ${revision}`);
    await openBatch(state.batch.batch_id);
    if (Number.isInteger(t) && t >= 0) {
      $("warnT").value = t;
      await evaluateWarning();
      if (hadWorkzoneQuery) await evaluateWorkzone(true);
    }
  } catch (e) {
    // Another process may have committed a higher revision concurrently:
    // reload so the page (and auto +1 revision) reflects the winning row.
    toast(`上报冲突：${e.message}`, 4200);
    await openBatch(state.batch.batch_id);
  }
}

async function evaluateWarning() {
  const t = parseInt($("warnT").value, 10);
  if (!Number.isInteger(t) || t < 0) {
    toast("t 必须是非负整数秒");
    return;
  }
  try {
    const d = await api(`/api/batches/${state.batch.batch_id}/warning?t=${t}`);
    state.batch = d;
    state.warning = d.warning;
    state.warningSorties = {};
    for (const row of d.warning.sorties) state.warningSorties[row.id] = row;
    renderBatchDetail();
  } catch (e) {
    state.warning = null;
    renderBatchDetail();
    toast("评估被拒绝：" + e.message, 4200);
  }
}

function renderBatchList() {
  const box = $("batchList");
  box.innerHTML = "";
  if (!state.batches.length) {
    box.innerHTML = `<div class="hint">尚无执行批次。</div>`;
    return;
  }
  for (const b of state.batches) {
    const div = document.createElement("div");
    div.className = "history-item batch-item" +
      (state.batch && state.batch.batch_id === b.id ? " active" : "");
    div.innerHTML = `<span>#${b.id} ${escapeHtml(b.name || "未命名批次")}（已上报 ${b.reported_count}）</span>
      <span class="h-del" title="删除批次">🗑</span>`;
    div.addEventListener("click", (e) => {
      if (e.target.classList.contains("h-del")) { deleteBatch(b.id); return; }
      openBatch(b.id);
    });
    box.appendChild(div);
  }
}

function statusLabel(status) {
  return { reported: "已上报", planned: "按计划", overdue: "逾期" }[status] || status;
}

function devClass(v) {
  if (v === null || v === undefined) return "";
  return v > 0 ? "pos" : v < 0 ? "neg" : "zero";
}

function renderBatchDetail() {
  const detail = $("batchDetail");
  if (!state.batch) {
    detail.classList.add("hidden");
    return;
  }
  detail.classList.remove("hidden");
  const b = state.batch;
  $("batchTitle").textContent = `批次 #${b.batch_id}${b.name ? " · " + b.name : ""}`;
  const plan = Object.keys(b.planned_offsets).sort()
    .map((k) => `${escapeHtml(k)}=${b.planned_offsets[k]}`).join("，");
  $("batchPlan").textContent = `计划偏移（秒）：${plan}；安全 R=${b.safety_radius}m / H=${b.safety_height}m`;

  const reports = reportMap(b);
  const box = $("warnSorties");
  box.innerHTML = "";
  for (const s of state.sorties) {
    const rep = reports[s.id];
    const wrow = state.warningSorties[s.id];
    const planned = b.planned_offsets[s.id];
    const dev = rep ? rep.actual_offset - planned : null;
    const status = wrow ? wrow.status : "planned";
    const div = document.createElement("div");
    div.className = "warn-sortie";
    div.innerHTML = `
      <div class="ws-top">
        <span class="swatch" style="background:${colorFor(s.id)}"></span>
        <span class="ws-id">${escapeHtml(s.id)}</span>
        <span class="ws-tag ${status}">${statusLabel(status)}</span>
        <span class="ws-meta">计划 ${planned}s${rep ? ` · 实际 ${rep.actual_offset}s · 修订 ${rep.revision}` : ""}</span>
      </div>
      <div class="ws-dev">
        <input type="number" min="0" step="1" value="${rep ? rep.actual_offset : 0}" data-off="${s.id}" />
        <button data-report="${s.id}">上报（修订号 ${nextRevision(s.id)}）</button>
        <span class="dev-val ${devClass(dev)}">${rep
          ? `偏离计划 ${dev > 0 ? "+" : ""}${dev}s` : "未上报"}</span>
      </div>`;
    box.appendChild(div);
  }
  box.querySelectorAll("[data-report]").forEach((btn) => {
    btn.addEventListener("click", () => submitReport(btn.getAttribute("data-report")));
  });

  const sum = $("warnSummary");
  const cf = $("warnConflicts");
  if (!state.warning) {
    sum.innerHTML = `<div class="hint">设置当前秒 t 后点“评估预警”。</div>`;
    cf.innerHTML = "";
    return;
  }
  const w = state.warning;
  sum.innerHTML = `
    <div class="${w.incomplete ? "bad" : "ok"}">
      ${w.incomplete ? `⚠️ 预测不完整：${w.overdue_sorties.map(escapeHtml).join("、")} 已逾期并排除`
                      : "✅ 预测完整（全部架次可预测）"}
    </div>
    <div class="hint">首个冲突时刻：${w.first_conflict_time === null ? "无" : w.first_conflict_time + " s"}；
      已上报偏离：${w.reported_deviations.length
        ? w.reported_deviations.map((d) => `${escapeHtml(d.sortie_id)} ${d.deviation_seconds > 0 ? "+" : ""}${d.deviation_seconds}s`).join("，")
        : "无"}</div>`;
  if (!w.conflicts.length) {
    cf.innerHTML = `<div class="wc-none">t = ${w.t} s 及之后无冲突区间。</div>`;
  } else {
    cf.innerHTML = `<div class="wc-title">冲突区间（连续时间，≥ t=${w.t}s）</div>` +
      w.conflicts.map((x) => `<div class="wc-line">${escapeHtml(x.sortie_a)} ↔ ${escapeHtml(x.sortie_b)}：
        [${x.time_start}, ${x.time_end}] s（见证 ${x.witness_time} s）</div>`).join("");
  }
}

// ---------------------------------------------------------------------------
// Warning-change review around one accepted report node
// ---------------------------------------------------------------------------

function nodeLabel(n) {
  if (n.is_baseline) {
    return `#${n.seq} 基线节点（升级时最新状态 / 空状态）`;
  }
  return `#${n.seq} ${escapeHtml(n.trigger_sortie_id)} 实际 ${n.trigger_actual_offset}s · 修订 ${n.trigger_revision}`;
}

function renderReviewNodes() {
  const sel = $("reviewNode");
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = "";
  // Users pick an *accepted report node*; the baseline is an internal replay
  // anchor (still available via the HTTP API).
  const selectable = state.reviewNodes.filter((n) => !n.is_baseline);
  if (!selectable.length) {
    sel.innerHTML = `<option value="">（暂无已接受的上报节点）</option>`;
    return;
  }
  for (const n of selectable) {
    const opt = document.createElement("option");
    opt.value = String(n.node_id);
    opt.textContent = nodeLabel(n);
    sel.appendChild(opt);
  }
  if (keep && state.reviewNodes.some((n) => String(n.node_id) === keep)) sel.value = keep;
}

function renderReview() {
  const box = $("reviewBox");
  if (!box) return;
  if (!state.review) { box.innerHTML = ""; return; }
  const r = state.review;
  const node = r.node;
  const head = node.baseline
    ? `<div class="rv-node">基线节点（${r.t}s 评估，无触发上报）</div>`
    : `<div class="rv-node">触发上报：${escapeHtml(node.sortie_id)} 实际 ${node.actual_offset}s · 修订 ${node.revision}</div>`;

  const fc = (v) => (v === null ? '<span class="rv-nodiff">无冲突</span>' : `${v} s`);
  const firstLine = `<div>首个冲突时刻：前 ${fc(r.first_conflict_time_before)} → 后 ${fc(r.first_conflict_time_after)}</div>`;

  const overParts = [];
  if (r.overdue_removed.length)
    overParts.push(`<span class="rv-over-removed">解除逾期：${r.overdue_removed.map(escapeHtml).join("、") || "无"}</span>`);
  if (r.overdue_added.length)
    overParts.push(`<span class="rv-over-added">新增逾期：${r.overdue_added.map(escapeHtml).join("、") || "无"}</span>`);
  if (!overParts.length) overParts.push(`<span class="rv-nodiff">逾期集合无变化（前：${r.overdue_before.map(escapeHtml).join("、") || "无"}）</span>`);
  const overLine = `<div class="rv-over">${overParts.join("；")}</div>`;

  const chips = r.sortie_changes.map((s) => {
    const tag = s.changed ? "changed" : "";
    const tail = s.changed
      ? `${s.takeoff_offset_before ?? "—"}→${s.takeoff_offset_after ?? "—"}s${s.revision_after ? `（修${s.revision_before ?? 0}→${s.revision_after}）` : ""}`
      : `${statusLabel(s.status_after)} ${s.takeoff_offset_after ?? "—"}s`;
    return `<span class="rv-chip ${tag}">${escapeHtml(s.id)}：${tail}</span>`;
  }).join("");

  let pairsHtml;
  if (!r.pair_changes.length) {
    pairsHtml = `<div class="rv-nodiff">冲突区间集合无变化（相交区间只显示变化部分，完全相同不列出）。</div>`;
  } else {
    pairsHtml = r.pair_changes.map((p) => {
      const segHtml = (seg, cls, label) => seg.map((x) =>
        `<div class="rv-seg ${cls}">${label} [${x.time_start}, ${x.time_end}] s（见证 ${x.witness_time} s）</div>`).join("");
      return `<div class="rv-pair">
        <div class="rv-pair-head">${escapeHtml(p.sortie_a)} ↔ ${escapeHtml(p.sortie_b)}</div>
        ${segHtml(p.added, "added", "🔺 新增冲突")}
        ${segHtml(p.removed, "removed", "🟢 解除冲突")}
      </div>`;
    }).join("");
  }

  box.innerHTML = `${head}
    <div>当前秒 t = ${r.t}；预测完整性：前 ${r.complete_before ? "完整" : "不完整"} → 后 ${r.complete_after ? "完整" : "不完整"}</div>
    ${firstLine}
    ${overLine}
    <div class="rv-sorties">${chips}</div>
    ${pairsHtml}`;
}

async function evaluateReview() {
  if (!state.batch) return;
  const t = parseInt($("warnT").value, 10);
  if (!Number.isInteger(t) || t < 0) {
    toast("t 必须是非负整数秒");
    return;
  }
  const nodeId = $("reviewNode").value;
  if (!nodeId) { toast("暂无可复盘节点"); return; }
  try {
    const d = await api(`/api/batches/${state.batch.batch_id}/report-nodes/${nodeId}/review?t=${t}`);
    state.review = d.review;
    renderReview();
  } catch (e) {
    state.review = null;
    renderReview();
    // Same rule as the live warning: t earlier than either side's reported
    // actual take-off rejects the whole review without a difference.
    toast("复盘被拒绝（t 早于上报的实际起飞时刻，不返回差异）：" + e.message, 4200);
  }
}

// ---------------------------------------------------------------------------
// Temporary work-zone start window
// ---------------------------------------------------------------------------

function readWorkzoneParams() {
  const t = parseInt($("warnT").value, 10);
  const p = {
    cx: parseFloat($("wzCx").value),
    cy: parseFloat($("wzCy").value),
    radius: parseFloat($("wzR").value),
    z_lo: parseFloat($("wzZlo").value),
    z_hi: parseFloat($("wzZhi").value),
    T: parseInt($("wzT").value, 10),
    d: parseInt($("wzD").value, 10),
    t,
  };
  if (!Number.isInteger(t) || t < 0) { toast("t 必须是非负整数秒"); return null; }
  if (!(p.radius > 0)) { toast("圆柱半径必须为正数"); return null; }
  if (!(p.z_lo < p.z_hi)) { toast("高度下界必须严格小于上界"); return null; }
  if (!Number.isInteger(p.T)) { toast("截止秒 T 必须是整数"); return null; }
  if (!Number.isInteger(p.d) || p.d <= 0) { toast("作业时长 d 必须是正整数"); return null; }
  if (p.T < t) { toast("截止秒 T 不能早于当前秒 t"); return null; }
  return p;
}

async function evaluateWorkzone(silent = false) {
  if (!state.batch) return;
  const p = readWorkzoneParams();
  if (!p) {
    state.workzone = null;
    renderWorkzone();
    return;
  }
  const qs = new URLSearchParams({
    cx: p.cx, cy: p.cy, radius: p.radius,
    z_lo: p.z_lo, z_hi: p.z_hi, t: p.t, T: p.T, d: p.d,
  }).toString();
  try {
    const d2 = await api(`/api/batches/${state.batch.batch_id}/workzone?${qs}`);
    state.workzone = d2.workzone;
    renderWorkzone();
  } catch (e) {
    state.workzone = null;
    renderWorkzone();
    if (!silent) toast("开工窗口查询被拒绝：" + e.message, 4200);
  }
}

function fmtIntIv([a, b]) { return a === b ? `${a}` : `[${a}, ${b}]`; }

function renderWorkzone() {
  const box = $("wzBox");
  if (!box) return;
  if (!state.workzone) { box.innerHTML = ""; return; }
  const w = state.workzone;
  const cw = w.candidate_window;
  const head = w.confirmable
    ? `<div class="wz-ok">✅ 航迹可确认（无逾期架次）</div>`
    : `<div class="wz-bad">⚠️ 不可确认：${w.overdue_sorties.map(escapeHtml).join("、")} 已逾期，航迹未知，不提供安全起点</div>`;

  let windowLine;
  if (!cw.has_candidates) {
    windowLine = `<div class="wz-note">候选开工秒范围 [${cw.start_lo}, ${cw.start_hi}] 为空（T − d &lt; t），没有候选开工秒。</div>`;
  } else if (!w.confirmable) {
    windowLine = `<div class="wz-note">候选开工秒范围为 [${cw.start_lo}, ${cw.start_hi}]，但存在逾期架次，无法给出安全起点。</div>`;
  } else {
    const safeTxt = w.safe_start_intervals.length
      ? w.safe_start_intervals.map(fmtIntIv).join("，")
      : "（空）";
    const forbTxt = w.forbidden_start_intervals.length
      ? w.forbidden_start_intervals.map(fmtIntIv).join("，")
      : "无";
    windowLine = `
      <div class="wz-note">候选开工秒范围：[${cw.start_lo}, ${cw.start_hi}]（作业闭区间 [s, s+${w.duration}] 须在 ${w.T}s 前结束）</div>
      <div class="${w.earliest_safe_start === null ? "wz-bad" : "wz-ok2"}">
        最早安全起点：${w.earliest_safe_start === null ? "（空，确无安全起点）" : `<b>${w.earliest_safe_start}</b> s`}
      </div>
      <div class="wz-safe">安全整数秒开工起点区间：${safeTxt}</div>
      <div class="wz-note">被占用排除的开工秒：${forbTxt}</div>`;
  }

  const occ = w.occupancy.filter((r) => r.occupied_intervals.length)
    .map((r) => `<div class="wc-line">${escapeHtml(r.sortie_id)}（${statusLabel(r.status)}，起飞 ${r.takeoff_offset ?? "—"}s）：` +
      r.occupied_intervals.map((iv) => `[${iv.time_start}, ${iv.time_end}]`).join("，") + " s</div>")
    .join("");
  const occHtml = occ
    ? `<div class="wc-title">航迹在圆柱内的连续时间占用（含边界/航点，已裁剪到 [${w.t}, ${w.T}]s）</div>${occ}`
    : `<div class="wz-note">[${w.t}, ${w.T}]s 内没有架次进入该圆柱。</div>`;

  box.innerHTML = `${head}${windowLine}${occHtml}`;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

$("fileInput").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    const text = await f.text();
    const payload = JSON.parse(text);
    await importScenario(payload);
    toast(`已导入 ${payload.sorties?.length ?? 0} 个架次`);
  } catch (err) {
    toast("导入失败：" + err.message, 4200);
  }
  e.target.value = "";
});

$("loadExampleBtn").addEventListener("click", async () => {
  try {
    const res = await fetch("/app/examples/crossroads.json");
    const payload = await res.json();
    await importScenario(payload);
    toast("已载入示例「十字路口」");
  } catch (err) {
    toast("示例载入失败：" + err.message);
  }
});

$("newSolveBtn").addEventListener("click", () => solve());
$("savePlanBtn").addEventListener("click", async () => {
  const name = prompt("为当前方案命名：", `方案 #${state.scenarioId}`);
  if (!name) return;
  try {
    await api(`/api/scenarios/${state.scenarioId}/save`, {
      method: "POST", body: JSON.stringify({ name }),
    });
    toast("方案已保存（含输入快照）");
    await fetchHistory();
  } catch (err) { toast("保存失败：" + err.message); }
});

$("closeResult").addEventListener("click", () => $("resultPanel").classList.add("hidden"));

$("createBatchBtn").addEventListener("click", createBatch);
$("evalWarnBtn").addEventListener("click", evaluateWarning);
$("evalWzBtn").addEventListener("click", () => evaluateWorkzone());
$("reviewBtn").addEventListener("click", evaluateReview);
$("warnT").addEventListener("keydown", (e) => { if (e.key === "Enter") evaluateWarning(); });
$("batchDel").addEventListener("click", () => {
  if (state.batch) deleteBatch(state.batch.batch_id);
});

$("timeScrub").addEventListener("input", (e) => {
  state.time = (parseFloat(e.target.value) / 100) * state.maxTime;
  $("timeLabel").textContent = `t = ${state.time.toFixed(1)} s`;
  renderMap(); renderTimeline(); renderProfile();
});

["showEarliest", "showSolved", "showConflicts"].forEach((id) => {
  $(id).addEventListener("change", () => { renderMap(); renderTimeline(); renderProfile(); });
});

["safetyRadius", "safetyHeight"].forEach((id) => {
  $(id).addEventListener("change", () => {
    if (state.scenarioId) solve(collectLocks());
  });
});
window.addEventListener("resize", () => { if (state.scenario) renderAll(); });

// init
(async function init() {
  try {
    await fetchHistory();
  } catch (e) { /* empty DB */ }
})();
