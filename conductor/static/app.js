/* Conductor web UI. Vanilla JS, no build step, works offline.
 *
 * Sections: utils · api · state · board render · detail render · review/diff
 * render · activity render · dialog · keyboard · polling · boot.
 *
 * Rule: every server-provided string goes through esc() before it reaches
 * innerHTML. Regions that hold user input (answer textarea, dialog) are static
 * DOM that is never re-rendered; other regions are re-rendered only when their
 * generated HTML actually changes, so scroll / <details> / focus survive polls.
 */
"use strict";

/* ───────────────────────── utils ───────────────────────── */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function relTime(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h " + (m % 60) + "m ago";
  return Math.floor(h / 24) + "d ago";
}

function clockTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)) + " " + u[i];
}

function fmtCost(c) {
  if (c === null || c === undefined) return "";
  return "$" + Number(c).toFixed(2);
}

function basename(p) {
  if (!p) return "";
  const parts = String(p).replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || p;
}

function runTitle(run) {
  if (run.title) return run.title;
  const t = (run.task || "").trim().split("\n")[0];
  return t.length > 70 ? t.slice(0, 67) + "…" : (t || "Untitled run");
}

/* Fill every element with data-ts with its relative time. Cheap; runs every second. */
function updateRelTimes(root = document) {
  for (const el of $$(".rel[data-ts]", root)) {
    const txt = relTime(Number(el.dataset.ts));
    if (el.textContent !== txt) el.textContent = txt;
  }
}

/* Set innerHTML only when it changed. Returns true if it re-rendered. */
function setHTML(el, html) {
  if (el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  updateRelTimes(el);
  return true;
}

/* Tiny safe markdown: escape first, then `code`, **bold**, bullets, headings, fences. */
function miniMarkdown(text) {
  const lines = String(text || "").split("\n");
  let out = [], fence = false;
  for (const raw of lines) {
    if (/^\s*```/.test(raw)) { fence = !fence; out.push('<span class="md-fence">' + esc(raw) + "</span>"); continue; }
    let line = esc(raw);
    if (fence) { out.push('<span class="md-code">' + line + "</span>"); continue; }
    line = line.replace(/`([^`]+)`/g, "<code>$1</code>")
               .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    let m;
    if ((m = line.match(/^(\s*)[-*] (.*)$/))) line = m[1] + '<span class="md-bullet">•</span> ' + m[2];
    else if ((m = line.match(/^#{1,6} (.*)$/))) line = '<strong class="md-h">' + m[1] + "</strong>";
    out.push(line);
  }
  return out.join("\n");
}

/* ───────────────────────── api ───────────────────────── */

class ApiError extends Error {
  constructor(msg, status, network) { super(msg); this.status = status; this.network = !!network; }
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("Cannot reach the conductor server", 0, true);
  }
  let data = null;
  const text = await resp.text().catch(() => "");
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
  if (!resp.ok) {
    const msg = (data && data.error) || (text && text.length < 300 ? text : "") || ("HTTP " + resp.status);
    throw new ApiError(msg, resp.status, resp.status === 502 || resp.status === 503 || resp.status === 504);
  }
  return data;
}

const API = {
  runs: () => api("GET", "/api/runs"),
  run: (id) => api("GET", "/api/runs/" + id),
  diff: (id) => api("GET", "/api/runs/" + id + "/diff"),
  log: (id, attempt) => api("GET", "/api/runs/" + id + "/log" + (attempt ? "?attempt=" + attempt : "")),
  create: (body) => api("POST", "/api/runs", body),
  action: (id, name, body) => api("POST", "/api/runs/" + id + "/" + name, body || {}),
  repo: (path) => api("GET", "/api/repo?path=" + encodeURIComponent(path)),
};

/* ───────────────────────── state ───────────────────────── */

const NEEDS_SCORE = 60;
const TERMINAL = new Set(["accepted", "rejected", "cancelled"]);
const PROGRESS = new Set(["running", "starting", "testing", "queued"]);
const ACTIVE = new Set(["running", "starting", "testing"]);

const STATE_LABEL = {
  queued: "queued", starting: "starting", running: "running", waiting_input: "waiting for you",
  testing: "testing", finished: "finished", failed: "failed", interrupted: "interrupted",
  accepted: "accepted", rejected: "rejected", cancelled: "cancelled",
};

const state = {
  runs: [],
  stats: null,
  selectedId: null,
  detail: null,          // last Run (+events) for selected run
  detailMissing: false,
  connOk: true,
  tabByRun: new Map(),   // run id -> tab the user explicitly picked
  drafts: new Map(),     // run id -> unsent answer text
  lastEventId: 0,
  diffFor: null,         // `${id}:${updated_at}` the diff view was rendered for
  diffFetchedAt: 0,
  diffLoading: false,
  logLoadedFor: null,
};

function setConnection(ok) {
  if (state.connOk === ok) return;
  state.connOk = ok;
  $("#conn-banner").hidden = ok;
  document.body.classList.toggle("disconnected", !ok);
}

function boardSections(runs) {
  const needs = [], progress = [], done = [];
  for (const r of runs) {
    if (TERMINAL.has(r.state)) done.push(r);
    else if ((r.attention_score || 0) >= NEEDS_SCORE) needs.push(r);
    else progress.push(r);   // running/starting/testing/queued (+ anything unexpected)
  }
  done.sort((a, b) => (b.finished_at || b.updated_at || 0) - (a.finished_at || a.updated_at || 0) || b.id - a.id);
  return { needs, progress, done: done.slice(0, 20) };
}

function selectRun(id, { scroll = false } = {}) {
  const hash = id ? "#run-" + id : "";
  if (location.hash !== hash) {
    if (hash) location.hash = hash; else history.replaceState(null, "", location.pathname + location.search);
  }
  applySelection(id, scroll);
}

function applySelection(id, scroll) {
  id = id ? Number(id) : null;
  if (id === state.selectedId) return;
  state.selectedId = id;
  state.detail = null;
  state.detailMissing = false;
  state.lastEventId = 0;
  state.diffFor = null;
  state.logLoadedFor = null;
  state.lastMergeCheck = null;
  resetDetailDom();
  for (const el of $$("#board .card")) el.classList.toggle("selected", Number(el.dataset.id) === id);
  if (scroll) {
    const card = $('#board .card[data-id="' + id + '"]');
    if (card) card.scrollIntoView({ block: "nearest" });
  }
  // Show cached board data immediately while the detail request is in flight.
  const fromBoard = state.runs.find((r) => r.id === id);
  if (fromBoard) renderDetail(fromBoard);
  pollDetailNow();
}

function selectionFromHash() {
  const m = location.hash.match(/^#run-(\d+)$/);
  return m ? Number(m[1]) : null;
}

/* ───────────────────────── board render ───────────────────────── */

function pill(st) {
  return '<span class="pill pill-' + esc(st) + '">' + esc(STATE_LABEL[st] || st) + "</span>";
}

function testBadge(ts) {
  if (!ts || ts === "none") return ts === "none" ? '<span class="badge badge-none">no tests</span>' : "";
  const label = { passed: "tests ✓", failed: "tests ✕", running: "testing…" }[ts] || ts;
  return '<span class="badge badge-test-' + esc(ts) + '">' + esc(label) + "</span>";
}

function diffStatHTML(ds) {
  if (!ds || (!ds.files && !ds.additions && !ds.deletions)) return "";
  return '<span class="ds"><span class="add">+' + esc(ds.additions || 0) + '</span> <span class="del">−' +
    esc(ds.deletions || 0) + "</span> · " + esc(ds.files || 0) + " file" + (ds.files === 1 ? "" : "s") + "</span>";
}

function overlapChips(overlaps) {
  if (!overlaps || !overlaps.length) return "";
  return overlaps.map((o) => {
    const files = o.files || [];
    const names = files.slice(0, 2).map(basename).join(", ") + (files.length > 2 ? " +" + (files.length - 2) : "");
    const tip = "Run #" + o.run_id + " (" + (o.state || "") + ") " + (o.title || "") + " also edits: " + files.join(", ");
    return '<span class="chip chip-overlap" title="' + esc(tip) + '">⚠ also editing ' + esc(names) + ": #" + esc(o.run_id) + "</span>";
  }).join("");
}

/* Color for an attention_reason line: green = ready to merge, red = failed, amber = needs a decision. */
function reasonTone(run) {
  if (run.state === "finished" && (run.attention_score || 0) >= 90) return "tone-good";
  if (run.state === "failed") return "tone-bad";
  if (run.state === "interrupted") return "tone-orange";
  return "tone-warn";
}

function cardHTML(run, section) {
  const parts = [];
  parts.push('<div class="c-top"><span class="c-id">#' + esc(run.id) + '</span><span class="c-title">' +
    esc(runTitle(run)) + "</span>" + pill(run.state) + "</div>");
  if (section === "needs" && run.attention_reason) {
    parts.push('<div class="c-reason ' + reasonTone(run) + '">' + esc(run.attention_reason) + "</div>");
  }
  if (run.activity && section !== "done") {
    parts.push('<div class="c-activity"><span class="act-text">' + esc(run.activity) +
      '</span><span class="rel" data-ts="' + esc(run.activity_at || "") + '"></span></div>');
  }
  const meta = [];
  meta.push('<span class="mono c-repo" title="' + esc(run.repo_path) + '">' + esc(basename(run.repo_path)) + "</span>");
  if (run.branch) meta.push('<span class="mono c-branch" title="' + esc(run.branch) + '">' + esc(run.branch) + "</span>");
  const ds = diffStatHTML(run.diff_stat); if (ds) meta.push(ds);
  const tb = run.state === "testing" ? "" : testBadge(run.test_status); if (tb) meta.push(tb);
  if (run.cost_usd) meta.push('<span class="cost">' + esc(fmtCost(run.cost_usd)) + "</span>");
  if (section === "done") meta.push('<span class="rel muted" data-ts="' + esc(run.finished_at || run.updated_at || "") + '"></span>');
  parts.push('<div class="c-meta">' + meta.join("") + "</div>");
  const chips = overlapChips(run.overlaps) + (run.stalled ? '<span class="chip chip-stalled" title="No agent output for a while">stalled</span>' : "");
  if (chips) parts.push('<div class="c-chips">' + chips + "</div>");
  return parts.join("");
}

function syncCards(container, runs, section, pool) {
  runs.forEach((run, i) => {
    let el = pool.get(run.id);
    if (!el) {
      el = document.createElement("div");
      el.className = "card";
      el.dataset.id = run.id;
      el.tabIndex = -1;
      pool.set(run.id, el);
    }
    el.className = "card st-" + run.state + (section === "needs" ? " needs" : "") +
      (run.id === state.selectedId ? " selected" : "");
    setHTML(el, cardHTML(run, section));
    if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null);
    el._seen = true;
  });
  while (container.children.length > runs.length) container.lastElementChild.remove();
}

const cardPool = new Map();

function renderBoard() {
  const { needs, progress, done } = boardSections(state.runs);
  for (const el of cardPool.values()) el._seen = false;
  syncCards($("#cards-needs"), needs, "needs", cardPool);
  syncCards($("#cards-progress"), progress, "progress", cardPool);
  syncCards($("#cards-done"), done, "done", cardPool);
  for (const [id, el] of cardPool) if (!el._seen) { el.remove(); cardPool.delete(id); }

  $("#n-needs").textContent = needs.length || "";
  $("#n-progress").textContent = progress.length || "";
  $("#n-done").textContent = done.length || "";
  $("#sec-needs").hidden = needs.length === 0;
  $("#sec-progress").hidden = progress.length === 0;
  $("#sec-done").hidden = done.length === 0;
  $("#board-empty").hidden = state.runs.length > 0;

  renderTopbar(needs.length);
  document.title = needs.length ? "(" + needs.length + ") Conductor" : "Conductor";
  notifyNewlyNeeding(needs);
}

/* ───────────────────────── reaching you ─────────────────────────
   Only one thing interrupts you: a run that has just started needing you
   (blocked on a question, ready to merge, failed...). Nothing else notifies. */

const notified = new Map(); // run id -> attention reason we already told you about
let notifyPrimed = false;   // don't fire for everything already waiting at page load

function notifyNewlyNeeding(needs) {
  const fresh = [];
  const live = new Set();
  for (const r of needs) {
    live.add(r.id);
    if (notified.get(r.id) !== r.attention_reason) {
      notified.set(r.id, r.attention_reason);
      fresh.push(r);
    }
  }
  for (const id of [...notified.keys()]) if (!live.has(id)) notified.delete(id);
  if (!notifyPrimed) { notifyPrimed = true; return; }
  if (!fresh.length || !("Notification" in window) || Notification.permission !== "granted") return;
  if (document.visibilityState === "visible" && document.hasFocus()) return; // you're already looking
  for (const r of fresh.slice(0, 3)) {
    const n = new Notification("#" + r.id + " " + r.title, {
      body: r.attention_reason + (r.state === "waiting_input" && r.question ? "\n" + r.question : ""),
      tag: "conductor-run-" + r.id,
    });
    n.onclick = () => { window.focus(); location.hash = "#run-" + r.id; n.close(); };
  }
}

function setupNotifyButton() {
  const btn = $("#notify-btn");
  if (!("Notification" in window)) return;
  const sync = () => {
    btn.hidden = Notification.permission === "granted";
    btn.disabled = Notification.permission === "denied";
    if (btn.disabled) btn.title = "Notifications are blocked for this site in your browser settings";
  };
  btn.addEventListener("click", async () => { try { await Notification.requestPermission(); } finally { sync(); } });
  sync();
}

function renderTopbar(needsN) {
  const s = state.stats || {};
  const c = s.counts || {};
  const running = (c.running || 0) + (c.starting || 0) + (c.testing || 0);
  const cap = s.max_parallel ? "/" + s.max_parallel : "";
  setHTML($("#counts"),
    '<span class="count c-running" title="Running / max parallel"><b>' + esc(running) + esc(cap) + "</b> running</span>" +
    '<span class="count c-needs' + (needsN ? " hot" : "") + '"><b>' + esc(needsN) + "</b> need you</span>" +
    '<span class="count c-queued"><b>' + esc(c.queued || 0) + "</b> queued</span>");
  setHTML($("#disk"), s.worktree_bytes === undefined ? "" :
    '<span class="mono">' + esc(fmtBytes(s.worktree_bytes)) + "</span> in " + esc(s.worktrees || 0) +
    " worktree" + (s.worktrees === 1 ? "" : "s"));
  const dl = $("#recent-repos");
  setHTML(dl, (s.recent_repos || []).map((p) => '<option value="' + esc(p) + '"></option>').join(""));
}

/* ───────────────────────── detail render ───────────────────────── */

const ACTION_LABELS = {
  accept: (r) => "Merge into " + (r.base_branch || "base"),
  accept_branch: () => "Keep branch only",
  reject: () => "Reject & clean up",
  restart: (r) => (r.state === "finished" ? "Request changes…" : "Restart"),
  cancel: () => "Stop",
  resolve_conflicts: () => "Ask agent to resolve conflicts",
  retest: () => "Re-run tests",
};
const ACTION_STYLE = { accept: "btn-success", reject: "btn-danger", cancel: "btn-danger-soft", resolve_conflicts: "btn-warn-soft" };
const ACTION_ORDER = ["accept", "accept_branch", "resolve_conflicts", "retest", "restart", "cancel", "reject"];

function resetDetailDom() {
  for (const id of ["#d-head", "#d-actions", "#rv-summary", "#rv-checks", "#rv-diff", "#tab-task", "#events", "#log-pre"]) {
    const el = $(id); el._html = undefined; el.innerHTML = "";
  }
  $("#d-action-msg").textContent = "";
  $("#q-err").textContent = "";
  $("#q-answer").value = state.selectedId ? (state.drafts.get(state.selectedId) || "") : "";
  $("#q-text")._html = undefined;
  $("#log-attempt")._html = undefined;
  $("#tab-activity")._stick = true;
  const has = !!state.selectedId;
  $("#d-empty").hidden = has;
  $("#d-main").hidden = !has;
  if (has) showTab(currentTab(null), false);
}

function defaultTab(run) {
  return run && (run.state === "finished" || run.state === "failed") ? "review" : "activity";
}

function currentTab(run) {
  return state.tabByRun.get(state.selectedId) || defaultTab(run);
}

function showTab(name, userPicked) {
  if (userPicked) state.tabByRun.set(state.selectedId, name);
  for (const b of $$("#d-tabs button")) {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
  for (const p of $$("#d-main .tab-body")) p.hidden = p.dataset.pane !== name;
  if (!state.detail) return;
  if (name === "review") maybeLoadDiff(true);
  if (name === "log" && state.logLoadedFor !== state.selectedId) loadLog();
  if (name === "activity") stickToBottom(true);
}

function renderDetail(run) {
  if (!run || run.id !== state.selectedId) return;
  const prevState = state.detail && state.detail.state;
  state.detail = run;
  renderHead(run);
  renderActions(run);
  renderQuestion(run);
  renderError(run);
  renderTask(run);
  // follow the state's default tab unless the user picked one
  if (!state.tabByRun.has(run.id)) {
    const want = defaultTab(run);
    const shown = ($("#d-tabs button.active") || {}).dataset;
    if (!shown || shown.tab !== want || prevState !== run.state) showTab(want, false);
  }
  if (run.events) renderEvents(run.events);
  renderLogAttempts(run);
  if (!$("#tab-review").hidden) { renderReviewTop(run); maybeLoadDiff(false); }
}

function metaItem(label, value, cls) {
  if (value === null || value === undefined || value === "") return "";
  return '<div class="kv"><span class="k">' + esc(label) + '</span><span class="v ' + (cls || "") + '" title="' +
    esc(value) + '">' + esc(value) + "</span></div>";
}

function renderHead(run) {
  const reason = run.attention_reason && (run.attention_score || 0) >= NEEDS_SCORE
    ? '<div class="d-reason ' + reasonTone(run) + '">' + esc(run.attention_reason) + "</div>" : "";
  const chips = overlapChips(run.overlaps) + (run.stalled ? '<span class="chip chip-stalled">stalled</span>' : "");
  const act = run.activity && !TERMINAL.has(run.state)
    ? '<div class="d-activity">' + esc(run.activity) + ' <span class="rel muted" data-ts="' + esc(run.activity_at || "") + '"></span></div>' : "";
  setHTML($("#d-head"),
    '<div class="d-title-row"><span class="d-id">#' + esc(run.id) + '</span><h1 class="d-title">' + esc(runTitle(run)) +
    "</h1>" + pill(run.state) + testBadge(run.test_status) + "</div>" + reason + act +
    (chips ? '<div class="c-chips">' + chips + "</div>" : "") +
    '<div class="d-meta">' +
      metaItem("branch", run.branch, "mono") +
      metaItem("base", run.base_branch, "mono") +
      metaItem("worktree", run.worktree_path, "mono sel") +
      metaItem("agent", run.agent + (run.model ? " · " + run.model : "")) +
      metaItem("attempt", run.attempt) +
      metaItem("cost", fmtCost(run.cost_usd || 0) + (run.budget_usd ? " / " + fmtCost(run.budget_usd) + " budget" : "")) +
      (run.result_ref ? metaItem("result", run.result_ref, "mono sel") : "") +
    "</div>");
}

function renderActions(run) {
  const acts = (run.actions || []).filter((a) => ACTION_LABELS[a]);
  acts.sort((a, b) => ACTION_ORDER.indexOf(a) - ACTION_ORDER.indexOf(b));
  setHTML($("#d-actions"), acts.map((a) =>
    '<button type="button" class="btn ' + (ACTION_STYLE[a] || "") + '" data-action="' + esc(a) + '">' +
    esc(ACTION_LABELS[a](run)) + "</button>").join(""));
}

async function doAction(name, btn) {
  const run = state.detail;
  if (!run) return;
  if (name === "reject" && !confirm("Reject run #" + run.id + "? Its branch and worktree will be deleted.")) return;
  if (name === "cancel" && !confirm("Stop run #" + run.id + "? The agent will be killed and its work discarded.")) return;
  let endpoint = name, body = {};
  if (name === "accept") body = { mode: "merge" };
  if (name === "accept_branch") { endpoint = "accept"; body = { mode: "branch" }; }
  if (name === "restart" && run.state === "finished") {
    // Feedback goes back to the same agent session, on the same branch.
    const note = prompt("What should the agent change? It resumes its session on the same branch.");
    if (!note || !note.trim()) return;
    body = { note: note.trim() };
  }
  const msg = $("#d-action-msg");
  msg.className = "action-msg"; msg.textContent = "Working…";
  $$("#d-actions button").forEach((b) => (b.disabled = true));
  try {
    const updated = await API.action(run.id, endpoint, body);
    msg.textContent = "";
    if (updated && updated.id === state.selectedId) renderDetail(Object.assign({}, state.detail, updated, { events: undefined }));
  } catch (e) {
    msg.className = "action-msg err"; msg.textContent = e.message;
  } finally {
    $$("#d-actions button").forEach((b) => (b.disabled = false));
    pollBoardNow(); pollDetailNow();
  }
}

function renderQuestion(run) {
  const box = $("#d-question");
  const show = run.state === "waiting_input";
  box.hidden = !show;
  if (show) {
    setHTML($("#q-text"), miniMarkdown(run.question || "(the agent did not include question text — see Activity)"));
    $("#q-send").disabled = !(run.actions || []).includes("answer");
  }
}

async function sendAnswer() {
  const run = state.detail, ta = $("#q-answer"), err = $("#q-err");
  if (!run) return;
  const text = ta.value.trim();
  if (!text) { err.textContent = "Write an answer first."; ta.focus(); return; }
  err.textContent = "";
  $("#q-send").disabled = true;
  try {
    const updated = await API.action(run.id, "answer", { text });
    ta.value = ""; state.drafts.delete(run.id);
    if (updated && updated.id === state.selectedId) renderDetail(Object.assign({}, state.detail, updated, { events: undefined }));
  } catch (e) {
    err.textContent = e.message;
  } finally {
    $("#q-send").disabled = false;
    pollBoardNow(); pollDetailNow();
  }
}

function renderError(run) {
  const el = $("#d-error");
  el.hidden = !run.error;
  if (run.error) setHTML(el, '<b>Error:</b> <span class="pre-wrap">' + esc(run.error) + "</span>");
}

function renderTask(run) {
  setHTML($("#tab-task"),
    '<div class="task-meta">' +
      metaItem("repo", run.repo_path, "mono sel") +
      metaItem("base commit", run.base_commit ? String(run.base_commit).slice(0, 12) : "", "mono") +
      metaItem("test cmd", run.test_cmd === null || run.test_cmd === undefined ? "(auto-detect)" : (run.test_cmd === "" ? "(none)" : run.test_cmd), "mono sel") +
      metaItem("created", run.created_at ? new Date(run.created_at * 1000).toLocaleString() : "") +
    "</div>" +
    '<h3 class="h3">Task</h3><pre class="task-text">' + esc(run.task) + "</pre>");
}

/* ───────────────────────── review / diff render ───────────────────────── */

function renderReviewTop(run, mergeCheck) {
  const summary = run.summary
    ? '<h3 class="h3">Agent summary</h3><div class="summary md">' + miniMarkdown(run.summary) + "</div>"
    : '<h3 class="h3">Agent summary</h3><div class="muted small">' + (ACTIVE.has(run.state) || run.state === "queued" ? "The agent is still working." : "No summary.") + "</div>";
  setHTML($("#rv-summary"), summary);

  const mc = mergeCheck || state.lastMergeCheck || run.merge_check;
  const base = esc(run.base_branch || "base");
  let mline;
  if (!mc) mline = '<div class="check check-pending">◌ Merge check pending</div>';
  else if (mc.conflicts && mc.conflicts.length) {
    mline = '<div class="check check-bad">✕ Conflicts with ' + base + ": " +
      mc.conflicts.map((f) => '<code>' + esc(f) + "</code>").join(", ") +
      (mc.behind ? ' <span class="muted">· behind by ' + esc(mc.behind) + " commit" + (mc.behind === 1 ? "" : "s") + "</span>" : "") + "</div>";
  } else if (mc.clean) {
    mline = '<div class="check check-ok">✓ Merges cleanly into ' + base +
      (mc.behind ? ' <span class="muted">· behind by ' + esc(mc.behind) + " commit" + (mc.behind === 1 ? "" : "s") + "</span>" : "") + "</div>";
  } else mline = '<div class="check check-pending">◌ Merge status unknown</div>';

  const ts = run.test_status;
  let tline;
  if (!ts) tline = '<div class="check check-pending">◌ Tests not run yet</div>';
  else if (ts === "none") tline = '<div class="check check-pending">– No test command</div>';
  else if (ts === "running") tline = '<div class="check check-running">▶ Tests running…</div>';
  else if (ts === "passed") tline = '<div class="check check-ok">✓ Tests passed' + (run.test_cmd ? ' <code>' + esc(run.test_cmd) + "</code>" : "") + "</div>";
  else tline = '<div class="check check-bad">✕ Tests failed' + (run.test_cmd ? ' <code>' + esc(run.test_cmd) + "</code>" : "") + "</div>";
  const tout = run.test_output
    ? '<details class="test-out" data-key="test-out"' + (ts === "failed" ? " open" : "") + '><summary>Test output</summary><pre>' + esc(run.test_output) + "</pre></details>"
    : "";
  const el = $("#rv-checks");
  const open = keepOpen(el);
  if (setHTML(el, '<div class="checks">' + mline + tline + "</div>" + tout)) restoreOpen(el, open);
}

function keepOpen(root) {
  const m = new Map();
  for (const d of $$("details[data-key]", root)) m.set(d.dataset.key, d.open);
  return m;
}
function restoreOpen(root, m) {
  for (const d of $$("details[data-key]", root)) if (m.has(d.dataset.key)) d.open = m.get(d.dataset.key);
}

async function maybeLoadDiff(force) {
  const run = state.detail;
  if (!run || state.diffLoading) return;
  const key = run.id + ":" + run.updated_at;
  if (state.diffFor === key) return;
  // While the agent is actively editing, updated_at moves a lot: throttle.
  if (!force && ACTIVE.has(run.state) && Date.now() - state.diffFetchedAt < 5000 && state.diffFor && state.diffFor.startsWith(run.id + ":")) return;
  state.diffLoading = true;
  const id = run.id;
  if (!state.diffFor || !state.diffFor.startsWith(id + ":")) setHTML($("#rv-diff"), '<div class="muted small pad">Loading diff…</div>');
  try {
    const d = await API.diff(id);
    if (id !== state.selectedId) return;
    state.diffFor = key; state.diffFetchedAt = Date.now();
    state.lastMergeCheck = d && d.merge_check;
    renderReviewTop(state.detail, d && d.merge_check);
    renderDiff(d || {});
  } catch (e) {
    if (id !== state.selectedId) return;
    state.diffFor = key; state.diffFetchedAt = Date.now();
    setHTML($("#rv-diff"), '<div class="muted small pad">Diff unavailable: ' + esc(e.message) + "</div>");
  } finally {
    state.diffLoading = false;
  }
}

/* Parse a unified diff into files → hunks → lines. */
function parseDiff(text) {
  const files = [];
  let f = null, h = null, oldN = 0, newN = 0;
  const lines = String(text || "").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  const unq = (p) => p.replace(/^"(.*)"$/, "$1");
  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      const m = line.match(/^diff --git (?:"?a\/)(.+?)"? (?:"?b\/)(.+?)"?$/);
      f = { path: m ? m[2] : line.slice(11), oldPath: m ? m[1] : null, meta: [], hunks: [], binary: false, nlines: 0 };
      files.push(f); h = null; continue;
    }
    if (!f) continue;
    if (!h) {
      if (line.startsWith("+++ ")) { const p = unq(line.slice(4)); if (p !== "/dev/null") f.path = p.replace(/^b\//, ""); continue; }
      if (line.startsWith("--- ")) { const p = unq(line.slice(4)); if (p !== "/dev/null") f.oldPath = p.replace(/^a\//, ""); continue; }
      if (line.startsWith("Binary files") || line.startsWith("GIT binary patch")) { f.binary = true; f.meta.push(line); continue; }
    }
    const hm = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/);
    if (hm) {
      h = { header: line, lines: [] }; f.hunks.push(h);
      oldN = Number(hm[1]); newN = Number(hm[2]); f.nlines++; continue;
    }
    if (!h) { f.meta.push(line); continue; }
    const c = line[0];
    if (c === "+") h.lines.push({ t: "add", o: "", n: newN++, s: line.slice(1) });
    else if (c === "-") h.lines.push({ t: "del", o: oldN++, n: "", s: line.slice(1) });
    else if (c === "\\") h.lines.push({ t: "meta", o: "", n: "", s: line });
    else h.lines.push({ t: "ctx", o: oldN++, n: newN++, s: line.slice(1) });
    f.nlines++;
  }
  return files;
}

const LARGE_FILE_LINES = 400;

function fileBodyHTML(f) {
  if (f.binary) return '<div class="muted small pad">Binary file</div>';
  if (!f.hunks.length) return '<div class="muted small pad">' + (f.meta.length ? esc(f.meta.filter((l) => !/^index /.test(l)).join("\n")) || "No content changes" : "No content changes") + "</div>";
  const rows = [];
  for (const h of f.hunks) {
    rows.push('<tr class="dl-hunk"><td class="ln"></td><td class="ln"></td><td class="code">' + esc(h.header) + "</td></tr>");
    for (const l of h.lines) {
      const sign = l.t === "add" ? "+" : l.t === "del" ? "-" : l.t === "meta" ? "" : " ";
      rows.push('<tr class="dl-' + l.t + '"><td class="ln">' + l.o + '</td><td class="ln">' + l.n +
        '</td><td class="code"><span class="sign">' + sign + "</span>" + esc(l.s) + "</td></tr>");
    }
  }
  return '<div class="dscroll"><table class="dtable"><tbody>' + rows.join("") + "</tbody></table></div>";
}

function statusLetter(st) {
  if (!st) return "M";
  const s = String(st);
  return (s[0] || "M").toUpperCase();
}

function renderDiff(d) {
  const root = $("#rv-diff");
  const parsed = parseDiff(d.diff);
  const byPath = new Map(parsed.map((f) => [f.path, f]));
  const list = (d.files && d.files.length ? d.files : parsed.map((f) => {
    let a = 0, del = 0;
    f.hunks.forEach((h) => h.lines.forEach((l) => { if (l.t === "add") a++; else if (l.t === "del") del++; }));
    return { path: f.path, status: "M", additions: a, deletions: del };
  }));
  // Files present in the diff but missing from the list (shouldn't happen) still render.
  for (const f of parsed) if (!list.find((x) => x.path === f.path)) list.push({ path: f.path, status: "M", additions: 0, deletions: 0 });

  // preserve open/closed state + scroll across re-renders
  const prevOpen = new Map();
  for (const det of $$("details.dfile", root)) prevOpen.set(det.dataset.path, det.open);
  const pane = $("#tab-review"), scrollTop = pane.scrollTop;

  if (!list.length) {
    setHTML(root, '<h3 class="h3">Changes</h3><div class="muted small pad">No changes yet.</div>');
    return;
  }
  let totA = 0, totD = 0;
  list.forEach((x) => { totA += x.additions || 0; totD += x.deletions || 0; });
  const head = '<h3 class="h3">Changes <span class="muted small">' + list.length + " file" + (list.length === 1 ? "" : "s") +
    ' · <span class="add">+' + totA + '</span> <span class="del">−' + totD + "</span></span>" +
    '<span class="spacer"></span><button type="button" class="btn btn-sm" data-diff="expand">Expand all</button>' +
    '<button type="button" class="btn btn-sm" data-diff="collapse">Collapse all</button></h3>';
  const fl = '<ul class="flist">' + list.map((x, i) =>
    '<li><a href="#" data-jump="' + i + '"><span class="st st-' + esc(statusLetter(x.status)) + '">' + esc(statusLetter(x.status)) +
    '</span><span class="mono fpath">' + esc(x.path) + '</span><span class="fstat"><span class="add">+' + esc(x.additions || 0) +
    '</span> <span class="del">−' + esc(x.deletions || 0) + "</span></span></a></li>").join("") + "</ul>";

  const blocks = list.map((x, i) => {
    const f = byPath.get(x.path) || (x.old_path && byPath.get(x.old_path)) || { path: x.path, hunks: [], meta: [], binary: false, nlines: 0 };
    const large = f.nlines > LARGE_FILE_LINES;
    const open = prevOpen.has(x.path) ? prevOpen.get(x.path) : !large;
    const rename = f.oldPath && f.oldPath !== f.path ? '<span class="muted"> ← ' + esc(f.oldPath) + "</span>" : "";
    return '<details class="dfile" id="dfile-' + i + '" data-idx="' + i + '" data-path="' + esc(x.path) + '"' + (open ? " open" : "") + ">" +
      '<summary><span class="st st-' + esc(statusLetter(x.status)) + '">' + esc(statusLetter(x.status)) + '</span><span class="mono fpath">' +
      esc(x.path) + "</span>" + rename + '<span class="fstat"><span class="add">+' + esc(x.additions || 0) + '</span> <span class="del">−' +
      esc(x.deletions || 0) + "</span>" + (large ? ' <span class="muted">· ' + f.nlines + " lines</span>" : "") + "</span></summary>" +
      '<div class="dbody">' + (open ? fileBodyHTML(f) : "") + "</div></details>";
  });
  root._files = list.map((x) => byPath.get(x.path) || (x.old_path && byPath.get(x.old_path)) || { path: x.path, hunks: [], meta: [], binary: false, nlines: 0 });
  root._html = undefined;
  root.innerHTML = head + fl + '<div class="dfiles">' + blocks.join("") + "</div>";
  pane.scrollTop = scrollTop;
}

/* lazily render bodies of collapsed (large) files on first open */
function ensureFileBody(det) {
  const body = $(".dbody", det);
  if (body && !body.firstChild) {
    const f = ($("#rv-diff")._files || [])[Number(det.dataset.idx)];
    if (f) body.innerHTML = fileBodyHTML(f);
  }
}

/* ───────────────────────── activity render ───────────────────────── */

const KIND_ICON = { state: "◆", activity: "›", tool: "⚙", text: "¶", question: "?", answer: "↩", result: "✓", test: "▶", error: "✕", info: "i" };

function eventHTML(ev) {
  const kind = KIND_ICON[ev.kind] ? ev.kind : "info";
  const msg = String(ev.message || "");
  const isText = kind === "text" || kind === "question" || kind === "answer" || kind === "result";
  const long = isText && (msg.length > 220 || msg.split("\n").length > 3);
  const body = isText
    ? '<div class="ev-text' + (long ? " clamp" : "") + '"' + (long ? ' title="Click to expand"' : "") + ">" + miniMarkdown(msg) + "</div>"
    : '<span class="ev-msg">' + esc(msg) + "</span>";
  return '<span class="ev-icon k-' + kind + '" title="' + esc(ev.kind) + '">' + KIND_ICON[kind] + "</span>" +
    '<span class="ev-time" title="' + esc(new Date((ev.ts || 0) * 1000).toLocaleString()) + '">' + esc(clockTime(ev.ts)) +
    (ev.attempt > 1 ? ' <span class="ev-att">a' + esc(ev.attempt) + "</span>" : "") + "</span>" + body;
}

function renderEvents(events) {
  const list = $("#events"), pane = $("#tab-activity");
  const atBottom = pane.hidden ? pane._stick !== false : pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
  const frag = document.createDocumentFragment();
  let added = 0;
  for (const ev of events) {
    if (ev.id <= state.lastEventId) continue;
    const li = document.createElement("li");
    li.className = "ev ev-" + (KIND_ICON[ev.kind] ? ev.kind : "info");
    li.innerHTML = eventHTML(ev);
    frag.appendChild(li);
    state.lastEventId = ev.id; added++;
  }
  if (!added) {
    if (!list.children.length) list.innerHTML = '<li class="ev-empty muted">No events yet.</li>';
    return;
  }
  const empty = $(".ev-empty", list); if (empty) empty.remove();
  list.appendChild(frag);
  // keep DOM bounded
  while (list.children.length > 1000) list.firstElementChild.remove();
  if (atBottom) stickToBottom(true);
}

function stickToBottom(force) {
  const pane = $("#tab-activity");
  if (force && !pane.hidden) { pane.scrollTop = pane.scrollHeight; pane._stick = true; }
}

/* ───────────────────────── raw log ───────────────────────── */

function renderLogAttempts(run) {
  const sel = $("#log-attempt");
  const n = Math.max(1, run.attempt || 1);
  const cur = sel.value;
  const html = Array.from({ length: n }, (_, i) => '<option value="' + (i + 1) + '">' + (i + 1) + "</option>").join("");
  if (setHTML(sel, html)) sel.value = cur && Number(cur) <= n ? cur : String(n);
}

async function loadLog() {
  const id = state.selectedId;
  if (!id) return;
  const pre = $("#log-pre"), info = $("#log-info");
  info.textContent = "Loading…";
  state.logLoadedFor = id;
  try {
    const d = await API.log(id, $("#log-attempt").value || undefined);
    if (id !== state.selectedId) return;
    const lines = (d && d.lines) || [];
    pre.textContent = lines.length ? lines.join("\n") : "(empty)";
    info.textContent = lines.length + " line" + (lines.length === 1 ? "" : "s") + " · loaded " + clockTime(Date.now() / 1000);
  } catch (e) {
    if (id !== state.selectedId) return;
    pre.textContent = "";
    info.textContent = "Could not load log: " + e.message;
  }
}

/* ───────────────────────── dialog ───────────────────────── */

const LS_REPO = "conductor.lastRepo", LS_AGENT = "conductor.lastAgent";
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: fine */ } }

let repoCheckSeq = 0;

function openDialog() {
  const dlg = $("#new-dialog");
  if (dlg.open) return;
  $("#f-err").textContent = "";
  const repo = $("#f-repo");
  if (!repo.value) repo.value = lsGet(LS_REPO) || ((state.stats && state.stats.recent_repos) || [])[0] || "";
  const agent = lsGet(LS_AGENT); if (agent) $("#f-agent").value = agent;
  dlg.showModal();
  if (repo.value) { checkRepo(); $("#f-task").focus(); } else repo.focus();
}

function closeDialog() { const d = $("#new-dialog"); if (d.open) d.close(); }

async function checkRepo() {
  const path = $("#f-repo").value.trim();
  const status = $("#f-repo-status"), sel = $("#f-base"), test = $("#f-test");
  if (!path) { status.textContent = ""; status.className = "hint"; return; }
  if (sel.dataset.path === path) return;
  const seq = ++repoCheckSeq;
  status.className = "hint"; status.textContent = "Checking…";
  try {
    const r = await API.repo(path);
    if (seq !== repoCheckSeq) return;
    sel.dataset.path = path;
    const branches = r.branches || [];
    const prev = sel.value;
    sel.innerHTML = branches.map((b) => '<option value="' + esc(b) + '">' + esc(b) + "</option>").join("") ||
      '<option value="">(no branches)</option>';
    sel.value = branches.includes(prev) && prev !== "" && sel._userPicked ? prev : (r.current_branch || branches[0] || "");
    test.placeholder = r.test_cmd_guess ? "auto: " + r.test_cmd_guess : "auto-detect (nothing found)";
    status.className = "hint ok";
    status.textContent = "✓ git repo" + (r.root && r.root !== path ? " at " + r.root : "") + (r.current_branch ? " · on " + r.current_branch : "");
  } catch (e) {
    if (seq !== repoCheckSeq) return;
    delete sel.dataset.path;
    sel.innerHTML = '<option value="">(enter a valid repo)</option>';
    test.placeholder = "auto-detect";
    status.className = "hint err";
    status.textContent = e.message;
  }
}

async function submitDialog() {
  const err = $("#f-err"), btn = $("#f-submit");
  const repo = $("#f-repo").value.trim(), task = $("#f-task").value.trim();
  if (!repo) { err.textContent = "Repository path is required."; $("#f-repo").focus(); return; }
  if (!task) { err.textContent = "Describe the task."; $("#f-task").focus(); return; }
  const body = { repo_path: repo, task, agent: $("#f-agent").value };
  const title = $("#f-title").value.trim(); if (title) body.title = title;
  const base = $("#f-base").value; if (base) body.base_branch = base;
  const model = $("#f-model").value.trim(); if (model) body.model = model;
  if ($("#f-notest").checked) body.test_cmd = "";
  else { const t = $("#f-test").value.trim(); if (t) body.test_cmd = t; }
  const budget = $("#f-budget").value;
  if (budget !== "" && !isNaN(Number(budget))) body.budget_usd = Number(budget);
  err.textContent = ""; btn.disabled = true; btn.textContent = "Starting…";
  try {
    const run = await API.create(body);
    lsSet(LS_REPO, repo); lsSet(LS_AGENT, body.agent);
    $("#f-task").value = ""; $("#f-title").value = "";
    closeDialog();
    if (run && run.id) {
      // put it on the board right away so selection has something to show
      if (!state.runs.find((r) => r.id === run.id)) { state.runs.push(run); renderBoard(); }
      selectRun(run.id, { scroll: true });
    }
    pollBoardNow();
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = "Start run";
  }
}

/* ───────────────────────── keyboard ───────────────────────── */

function isTyping(e) {
  const t = e.target;
  if ($("#new-dialog").open) return true;
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

function moveSelection(delta) {
  const cards = $$("#board .card").filter((c) => c.offsetParent !== null);
  if (!cards.length) return;
  let i = cards.findIndex((c) => Number(c.dataset.id) === state.selectedId);
  i = i < 0 ? (delta > 0 ? 0 : cards.length - 1) : Math.min(cards.length - 1, Math.max(0, i + delta));
  selectRun(Number(cards[i].dataset.id), { scroll: true });
}

function onKey(e) {
  if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e)) return;
  if (e.key === "j" || e.key === "ArrowDown" && e.target === document.body) { e.preventDefault(); moveSelection(1); }
  else if (e.key === "k" || e.key === "ArrowUp" && e.target === document.body) { e.preventDefault(); moveSelection(-1); }
  else if (e.key === "n") { e.preventDefault(); openDialog(); }
  else if (e.key === "Escape" && state.selectedId) selectRun(null);
}

/* ───────────────────────── polling ───────────────────────── */

const BOARD_MS = 1500, DETAIL_MS = 2000;
let boardTimer = null, detailTimer = null, boardBusy = false, detailBusy = false;

async function pollBoard() {
  clearTimeout(boardTimer);
  if (boardBusy) return;
  boardBusy = true;
  try {
    const d = await API.runs();
    setConnection(true);
    state.runs = (d && d.runs) || [];
    state.stats = (d && d.stats) || state.stats;
    renderBoard();
  } catch (e) {
    if (e.network) setConnection(false);
  } finally {
    boardBusy = false;
    boardTimer = setTimeout(pollBoard, state.connOk ? BOARD_MS : 2500);
  }
}

async function pollDetail() {
  clearTimeout(detailTimer);
  const id = state.selectedId;
  if (!id) return;
  if (detailBusy) { detailTimer = setTimeout(pollDetail, 300); return; }
  detailBusy = true;
  try {
    const run = await API.run(id);
    if (id === state.selectedId) { state.detailMissing = false; renderDetail(run); }
  } catch (e) {
    if (e.network) setConnection(false);
    else if (e.status === 404 && id === state.selectedId) {
      state.detailMissing = true;
      setHTML($("#d-head"), '<div class="d-title-row"><span class="d-id">#' + esc(id) + '</span><h1 class="d-title muted">Run not found</h1></div>');
      setHTML($("#d-actions"), "");
    }
  } finally {
    detailBusy = false;
    if (id === state.selectedId) detailTimer = setTimeout(pollDetail, DETAIL_MS);
  }
}

function pollBoardNow() { clearTimeout(boardTimer); boardTimer = setTimeout(pollBoard, 0); }
function pollDetailNow() { clearTimeout(detailTimer); detailTimer = setTimeout(pollDetail, 0); }

/* ───────────────────────── boot / events ───────────────────────── */

function wire() {
  $("#board").addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (card) selectRun(Number(card.dataset.id));
  });
  $("#d-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) showTab(b.dataset.tab, true);
  });
  $("#d-actions").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-action]");
    if (b && !b.disabled) doAction(b.dataset.action, b);
  });
  $("#q-send").addEventListener("click", sendAnswer);
  $("#q-answer").addEventListener("input", (e) => { if (state.selectedId) state.drafts.set(state.selectedId, e.target.value); });
  $("#q-answer").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendAnswer(); }
  });

  // review tab: file jumps, expand/collapse, lazy bodies
  $("#rv-diff").addEventListener("click", (e) => {
    const a = e.target.closest("[data-jump]");
    if (a) {
      e.preventDefault();
      const det = $("#dfile-" + a.dataset.jump);
      if (det) { det.open = true; ensureFileBody(det); det.scrollIntoView({ block: "start", behavior: "smooth" }); }
      return;
    }
    const b = e.target.closest("[data-diff]");
    if (b) for (const det of $$("details.dfile", $("#rv-diff"))) { det.open = b.dataset.diff === "expand"; if (det.open) ensureFileBody(det); }
  });
  $("#rv-diff").addEventListener("toggle", (e) => {
    if (e.target.matches && e.target.matches("details.dfile") && e.target.open) ensureFileBody(e.target);
  }, true);

  // activity: expand clamped text; track whether user scrolled up
  $("#events").addEventListener("click", (e) => {
    const t = e.target.closest(".ev-text.clamp, .ev-text.expanded");
    if (t && !window.getSelection().toString()) { t.classList.toggle("clamp"); t.classList.toggle("expanded"); }
  });
  $("#tab-activity").addEventListener("scroll", (e) => {
    const p = e.target; p._stick = p.scrollHeight - p.scrollTop - p.clientHeight < 40;
  });

  $("#log-refresh").addEventListener("click", loadLog);
  $("#log-attempt").addEventListener("change", loadLog);

  // dialog
  $("#new-run-btn").addEventListener("click", openDialog);
  setupNotifyButton();
  $("#f-cancel").addEventListener("click", closeDialog);
  $("#f-repo").addEventListener("blur", checkRepo);
  $("#f-repo").addEventListener("change", checkRepo);
  $("#f-base").addEventListener("change", (e) => { e.target._userPicked = true; });
  $("#f-notest").addEventListener("change", (e) => { $("#f-test").disabled = e.target.checked; });
  $("#new-form").addEventListener("submit", (e) => { e.preventDefault(); submitDialog(); });
  $("#new-dialog").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitDialog(); }
  });
  $("#new-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeDialog(); });

  document.addEventListener("keydown", onKey);
  window.addEventListener("hashchange", () => applySelection(selectionFromHash(), true));
  setInterval(() => updateRelTimes(), 1000);
}

function boot() {
  wire();
  resetDetailDom();
  const id = selectionFromHash();
  if (id) applySelection(id, true);
  pollBoard();
}

boot();
