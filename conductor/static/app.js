/* Conductor web UI. Vanilla JS, no build step, works offline.
 *
 * Sections: utils · api · state · board render (next3 strip, groups, paged
 * sections) · reaching you · detail render · review/diff render · activity
 * render · raw log · policy panel · override dialog · toasts · group bulk ops ·
 * audit · workflows · schedules · dialog · keyboard · polling · boot.
 *
 * The board talks to the paginated API (view=needs_you|active|done, offset
 * paging) and only polls the pages that are currently on screen; Done and
 * collapsed groups are not fetched at all.
 *
 * Rule: every server-provided string goes through esc() before it reaches
 * innerHTML. Regions that hold user input (answer textarea, dialogs) are static
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

/* "waiting 12m": how long a run has been sitting in its attention tier. */
function waitTime(ts) {
  if (!ts) return "";
  const m = Math.floor(Math.max(0, Date.now() / 1000 - ts) / 60);
  if (m < 1) return "waiting <1m";
  if (m < 60) return "waiting " + m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return "waiting " + h + "h " + (m % 60) + "m";
  return "waiting " + Math.floor(h / 24) + "d " + (h % 24) + "h";
}

function plural(n, word, many) {
  return n + " " + (n === 1 ? word : (many || word + "s"));
}

/* Date+time for log-like lists: clock time today, short date otherwise. */
function whenTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (d.toDateString() === new Date().toDateString()) return clockTime(ts);
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/* A policy rule value or audit detail value as short text. */
function fmtValue(v) {
  if (v === null || v === undefined) return "";
  if (v === true) return "yes";
  if (v === false) return "no";
  if (Array.isArray(v)) return v.map(fmtValue).join(", ");
  if (typeof v === "object") return Object.entries(v).map(([k, x]) => k + ": " + fmtValue(x)).join(" · ");
  return String(v);
}

function clip(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
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
  for (const el of $$(".wait[data-ts]", root)) {
    const txt = waitTime(Number(el.dataset.ts));
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
  constructor(msg, status, network, data) {
    super(msg); this.status = status; this.network = !!network; this.data = data || null;
  }
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
    throw new ApiError(msg, resp.status, resp.status === 502 || resp.status === 503 || resp.status === 504, data);
  }
  return data;
}

/* Query string from an object, skipping empty values. */
function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") p.set(k, v);
  const s = p.toString();
  return s ? "?" + s : "";
}

/* group keys look like "batch:7f3a…" / "schedule:3": keep the colon readable in the path. */
function pathKey(k) { return encodeURIComponent(k).replace(/%3A/gi, ":"); }

const API = {
  runs: (params) => api("GET", "/api/runs" + qs(params)),
  run: (id) => api("GET", "/api/runs/" + id),
  diff: (id) => api("GET", "/api/runs/" + id + "/diff"),
  log: (id, attempt) => api("GET", "/api/runs/" + id + "/log" + (attempt ? "?attempt=" + attempt : "")),
  create: (body) => api("POST", "/api/runs", body),
  batch: (body) => api("POST", "/api/batches", body),
  action: (id, name, body) => api("POST", "/api/runs/" + id + "/" + name, body || {}),
  groupOp: (key, op) => api("POST", "/api/groups/" + pathKey(key) + "/" + op, {}),
  repo: (path) => api("GET", "/api/repo?path=" + encodeURIComponent(path)),
  policy: (repo) => api("GET", "/api/policy" + qs({ repo })),
  audit: (params) => api("GET", "/api/audit" + qs(params)),
  workflows: (repo) => api("GET", "/api/workflows" + qs({ repo })),
  schedules: () => api("GET", "/api/schedules"),
  createSchedule: (body) => api("POST", "/api/schedules", body),
  scheduleOp: (id, op) => api("POST", "/api/schedules/" + id + "/" + op, {}),
};

/* ───────────────────────── state ───────────────────────── */

const NEEDS_SCORE = 60;
const PAGE = 50;            // runs per page, per section / group
const GROUPS_SHOWN = 8;     // group rows before "Show all groups"
const TERMINAL = new Set(["accepted", "rejected", "cancelled"]);
const ACTIVE = new Set(["running", "starting", "testing"]);
const POLICY_HARD = new Set(["kill", "block", "approval"]);

const STATE_LABEL = {
  queued: "queued", starting: "starting", running: "running", waiting_input: "waiting for you",
  testing: "testing", finished: "finished", failed: "failed", interrupted: "interrupted",
  accepted: "accepted", rejected: "rejected", cancelled: "cancelled",
};

/* One paged list on the board: a section, or an expanded group. */
function newList() { return { pages: 1, runs: [], total: null, more: false, loaded: false }; }

const state = {
  stats: null,
  known: new Map(),      // run id -> latest Run seen in any list (instant detail on select)
  lists: { needs: newList(), progress: newList(), done: newList() },
  next3: [],
  groups: [],            // Group objects from the needs_you response
  groupLists: new Map(), // group_key -> list, only while expanded
  groupsOpen: new Set(),
  groupsShowAll: false,
  pendingGroups: new Map(), // group_key -> placeholder Group from a batch, until the server lists it
  flashGroup: null,
  doneOpen: false,
  filter: { q: "", repo: "" },
  boardGen: 0,           // bumped on filter change; stale responses are dropped
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
  policyCache: new Map(), // repo path -> GET /api/policy response, or {error}
  effPolicyOpen: false,
  runAudit: [], auditFor: null, auditFetchedAt: 0,
  scrollToPolicy: false,
  dialogCap: null,       // budget cap from the new-run dialog's repo policy
};

function setConnection(ok) {
  if (state.connOk === ok) return;
  state.connOk = ok;
  $("#conn-banner").hidden = ok;
  document.body.classList.toggle("disconnected", !ok);
}

function filterActive() { return !!(state.filter.q || state.filter.repo); }

function remember(runs) { for (const r of runs || []) if (r && r.id) state.known.set(r.id, r); }

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
  state.runAudit = []; state.auditFor = null; state.auditFetchedAt = 0;
  resetDetailDom();
  for (const el of $$("#board .card, #n3-cards .n3-card")) el.classList.toggle("selected", Number(el.dataset.id) === id);
  if (scroll) {
    const card = $('#board .card[data-id="' + id + '"]');
    if (card) card.scrollIntoView({ block: "nearest" });
  }
  // Show cached board data immediately while the detail request is in flight.
  const fromBoard = state.known.get(id);
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
  if (overlaps.length > 1) {
    // many runs touching the same files: one chip, details in the tooltip
    const files = [...new Set(overlaps.flatMap((o) => o.files || []))];
    const names = files.slice(0, 2).map(basename).join(", ") + (files.length > 2 ? " +" + (files.length - 2) : "");
    const ids = overlaps.slice(0, 3).map((o) => "#" + o.run_id).join(" ") + (overlaps.length > 3 ? " +" + (overlaps.length - 3) : "");
    const tip = overlaps.map((o) => "Run #" + o.run_id + " (" + (o.state || "") + ") " + (o.title || "") + ": " + (o.files || []).join(", ")).join("\n");
    return '<span class="chip chip-overlap" title="' + esc(tip) + '">⚠ also editing ' + esc(names) + ": " + esc(ids) + "</span>";
  }
  return overlaps.map((o) => {
    const files = o.files || [];
    const names = files.slice(0, 2).map(basename).join(", ") + (files.length > 2 ? " +" + (files.length - 2) : "");
    const tip = "Run #" + o.run_id + " (" + (o.state || "") + ") " + (o.title || "") + " also edits: " + files.join(", ");
    return '<span class="chip chip-overlap" title="' + esc(tip) + '">⚠ also editing ' + esc(names) + ": #" + esc(o.run_id) + "</span>";
  }).join("");
}

/* Findings that stop the agent, block the merge or need an override: live
   violations plus pre-merge findings from merge_check.policy. */
function hardViolations(run) {
  const mc = run.merge_check || {};
  return [...(run.violations || []), ...(mc.policy || [])].filter((v) => v && POLICY_HARD.has(v.action));
}

function policyChip(run) {
  const hard = hardViolations(run);
  if (!hard.length) return "";
  const tip = hard.map((v) => (POLICY_BADGE[v.action] || [v.action])[0] + ": " + v.rule + " — " + (v.detail || "")).join("\n");
  return '<span class="chip chip-policy" title="' + esc(tip) + '">policy</span>';
}

function runChips(run) {
  return policyChip(run) + overlapChips(run.overlaps) +
    (run.stalled ? '<span class="chip chip-stalled" title="No agent output for a while">stalled</span>' : "");
}

/* Color for an attention_reason line: green = ready to merge, red = failed / policy, amber = needs a decision. */
const POLICY_REASON = /^(Policy violation|Stopped by policy|Needs approval)\b/;

function reasonTone(run) {
  const reason = run.attention_reason || "";
  if (/^Needs approval\b/.test(reason)) return "tone-warn";
  if (POLICY_REASON.test(reason) || run.attention_score === 95) return "tone-bad";
  if (run.state !== "waiting_input" && hardViolations(run).length) return "tone-bad";
  if (run.state === "finished" && ((run.attention_score || 0) >= 90 || (run.actions || []).includes("accept"))) return "tone-good";
  if (run.state === "failed") return "tone-bad";
  if (run.state === "interrupted") return "tone-orange";
  return "tone-warn";
}

/* The attention reason to show; a workflow gate says which step it would start. */
function reasonText(run) {
  if (isWorkflowGate(run)) {
    const next = wfSteps(run)[(run.workflow_step || 0) + 1];
    if (next) return "Approve next step: " + next.id;
  }
  return run.attention_reason || "";
}

function needsYou(run) { return !TERMINAL.has(run.state) && (run.attention_score || 0) >= NEEDS_SCORE; }

function cardHTML(run, section) {
  const parts = [];
  parts.push('<div class="c-top"><span class="c-id">#' + esc(run.id) + '</span><span class="c-title">' +
    esc(runTitle(run)) + "</span>" + pill(run.state) + "</div>");
  if ((section === "needs" || (section === "group" && needsYou(run))) && run.attention_reason) {
    parts.push('<div class="c-reason ' + reasonTone(run) + '">' + esc(reasonText(run)) + "</div>");
  }
  if (run.activity && section !== "done" && !TERMINAL.has(run.state)) {
    parts.push('<div class="c-activity"><span class="act-text">' + esc(run.activity) +
      '</span><span class="rel" data-ts="' + esc(run.activity_at || "") + '"></span></div>');
  }
  const meta = [];
  meta.push('<span class="mono c-repo" title="' + esc(run.repo_path) + '">' + esc(basename(run.repo_path)) + "</span>");
  if (run.branch && !run.workflow) meta.push('<span class="mono c-branch" title="' + esc(run.branch) + '">' + esc(run.branch) + "</span>");
  const wf = workflowStepHTML(run); if (wf) meta.push(wf);
  const ds = diffStatHTML(run.diff_stat); if (ds) meta.push(ds);
  const tb = run.state === "testing" ? "" : testBadge(run.test_status); if (tb) meta.push(tb);
  if (run.cost_usd) meta.push('<span class="cost">' + esc(fmtCost(run.cost_usd)) + "</span>");
  if (section === "done" || TERMINAL.has(run.state)) meta.push('<span class="rel muted" data-ts="' + esc(run.finished_at || run.updated_at || "") + '"></span>');
  parts.push('<div class="c-meta">' + meta.join("") + "</div>");
  const chips = runChips(run);
  if (chips) parts.push('<div class="c-chips">' + chips + "</div>");
  return parts.join("");
}

/* Keyed card list: each container keeps its own id -> element pool, so a run
   can show in a section and in an expanded group at the same time. */
function syncCards(container, runs, section) {
  const pool = container._pool || new Map();
  const next = new Map();
  runs.forEach((run, i) => {
    let el = pool.get(run.id);
    if (!el) {
      el = document.createElement("div");
      el.dataset.id = run.id;
      el.tabIndex = -1;
    }
    next.set(run.id, el);
    el.className = "card st-" + run.state + (section === "needs" || (section === "group" && needsYou(run)) ? " needs" : "") +
      (run.id === state.selectedId ? " selected" : "");
    setHTML(el, cardHTML(run, section));
    if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null);
  });
  while (container.children.length > runs.length) container.lastElementChild.remove();
  container._pool = next;
  container._html = undefined;   // an empty-state message set via setHTML is gone now
}

function moreButton(btn, list) {
  btn.hidden = !list.more;
  if (list.more) setHTML(btn, "Show more <span class=\"muted\">· " + esc(list.runs.length) + " of " + esc(list.total) + " shown</span>");
}

function doneCountFromStats() {
  const c = (state.stats && state.stats.counts) || {};
  return (c.accepted || 0) + (c.rejected || 0) + (c.cancelled || 0);
}

function renderBoard() {
  const L = state.lists;
  renderNext3();
  renderGroups();
  syncCards($("#cards-needs"), L.needs.runs, "needs");
  syncCards($("#cards-progress"), L.progress.runs, "progress");
  syncCards($("#cards-done"), L.done.runs, "done");
  moreButton($('[data-more="needs"]'), L.needs);
  moreButton($('[data-more="progress"]'), L.progress);
  moreButton($('[data-more="done"]'), L.done);

  const doneN = L.done.loaded ? L.done.total : (filterActive() ? null : doneCountFromStats());
  $("#n-needs").textContent = L.needs.total || "";
  $("#n-progress").textContent = L.progress.total || "";
  $("#n-done").textContent = doneN || "";
  $("#sec-needs").hidden = !L.needs.runs.length;
  $("#sec-progress").hidden = !L.progress.runs.length;
  $("#sec-done").hidden = doneN === 0 && !state.doneOpen;
  if (state.doneOpen && L.done.loaded && !L.done.runs.length) setHTML($("#cards-done"), '<div class="muted small pad">Nothing here.</div>');

  const loaded = L.needs.loaded && L.progress.loaded;
  const nothing = loaded && !L.needs.runs.length && !L.progress.runs.length && !state.groups.length &&
    (L.done.loaded ? !L.done.runs.length : doneN === 0 || doneN === null);
  const empty = $("#board-empty");
  empty.hidden = !nothing;
  if (nothing) {
    setHTML(empty, filterActive()
      ? '<p>No runs match.</p><p class="muted"><button type="button" class="btn btn-sm" data-clear-filter>Clear filter</button></p>'
      : '<p>No runs yet.</p><p class="muted">Press <kbd>n</kbd> or click <b>New run</b> to start an agent.</p>');
  }
  renderFilterNote();

  const needsN = L.needs.total || 0;
  renderTopbar(needsN);
  document.title = needsN ? "(" + needsN + ") Conductor" : "Conductor";
  if (!filterActive()) notifyNewlyNeeding(L.needs.runs);
}

function renderFilterNote() {
  const f = state.filter, el = $("#filter-note");
  el.hidden = !filterActive();
  if (!filterActive()) return;
  const bits = [];
  if (f.q) bits.push("matching <b>" + esc(f.q) + "</b>");
  if (f.repo) bits.push('in <span class="mono" title="' + esc(f.repo) + '">' + esc(basename(f.repo)) + "</span>");
  setHTML(el, "Showing runs " + bits.join(" ") + ' <button type="button" class="btn-link" data-clear-filter>clear</button>');
}

/* ── "Look at these next" strip ── */

/* The single button a next3 card offers, matching what the run is waiting on. */
function primaryAction(run) {
  const acts = run.actions || [];
  if (isWorkflowGate(run)) return { kind: "answer", label: "Review step", cls: "btn-warn" };
  if (run.state === "waiting_input") return { kind: "answer", label: "Answer", cls: "btn-warn" };
  if (POLICY_REASON.test(run.attention_reason || "") || hardViolations(run).length || acts.includes("accept_override")) {
    return { kind: "review", label: "Review", cls: reasonTone(run) === "tone-warn" ? "btn-warn-soft" : "btn-danger" };
  }
  if (acts.includes("accept")) return { kind: "merge", label: "Merge", cls: "btn-success" };
  return { kind: "open", label: "Open", cls: "" };
}

function next3HTML(run) {
  const pa = primaryAction(run);
  const tone = reasonTone(run).replace("tone-", "t-");
  return '<div class="n3-card ' + tone + (run.id === state.selectedId ? " selected" : "") + '" data-id="' + esc(run.id) + '">' +
    '<div class="n3-reason ' + reasonTone(run) + '" title="' + esc(reasonText(run)) + '">' + esc(reasonText(run) || STATE_LABEL[run.state] || run.state) + "</div>" +
    '<div class="n3-title"><span class="c-id">#' + esc(run.id) + '</span> <span title="' + esc(runTitle(run)) + '">' + esc(runTitle(run)) + "</span></div>" +
    '<div class="n3-foot"><span class="mono c-repo" title="' + esc(run.repo_path) + '">' + esc(basename(run.repo_path)) + "</span>" +
    (run.group_title ? '<span class="n3-group" title="' + esc(run.group_title) + '">' + esc(run.group_title) + "</span>" : "") +
    '<span class="wait" data-ts="' + esc(run.attention_since || run.updated_at || "") + '"></span>' +
    '<span class="spacer"></span><button type="button" class="btn btn-sm ' + pa.cls + '" data-n3="' + pa.kind + '" data-id="' + esc(run.id) + '">' +
    esc(pa.label) + "</button></div></div>";
}

function renderNext3() {
  const runs = (state.next3 || []).slice(0, 3);
  $("#next3").hidden = !runs.length;
  setHTML($("#n3-cards"), runs.map(next3HTML).join(""));
}

async function onNext3(kind, id, btn) {
  const run = state.known.get(id);
  if (kind === "merge") {
    btn.disabled = true; btn.textContent = "Merging…";
    try {
      await API.action(id, "accept", { mode: "merge" });
      toast("Merged <b>#" + esc(id) + "</b> " + esc(run ? runTitle(run) : "") + " into " + esc((run && run.base_branch) || "base"), "ok");
    } catch (e) {
      toast("Could not merge #" + esc(id) + ": " + esc(e.message), "err");
      selectRun(id, { scroll: true }); showTab("review", true);
    } finally {
      pollBoardNow(); if (state.selectedId === id) pollDetailNow();
    }
    return;
  }
  selectRun(id, { scroll: true });
  if (kind === "answer") {
    requestAnimationFrame(() => { const ta = $("#q-answer"); if (!$("#d-question").hidden) ta.focus(); });
  } else if (kind === "review") {
    state.scrollToPolicy = true;
    showTab("review", true);
    renderPolicy();
  }
}

/* ── groups ── */

function allGroups() {
  const listed = new Set(state.groups.map((g) => g.group_key));
  for (const k of [...state.pendingGroups.keys()]) if (listed.has(k)) state.pendingGroups.delete(k);
  return [...state.pendingGroups.values(), ...state.groups];
}

function findGroup(key) { return allGroups().find((g) => g.group_key === key); }

function groupCountsHTML(g) {
  const parts = [];
  if (g.ready) parts.push('<span class="g-ready">' + esc(g.ready) + " ready</span>");
  if (g.failed) parts.push('<span class="g-failed">' + esc(g.failed) + " failing</span>");
  if (g.needs_you) parts.push('<span class="g-needs">' + esc(g.needs_you) + (g.needs_you === 1 ? " needs" : " need") + " you</span>");
  if (g.active) parts.push('<span class="g-active">' + esc(g.active) + " active</span>");
  parts.push("<span>" + esc(g.total || 0) + " total</span>");
  return parts.join('<span class="g-dot">·</span>');
}

/* Interrupted runs aren't counted in Group; count the loaded runs when the whole group is on screen. */
function groupInterrupted(g) {
  if (typeof g.interrupted === "number") return g.interrupted;
  const l = state.groupLists.get(g.group_key);
  if (!l || !l.loaded || l.more) return null;
  return l.runs.filter((r) => r.state === "interrupted").length;
}

function groupBulkHTML(g) {
  const intr = groupInterrupted(g);
  return '<button type="button" class="btn btn-sm btn-success" data-gop="accept_ready"' + (g.ready ? "" : " disabled") +
      ">Merge all ready" + (g.ready ? " (" + esc(g.ready) + ")" : "") + "</button>" +
    '<button type="button" class="btn btn-sm btn-danger" data-gop="reject_failed"' + (g.failed ? "" : " disabled") +
      ">Reject all failed" + (g.failed ? " (" + esc(g.failed) + ")" : "") + "</button>" +
    '<button type="button" class="btn btn-sm" data-gop="restart_interrupted"' + (intr === 0 ? " disabled" : "") +
      ">Restart interrupted" + (intr ? " (" + esc(intr) + ")" : "") + "</button>";
}

function groupEl(g, pool) {
  let el = pool.get(g.group_key);
  if (el) return el;
  el = document.createElement("div");
  el.className = "group";
  el.dataset.key = g.group_key;
  el.innerHTML = '<div class="g-head" data-gtoggle role="button" tabindex="0"></div>' +
    '<div class="g-body" hidden><div class="g-bulk"></div><div class="cards"></div>' +
    '<button type="button" class="btn btn-sm more" data-gmore hidden></button></div>';
  return el;
}

function renderGroups() {
  const box = $("#groups");
  const groups = allGroups();
  const shown = state.groupsShowAll ? groups : groups.slice(0, GROUPS_SHOWN);
  const pool = box._pool || new Map(), next = new Map();
  shown.forEach((g, i) => {
    const el = groupEl(g, pool);
    next.set(g.group_key, el);
    const open = state.groupsOpen.has(g.group_key);
    el.classList.toggle("open", open);
    const head = el.children[0], body = el.children[1];
    head.setAttribute("aria-expanded", open ? "true" : "false");
    setHTML(head, '<span class="g-caret">' + (open ? "▾" : "▸") + '</span><span class="g-title" title="' + esc(g.group_key) + '">' +
      esc(g.title || g.group_key) + '</span><span class="g-counts">' + groupCountsHTML(g) + "</span>");
    body.hidden = !open;
    if (open) {
      const l = state.groupLists.get(g.group_key) || newList();
      setHTML(body.children[0], groupBulkHTML(g));
      if (l.loaded && l.runs.length) syncCards(body.children[1], l.runs, "group");
      else if (l.loaded) { syncCards(body.children[1], [], "group"); setHTML(body.children[1], '<div class="muted small pad">' + (filterActive() ? "No runs match the filter." : "No runs listed yet.") + "</div>"); }
      else if (!body.children[1].children.length) body.children[1].innerHTML = '<div class="muted small pad">Loading…</div>';
      moreButton(body.children[2], l);
    }
    if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
    if (state.flashGroup === g.group_key) {
      state.flashGroup = null;
      el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
      el.scrollIntoView({ block: "nearest" });
    }
  });
  while (box.children.length > shown.length) box.lastElementChild.remove();
  box._pool = next;
  $("#sec-groups").hidden = !groups.length;
  $("#n-groups").textContent = groups.length || "";
  const all = $("#groups-all");
  all.hidden = groups.length <= GROUPS_SHOWN;
  all.textContent = state.groupsShowAll ? "Show fewer groups" : "Show all " + groups.length + " groups";
}

function toggleGroup(key) {
  if (state.groupsOpen.has(key)) { state.groupsOpen.delete(key); state.groupLists.delete(key); }
  else { state.groupsOpen.add(key); state.groupLists.set(key, newList()); pollBoardNow(); }
  renderGroups();
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
  const repos = s.recent_repos || [];
  setHTML($("#recent-repos"), repos.map((p) => '<option value="' + esc(p) + '"></option>').join(""));
  renderRepoFilter(repos);
}

/* Repo filter <select>: basename labels, disambiguated by parent dir when two repos share a name. */
function renderRepoFilter(repos) {
  const sel = $("#repo-filter"), cur = state.filter.repo;
  const list = repos.slice();
  if (cur && !list.includes(cur)) list.unshift(cur);
  const names = list.map(basename);
  const label = (p, i) => names.indexOf(names[i]) !== names.lastIndexOf(names[i])
    ? String(p).replace(/\/+$/, "").split("/").slice(-2).join("/") : names[i];
  const html = '<option value="">All repos</option>' +
    list.map((p, i) => '<option value="' + esc(p) + '" title="' + esc(p) + '">' + esc(label(p, i)) + "</option>").join("");
  if (setHTML(sel, html)) sel.value = cur;
}

/* ───────────────────────── detail render ───────────────────────── */

const ACTION_LABELS = {
  accept: (r) => "Merge into " + (r.base_branch || "base"),
  accept_branch: () => "Keep branch only",
  reject: () => "Reject & clean up",
  restart: (r) => (r.state === "finished" ? "Request changes…" : "Restart"),
  cancel: () => "Stop",
  accept_override: () => "Merge with override…",
  resolve_conflicts: () => "Ask agent to resolve conflicts",
  retest: () => "Re-run tests",
};
const ACTION_STYLE = { accept: "btn-success", accept_override: "btn-warn-soft", reject: "btn-danger", cancel: "btn-danger-soft", resolve_conflicts: "btn-warn-soft" };
const ACTION_ORDER = ["accept", "accept_override", "accept_branch", "resolve_conflicts", "retest", "restart", "cancel", "reject"];

function resetDetailDom() {
  for (const id of ["#d-head", "#d-actions", "#rv-summary", "#rv-checks", "#rv-policy", "#rv-diff", "#tab-task", "#events", "#log-pre"]) {
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
  if (name === "review") { renderReviewTop(state.detail); maybeLoadDiff(true); maybeLoadRunAudit(); }
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
  if (!$("#tab-review").hidden) { renderReviewTop(run); maybeLoadDiff(false); maybeLoadRunAudit(); }
}

function metaItem(label, value, cls) {
  if (value === null || value === undefined || value === "") return "";
  return '<div class="kv"><span class="k">' + esc(label) + '</span><span class="v ' + (cls || "") + '" title="' +
    esc(value) + '">' + esc(value) + "</span></div>";
}

function renderHead(run) {
  const reason = run.attention_reason && (run.attention_score || 0) >= NEEDS_SCORE
    ? '<div class="d-reason ' + reasonTone(run) + '">' + esc(reasonText(run)) + "</div>" : "";
  const chips = runChips(run);
  const act = run.activity && !TERMINAL.has(run.state)
    ? '<div class="d-activity">' + esc(run.activity) + ' <span class="rel muted" data-ts="' + esc(run.activity_at || "") + '"></span></div>' : "";
  setHTML($("#d-head"),
    '<div class="d-title-row"><span class="d-id">#' + esc(run.id) + '</span><h1 class="d-title">' + esc(runTitle(run)) +
    "</h1>" + pill(run.state) + testBadge(run.test_status) + "</div>" + reason + act +
    (chips ? '<div class="c-chips">' + chips + "</div>" : "") +
    workflowTrackerHTML(run) +
    '<div class="d-meta">' +
      metaItem("branch", run.branch, "mono") +
      metaItem("base", run.base_branch, "mono") +
      metaItem("worktree", run.worktree_path, "mono sel") +
      metaItem("agent", run.agent + (run.model ? " · " + run.model : "")) +
      metaItem("attempt", run.attempt) +
      (run.group_title ? metaItem("group", run.group_title) : "") +
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
  if (name === "accept_override") { openOverrideDialog(run); return; }
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
    const gate = isWorkflowGate(run);
    box.classList.toggle("gate", gate);
    $("#q-label").textContent = gate ? "Approve the next step" : "The agent is asking you";
    $("#q-answer").placeholder = gate
      ? "Optional: instructions for the next step… (Ctrl/Cmd+Enter to send)"
      : "Type your answer… (Ctrl/Cmd+Enter to send)";
    $("#q-approve").hidden = !gate;
    $("#q-send").textContent = gate ? "Send instructions" : "Send answer";
    $("#q-send").className = "btn " + (gate ? "" : "btn-warn");
    setHTML($("#q-text"), miniMarkdown(run.question || "(the agent did not include question text — see Activity)"));
    const can = (run.actions || []).includes("answer");
    $("#q-send").disabled = !can;
    $("#q-approve").disabled = !can;
  }
}

/* text: send this instead of the textarea (the gate's "Approve" button sends "approve"). */
async function sendAnswer(text) {
  const run = state.detail, ta = $("#q-answer"), err = $("#q-err");
  if (!run) return;
  const fromBox = typeof text !== "string";
  if (fromBox) text = ta.value.trim();
  if (!text) { err.textContent = isWorkflowGate(run) ? "Write instructions first, or click Approve." : "Write an answer first."; ta.focus(); return; }
  err.textContent = "";
  $("#q-send").disabled = true;
  try {
    const updated = await API.action(run.id, "answer", { text });
    if (fromBox) { ta.value = ""; state.drafts.delete(run.id); }
    if (updated && updated.id === state.selectedId) renderDetail(Object.assign({}, state.detail, updated, { events: undefined }));
  } catch (e) {
    err.textContent = e.message;
  } finally {
    $("#q-send").disabled = false; $("#q-approve").disabled = false;
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
  renderPolicy();
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
    (ev.attempt > 1 ? ' <span class="ev-att" title="attempt ' + esc(ev.attempt) + '">attempt ' + esc(ev.attempt) + "</span>" : "") + "</span>" + body;
}

function renderEvents(events) {
  const list = $("#events"), pane = $("#tab-activity");
  const atBottom = pane.hidden ? pane._stick !== false : pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;
  const frag = document.createDocumentFragment();
  let added = 0;
  for (const ev of events) {
    if (ev.id <= state.lastEventId) continue;
    const li = document.createElement("li");
    li.className = "ev ev-" + (KIND_ICON[ev.kind] ? ev.kind : "info") +
      (/^Workflow (step\b|"[^"]*" complete|stopped)/.test(ev.message || "") ? " ev-wfstep" : "");
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

/* ───────────────────────── policy panel ───────────────────────── */

const POLICY_BADGE = {
  kill: ["stopped", "pb-red"], block: ["blocks merge", "pb-red"],
  approval: ["needs approval", "pb-amber"], flag: ["flag", "pb-gray"],
};
const POLICY_RANK = { kill: 0, block: 1, approval: 2, flag: 3 };

function policyBadge(action) {
  const b = POLICY_BADGE[action] || [action || "?", "pb-gray"];
  return '<span class="pbadge ' + b[1] + '">' + esc(b[0]) + "</span>";
}

/* run.violations (live) + merge_check.policy (from the diff), de-duplicated
   (a file caught live is in the diff too: shown once, as the live finding),
   worst first. */
function policyFindings(run, mc) {
  const out = [], seen = new Set();
  const add = (v, src) => {
    if (!v) return;
    const k = v.path ? v.rule + "|path|" + v.path : v.rule + "|" + v.detail + "|" + v.action;
    if (seen.has(k)) return;
    seen.add(k); out.push(Object.assign({ src }, v));
  };
  (run.violations || []).forEach((v) => add(v, (v.how ? v.how + " · " : "") +
    (v.at ? "seen " + relTime(v.at) : "while running")));
  ((mc && mc.policy) || []).forEach((v) => add(v, "pre-merge check"));
  out.sort((a, b) => (POLICY_RANK[a.action] ?? 9) - (POLICY_RANK[b.action] ?? 9));
  return out;
}

function findingsListHTML(list) {
  return '<ul class="pol-list">' + list.map((v) =>
    '<li class="pol-item">' + policyBadge(v.action) + '<span class="mono pol-rule">' + esc(v.rule) + "</span>" +
    '<span class="pol-detail">' + esc(v.detail) + "</span>" +
    (v.src ? '<span class="pol-src muted">' + esc(v.src) + "</span>" : "") + "</li>").join("") + "</ul>";
}

function rulesHTML(p) {
  if (!p) return '<div class="muted small pad">Loading…</div>';
  if (p.error) return '<div class="err small pad">Policy unavailable: ' + esc(p.error) + "</div>";
  const rules = p.rules || [];
  const body = rules.length
    ? '<dl class="rules">' + rules.map((r) => "<dt class=\"mono\">" + esc(r.rule) + "</dt><dd>" + esc(fmtValue(r.value)) + "</dd>").join("") + "</dl>"
    : '<div class="muted small">No rules: defaults only.</div>';
  const src = (p.sources || []).length
    ? '<div class="muted small rules-src">from ' + p.sources.map((s) => '<code>' + esc(s) + "</code>").join(", ") +
      (p.sha ? " · sha <code>" + esc(String(p.sha).slice(0, 12)) + "</code>" : "") + "</div>" : "";
  return body + src;
}

function auditTableHTML(entries, withRun) {
  return '<table class="atable"><thead><tr><th>Time</th><th>Actor</th>' + (withRun ? "<th>Run</th>" : "") +
    "<th>Action</th><th>Detail</th></tr></thead><tbody>" + entries.map((e) => {
      const detail = fmtValue(e.detail);
      return "<tr><td class=\"mono nowrap\" title=\"" + esc(new Date((e.ts || 0) * 1000).toLocaleString()) + "\">" + esc(whenTime(e.ts)) + "</td>" +
        "<td>" + esc(e.actor) + "</td>" +
        (withRun ? "<td>" + (e.run_id ? '<a href="#run-' + esc(e.run_id) + '" data-audit-run="' + esc(e.run_id) + '" class="mono">#' + esc(e.run_id) + "</a>" : '<span class="muted">–</span>') + "</td>" : "") +
        '<td><span class="a-action mono">' + esc(e.action) + "</span></td>" +
        '<td class="a-detail" title="' + esc(detail) + '">' + esc(clip(detail, 180)) + "</td></tr>";
    }).join("") + "</tbody></table>";
}

function renderPolicy() {
  const run = state.detail, el = $("#rv-policy");
  if (!run) return;
  const mc = state.lastMergeCheck || run.merge_check;
  const list = policyFindings(run, mc);
  const override = (run.actions || []).includes("accept_override");
  let html = '<h3 class="h3">Policy' + (run.policy_sha ? ' <span class="muted small">sha <code>' + esc(String(run.policy_sha).slice(0, 12)) + "</code></span>" : "") + "</h3>";
  html += list.length ? findingsListHTML(list) : '<div class="check check-ok">✓ No policy findings</div>';
  if (override) {
    html += '<div class="pol-override"><span>Policy blocks a plain merge. An override needs a reason and is recorded in the audit log.</span>' +
      '<button type="button" class="btn btn-sm btn-warn-soft" data-override>Merge with override…</button></div>';
  }
  const pol = state.policyCache.get(run.repo_path);
  html += '<details class="eff-pol" data-key="eff-policy"' + (state.effPolicyOpen ? " open" : "") + "><summary>Effective policy " +
    '<span class="muted">for <span class="mono">' + esc(basename(run.repo_path)) + "</span></span></summary>" +
    (state.effPolicyOpen ? rulesHTML(pol) : "") + "</details>";
  if (state.runAudit.length) {
    html += '<h3 class="h3">Audit <span class="muted small">' + esc(plural(state.runAudit.length, "entry", "entries")) + "</span></h3>" +
      auditTableHTML(state.runAudit, false);
  }
  setHTML(el, html);
  if (state.effPolicyOpen && pol === undefined) loadPolicy(run.repo_path);
  if (state.scrollToPolicy) { state.scrollToPolicy = false; requestAnimationFrame(() => el.scrollIntoView({ block: "start" })); }
}

const policyLoading = new Set();
async function loadPolicy(repo, force) {
  if (!repo || policyLoading.has(repo) || (!force && state.policyCache.has(repo))) return state.policyCache.get(repo);
  policyLoading.add(repo);
  try {
    state.policyCache.set(repo, await API.policy(repo));
  } catch (e) {
    if (e.network) { policyLoading.delete(repo); return undefined; }
    state.policyCache.set(repo, { error: e.message });
  } finally {
    policyLoading.delete(repo);
  }
  if (state.detail && state.detail.repo_path === repo) renderPolicy();
  return state.policyCache.get(repo);
}

/* This run's audit entries; refreshed when the run changes, at most every 5s. */
async function maybeLoadRunAudit() {
  const run = state.detail;
  if (!run || $("#tab-review").hidden) return;
  const key = run.id + ":" + run.updated_at;
  if (state.auditFor === key || Date.now() - state.auditFetchedAt < 5000) return;
  state.auditFor = key; state.auditFetchedAt = Date.now();
  const id = run.id;
  try {
    const d = await API.audit({ run_id: id, limit: 50 });
    if (id !== state.selectedId) return;
    state.runAudit = (d && d.entries) || [];
    renderPolicy();
  } catch (e) { /* audit is optional context */ }
}

/* ───────────────────────── override dialog ───────────────────────── */

function openOverrideDialog(run) {
  const dlg = $("#override-dialog");
  dlg._runId = run.id;
  $("#ov-run").textContent = "#" + run.id + " " + runTitle(run) + " → " + (run.base_branch || "base");
  const list = policyFindings(run, state.lastMergeCheck || run.merge_check).filter((v) => v.action !== "flag");
  setHTML($("#ov-findings"), list.length ? findingsListHTML(list) : "");
  $("#ov-reason").value = "";
  setHTML($("#ov-err"), "");
  $("#ov-submit").disabled = false; $("#ov-submit").textContent = "Merge with override";
  dlg.showModal();
  $("#ov-reason").focus();
}

async function submitOverride() {
  const dlg = $("#override-dialog"), id = dlg._runId, ta = $("#ov-reason"), btn = $("#ov-submit");
  const reason = ta.value.trim();
  if (!reason) { setHTML($("#ov-err"), "Write why this override is OK — it goes in the audit log."); ta.focus(); return; }
  setHTML($("#ov-err"), "");
  btn.disabled = true; btn.textContent = "Merging…";
  try {
    const updated = await API.action(id, "accept", { mode: "merge", override_reason: reason });
    dlg.close();
    toast("Merged <b>#" + esc(id) + "</b> with a policy override.", "ok");
    if (updated && updated.id === state.selectedId) renderDetail(Object.assign({}, state.detail, updated, { events: undefined }));
    state.auditFetchedAt = 0;
  } catch (e) {
    // 409 carries the current violations: show those as the findings, the message below
    const vs = e.data && (e.data.violations || e.data.policy);
    if (e.status === 409 && Array.isArray(vs) && vs.length) setHTML($("#ov-findings"), findingsListHTML(vs));
    setHTML($("#ov-err"), esc(e.message));
  } finally {
    btn.disabled = false; btn.textContent = "Merge with override";
    pollBoardNow(); pollDetailNow();
  }
}

/* ───────────────────────── toasts ───────────────────────── */

/* html must already be escaped. kind: ok | warn | err | info. */
function toast(html, kind, ms) {
  const el = document.createElement("div");
  el.className = "toast toast-" + (kind || "info");
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  el.innerHTML = '<div class="toast-body">' + html + '</div><button type="button" class="toast-x" aria-label="Dismiss">×</button>';
  const box = $("#toasts");
  box.appendChild(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  const kill = () => el.remove();
  el.querySelector(".toast-x").addEventListener("click", kill);
  let t = setTimeout(kill, ms || (kind === "ok" ? 5000 : 12000));
  el.addEventListener("mouseenter", () => clearTimeout(t));
  el.addEventListener("mouseleave", () => { t = setTimeout(kill, 4000); });
}

/* ───────────────────────── group bulk ops ───────────────────────── */

const GROUP_OPS = {
  accept_ready: { verb: "Merged", ask: (g, t) => "Merge all " + plural(g.ready || 0, "ready run") + " in “" + t + "” into their base branches?\n\nRuns that no longer merge cleanly or are blocked by policy are skipped." },
  reject_failed: { verb: "Rejected", ask: (g, t) => "Reject all " + plural(g.failed || 0, "failed run") + " in “" + t + "”?\n\nTheir branches and worktrees will be deleted." },
  restart_interrupted: {
    verb: "Restarted",
    ask: (g, t) => { const n = groupInterrupted(g); return "Restart " + (n === null ? "all interrupted runs" : plural(n, "interrupted run")) + " in “" + t + "”?"; },
  },
};

async function groupOp(key, op, btn) {
  const g = findGroup(key), spec = GROUP_OPS[op];
  if (!g || !spec) return;
  const title = g.title || key;
  if (!confirm(spec.ask(g, title))) return;
  const bulk = btn.closest(".g-bulk");
  $$("button", bulk).forEach((b) => (b.disabled = true));
  const label = btn.textContent; btn.textContent = "Working…";
  try {
    const res = (await API.groupOp(key, op)) || {};
    const done = res.done || [], skipped = res.skipped || [];
    let html = "<b>" + esc(spec.verb) + " " + esc(done.length) + "</b>" + (skipped.length ? " · skipped " + esc(skipped.length) : "") +
      ' <span class="muted">in ' + esc(title) + "</span>";
    if (skipped.length) {
      html += '<ul class="toast-list">' + skipped.slice(0, 8).map((s) =>
        '<li><a href="#run-' + esc(s.id) + '" class="mono">#' + esc(s.id) + "</a> " + esc(s.reason) + "</li>").join("") +
        (skipped.length > 8 ? '<li class="muted">and ' + esc(skipped.length - 8) + " more</li>" : "") + "</ul>";
    }
    toast(html, skipped.length ? "warn" : "ok");
  } catch (e) {
    toast("Bulk action failed: " + esc(e.message), "err");
  } finally {
    btn.textContent = label;
    bulk._html = undefined;   // re-render buttons (and their disabled state) on the next board render
    pollBoardNow(); if (state.selectedId) pollDetailNow();
  }
}

/* ───────────────────────── audit ───────────────────────── */

async function openAudit() {
  const dlg = $("#audit-dialog");
  if (!dlg.open) dlg.showModal();
  await loadAudit();
}

async function loadAudit() {
  const body = $("#audit-body"), info = $("#audit-info");
  info.textContent = "Loading…";
  try {
    const d = await API.audit({ limit: 100 });
    const entries = (d && d.entries) || [];
    setHTML(body, entries.length ? auditTableHTML(entries, true) : '<div class="muted pad">No audit entries yet.</div>');
    info.textContent = plural(entries.length, "entry", "entries") + (entries.length >= 100 ? " (latest 100)" : "") + " · loaded " + clockTime(Date.now() / 1000);
  } catch (e) {
    info.textContent = "";
    setHTML(body, '<div class="err pad">Could not load the audit log: ' + esc(e.message) + "</div>");
  }
}

/* ───────────────────────── workflows ───────────────────────── */

function wfSteps(run) {
  return (run.workflow_def && run.workflow_def.steps) || [];
}

/* Waiting at a workflow approval gate (not an agent question). */
function isWorkflowGate(run) {
  return !!run && run.state === "waiting_input" && !!run.workflow && /^Step /.test(run.question || "");
}

/* Card line: "step 2 of 4 · add-tests". */
function workflowStepHTML(run) {
  const steps = wfSteps(run);
  if (!run.workflow || !steps.length) return "";
  const i = Math.min(run.workflow_step || 0, steps.length - 1);
  const gate = isWorkflowGate(run);
  const txt = gate && steps[i + 1]
    ? "step " + (i + 1) + " of " + steps.length + " done · approve " + steps[i + 1].id
    : "step " + (i + 1) + " of " + steps.length + " · " + steps[i].id;
  return '<span class="wf-step" title="Workflow ' + esc(run.workflow) + '">' + esc(txt) + "</span>";
}

/* Per step: done | current | next (awaiting approval) | failed | pending. */
function workflowStepStates(run) {
  const steps = wfSteps(run), n = steps.length;
  const i = Math.min(run.workflow_step || 0, Math.max(0, n - 1));
  const finishedOk = run.state === "finished" || run.state === "accepted";
  const stopped = ["failed", "interrupted", "rejected", "cancelled"].includes(run.state);
  return steps.map((s, k) => {
    if (k < i) return "done";
    if (k === i) {
      if (isWorkflowGate(run) || finishedOk) return "done";
      return stopped ? "failed" : "current";
    }
    if (k === i + 1 && isWorkflowGate(run)) return "next";
    return "pending";
  });
}

const WF_MARK = { done: "✓", current: "●", next: "?", failed: "✕", pending: "" };

function workflowTrackerHTML(run) {
  const steps = wfSteps(run);
  if (!run.workflow || !steps.length) return "";
  const st = workflowStepStates(run);
  const label = { done: "done", current: "in progress", next: "awaiting your approval", failed: "stopped here", pending: "pending" };
  return '<div class="wf-track"><span class="wf-name" title="' + esc((run.workflow_def && run.workflow_def.description) || "") + '">' +
    "Workflow <b>" + esc(run.workflow) + "</b></span><ol>" + steps.map((s, k) =>
      '<li class="wf-' + st[k] + '" title="Step ' + (k + 1) + ": " + esc(s.id) + " — " + label[st[k]] +
      (s.gate === "approve" ? " · needs approval to continue" : "") + '"><span class="wf-dot">' + (WF_MARK[st[k]] || (k + 1)) + "</span>" +
      '<span class="wf-id">' + esc(s.id) + "</span>" + (s.gate === "approve" ? '<span class="wf-gate" title="Approval gate after this step">gate</span>' : "") +
      "</li>").join("") + "</ol></div>";
}

/* Compact numbered step list for the new-run / new-schedule forms. */
function workflowStepsListHTML(wf) {
  if (!wf) return "";
  const steps = wf.steps || (wf.step_ids || []).map((id) => ({ id }));
  return (wf.description ? '<div class="muted small">' + esc(wf.description) + "</div>" : "") +
    '<ol class="wf-list">' + steps.map((s) =>
      '<li><span class="mono">' + esc(s.id) + "</span>" +
      (s.gate === "approve" ? ' <span class="pbadge pb-amber">needs approval</span>' : "") +
      (s.tests === "skip" ? ' <span class="pbadge pb-gray">tests skipped</span>' : "") + "</li>").join("") + "</ol>";
}

const workflowCache = new Map();   // repo path -> [Workflow] | {error}

async function loadWorkflows(repo) {
  try {
    const d = await API.workflows(repo);
    const list = (d && d.workflows) || [];
    workflowCache.set(repo, list);
    return list;
  } catch (e) {
    return { error: e.message };
  }
}

/* Fill a workflow <select> (keeps the current choice when it still exists) and its step list. */
function fillWorkflowSelect(sel, stepsBox, list) {
  const cur = sel.value;
  if (!Array.isArray(list)) {
    sel.innerHTML = '<option value="">None — single task</option>';
    setHTML(stepsBox, list && list.error ? '<div class="err small">Workflows unavailable: ' + esc(list.error) + "</div>" : "");
    return;
  }
  sel.innerHTML = '<option value="">None — single task</option>' + list.map((w) =>
    '<option value="' + esc(w.name) + '">' + esc(w.name) + " (" + esc(plural((w.steps || w.step_ids || []).length, "step")) + ")</option>").join("");
  sel._list = list;
  sel.value = list.some((w) => w.name === cur) ? cur : "";
  showWorkflowSteps(sel, stepsBox);
}

function showWorkflowSteps(sel, stepsBox) {
  const wf = (sel._list || []).find((w) => w.name === sel.value);
  stepsBox.hidden = !wf;
  setHTML(stepsBox, workflowStepsListHTML(wf));
}

/* ───────────────────────── schedules ───────────────────────── */

function relFuture(ts) {
  if (!ts) return "";
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return "due now";
  if (s < 60) return "in " + s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return "in " + m + "m";
  const h = Math.floor(m / 60);
  if (h < 24) return "in " + h + "h " + (m % 60) + "m";
  return "in " + Math.floor(h / 24) + "d " + (h % 24) + "h";
}

function utcTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

const schedState = { list: [], timer: null, repoSeq: 0 };

function scheduleRowHTML(sc) {
  const trig = sc.kind === "cron"
    ? '<span class="sc-kind">cron</span> <code>' + esc(sc.cron) + "</code>"
    : '<span class="sc-kind">on change</span> <code>' + esc(sc.watch_ref || sc.base_branch || "") + "</code>" +
      (sc.poll_secs ? ' <span class="muted">every ' + esc(sc.poll_secs) + "s</span>" : "");
  const next = sc.enabled && sc.next_fire_at
    ? '<div>' + esc(relFuture(sc.next_fire_at)) + '</div><div class="muted mono small">' + esc(utcTime(sc.next_fire_at)) + "</div>"
    : '<span class="muted">' + (sc.enabled ? "–" : "paused") + "</span>";
  const last = sc.last_fired_at
    ? '<div class="rel" data-ts="' + esc(sc.last_fired_at) + '"></div><div class="muted mono small">' + esc(utcTime(sc.last_fired_at)) + "</div>"
    : '<span class="muted">never</span>';
  const run = sc.last_run_id ? '<a href="#run-' + esc(sc.last_run_id) + '" data-sc-run="' + esc(sc.last_run_id) + '" class="mono">#' + esc(sc.last_run_id) + "</a>" : '<span class="muted">–</span>';
  return '<tr data-sc="' + esc(sc.id) + '" class="' + (sc.enabled ? "" : "sc-paused") + '">' +
    '<td><div class="sc-name">' + esc(sc.name) + "</div>" +
      '<div class="muted small sc-task" title="' + esc(sc.task) + '">' + esc(clip(sc.task, 70)) + "</div>" +
      (sc.workflow ? '<div class="small"><span class="wf-step">workflow ' + esc(sc.workflow) + "</span></div>" : "") + "</td>" +
    '<td class="mono" title="' + esc(sc.repo_path) + '">' + esc(basename(sc.repo_path)) +
      (sc.base_branch ? '<div class="muted small">' + esc(sc.base_branch) + "</div>" : "") + "</td>" +
    "<td>" + trig + '<div class="muted small">overlap ' + esc(sc.overlap || "skip") + " · catch-up " + esc(sc.catch_up || "once") + "</div></td>" +
    "<td>" + next + "</td><td>" + last + "</td>" +
    '<td class="sc-note" title="' + esc(sc.last_note) + '">' + esc(clip(sc.last_note, 80)) + "</td>" +
    "<td>" + run + "</td>" +
    '<td><label class="switch" title="' + (sc.enabled ? "Enabled: click to pause" : "Paused: click to resume") + '">' +
      '<input type="checkbox" data-sc-op="' + (sc.enabled ? "pause" : "resume") + '"' + (sc.enabled ? " checked" : "") + '><span class="slider"></span></label></td>' +
    '<td class="sc-acts"><button type="button" class="btn btn-sm" data-sc-op="' + (sc.enabled ? "pause" : "resume") + '">' + (sc.enabled ? "Pause" : "Resume") + "</button>" +
      '<button type="button" class="btn btn-sm" data-sc-op="run_now">Run now</button>' +
      '<button type="button" class="btn btn-sm btn-danger" data-sc-op="delete">Delete</button></td></tr>';
}

function renderSchedules() {
  const list = schedState.list;
  setHTML($("#sc-body"), list.length
    ? '<table class="atable sc-table"><thead><tr><th>Name</th><th>Repo</th><th>Trigger</th><th>Next</th><th>Last fired</th>' +
      "<th>Last note</th><th>Last run</th><th>On</th><th></th></tr></thead><tbody>" + list.map(scheduleRowHTML).join("") + "</tbody></table>"
    : '<div class="muted pad">No schedules yet. Use <b>New schedule</b> to run a task on a timer or whenever a branch changes.</div>');
}

async function loadSchedules() {
  const info = $("#sc-info");
  try {
    const d = await API.schedules();
    schedState.list = (d && d.schedules) || [];
    renderSchedules();
    info.textContent = plural(schedState.list.length, "schedule") + " · loaded " + clockTime(Date.now() / 1000);
  } catch (e) {
    info.textContent = "";
    setHTML($("#sc-body"), '<div class="err pad">Could not load schedules: ' + esc(e.message) + "</div>");
  }
}

function openSchedules() {
  const dlg = $("#sched-dialog");
  if (!dlg.open) dlg.showModal();
  loadSchedules();
  clearInterval(schedState.timer);
  schedState.timer = setInterval(() => { if ($("#sched-dialog").open) loadSchedules(); else clearInterval(schedState.timer); }, 5000);
}

async function scheduleOp(id, op, el) {
  const sc = schedState.list.find((x) => x.id === id);
  if (op === "delete" && !confirm("Delete schedule “" + (sc ? sc.name : id) + "”? Runs it already started are kept.")) {
    return;
  }
  el.disabled = true;
  try {
    await API.scheduleOp(id, op);
    if (op === "run_now") { toast("Fired <b>" + esc(sc ? sc.name : "#" + id) + "</b>: its run will show on the board.", "ok"); pollBoardNow(); }
  } catch (e) {
    toast("Schedule action failed: " + esc(e.message), "err");
  } finally {
    el.disabled = false;
    $("#sc-body")._html = undefined;   // the checkbox may be out of sync with the server
    loadSchedules();
  }
}

/* new-schedule form */

function toggleScheduleForm(show) {
  const form = $("#sc-form");
  form.hidden = show === undefined ? !form.hidden : !show;
  $("#sc-new").textContent = form.hidden ? "New schedule" : "Hide form";
  if (!form.hidden) {
    const repo = $("#sc-repo");
    if (!repo.value) repo.value = lsGet(LS_REPO) || ((state.stats && state.stats.recent_repos) || [])[0] || "";
    if (repo.value) checkScheduleRepo();
    const agent = lsGet(LS_AGENT); if (agent) $("#sc-agent").value = agent;
    syncScheduleKind();
    $("#sc-name").focus();
  }
}

function syncScheduleKind() {
  const cron = $("#sc-kind-cron").checked;
  $("#sc-cron-row").hidden = !cron;
  $("#sc-change-row").hidden = cron;
}

async function checkScheduleRepo() {
  const path = $("#sc-repo").value.trim(), status = $("#sc-repo-status"), sel = $("#sc-base");
  if (!path) { status.textContent = ""; return; }
  if (sel.dataset.path === path) return;
  const seq = ++schedState.repoSeq;
  status.className = "hint"; status.textContent = "Checking…";
  try {
    const [r, wfs] = await Promise.all([API.repo(path), loadWorkflows(path)]);
    if (seq !== schedState.repoSeq) return;
    sel.dataset.path = path;
    const branches = r.branches || [];
    sel.innerHTML = branches.map((b) => '<option value="' + esc(b) + '">' + esc(b) + "</option>").join("") || '<option value="">(no branches)</option>';
    sel.value = r.current_branch || branches[0] || "";
    if (!$("#sc-ref").value) $("#sc-ref").placeholder = sel.value || "main";
    status.className = "hint ok"; status.textContent = "✓ git repo" + (r.current_branch ? " · on " + r.current_branch : "");
    fillWorkflowSelect($("#sc-workflow"), $("#sc-wf-steps"), wfs);
  } catch (e) {
    if (seq !== schedState.repoSeq) return;
    delete sel.dataset.path;
    sel.innerHTML = '<option value="">(enter a valid repo)</option>';
    status.className = "hint err"; status.textContent = e.message;
  }
}

async function submitSchedule() {
  const err = $("#sc-err"), btn = $("#sc-submit");
  const v = (id) => $(id).value.trim();
  const cron = $("#sc-kind-cron").checked;
  const body = { name: v("#sc-name"), repo_path: v("#sc-repo"), task: v("#sc-task"), kind: cron ? "cron" : "on_change",
    agent: $("#sc-agent").value, overlap: $("#sc-overlap").value, catch_up: $("#sc-catchup").value };
  const missing = [!body.name && "a name", !body.repo_path && "a repository", !body.task && "a task",
    cron && !v("#sc-cron") && "a cron expression"].filter(Boolean);
  if (missing.length) { err.textContent = "Give the schedule " + missing.join(", ") + "."; return; }
  if (cron) body.cron = v("#sc-cron");
  else {
    body.watch_ref = v("#sc-ref") || $("#sc-base").value || undefined;
    if (v("#sc-poll")) body.poll_secs = Number(v("#sc-poll"));
  }
  if ($("#sc-base").value) body.base_branch = $("#sc-base").value;
  if ($("#sc-workflow").value) body.workflow = $("#sc-workflow").value;
  if (v("#sc-budget") !== "" && !isNaN(Number(v("#sc-budget")))) body.budget_usd = Number(v("#sc-budget"));
  err.textContent = ""; btn.disabled = true; btn.textContent = "Creating…";
  try {
    const sc = await API.createSchedule(body);
    toast("Created schedule <b>" + esc(sc && sc.name || body.name) + "</b>" +
      (sc && sc.next_fire_at ? " · first run " + esc(relFuture(sc.next_fire_at)) : ""), "ok");
    for (const id of ["#sc-name", "#sc-task", "#sc-cron"]) $(id).value = "";
    toggleScheduleForm(false);
    loadSchedules();
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = "Create schedule";
  }
}

/* ───────────────────────── dialog ───────────────────────── */

const LS_REPO = "conductor.lastRepo", LS_AGENT = "conductor.lastAgent";
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: fine */ } }

const TASK_PLACEHOLDER = "Describe what the agent should do. Be specific about files, behaviour and how to verify.";
const BATCH_PLACEHOLDER = "One task per line. Each line becomes its own run; the runs are grouped together.\n" +
  "Bump requests to 2.32 in services/api\nBump requests to 2.32 in services/worker";

let repoCheckSeq = 0;

function openDialog() {
  const dlg = $("#new-dialog");
  if (dlg.open) return;
  $("#f-err").textContent = "";
  const repo = $("#f-repo");
  if (!repo.value) repo.value = lsGet(LS_REPO) || ((state.stats && state.stats.recent_repos) || [])[0] || "";
  const agent = lsGet(LS_AGENT); if (agent) $("#f-agent").value = agent;
  syncBatchUi();
  dlg.showModal();
  if (repo.value) { checkRepo(); $("#f-task").focus(); } else repo.focus();
}

function closeDialog() { const d = $("#new-dialog"); if (d.open) d.close(); }

async function checkRepo() {
  const path = $("#f-repo").value.trim();
  const status = $("#f-repo-status"), sel = $("#f-base"), test = $("#f-test");
  if (!path) { status.textContent = ""; status.className = "hint"; clearDialogPolicy(); return; }
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
    loadDialogPolicy(path, seq);
    loadWorkflows(path).then((wfs) => { if (seq === repoCheckSeq) fillWorkflowSelect($("#f-workflow"), $("#f-wf-steps"), wfs); });
  } catch (e) {
    if (seq !== repoCheckSeq) return;
    delete sel.dataset.path;
    sel.innerHTML = '<option value="">(enter a valid repo)</option>';
    test.placeholder = "auto-detect";
    status.className = "hint err";
    status.textContent = e.message;
    clearDialogPolicy();
    fillWorkflowSelect($("#f-workflow"), $("#f-wf-steps"), []);
  }
}

/* The budget cap a policy imposes. Not spelled out in docs/api.md: look for the obvious names. */
function policyBudgetCap(p) {
  if (!p || p.error) return null;
  const pol = p.policy || {};
  for (const k of ["max_budget_usd", "budget_cap_usd", "max_budget"]) if (typeof pol[k] === "number") return pol[k];
  const r = (p.rules || []).find((x) => /budget/i.test(x.rule || "") && typeof x.value === "number");
  return r ? r.value : null;
}

async function loadDialogPolicy(path, seq) {
  const box = $("#f-policy");
  box.hidden = false;
  setHTML(box, '<div class="muted small">Loading policy for this repo…</div>');
  const p = await loadPolicy(path, true);
  if (seq !== repoCheckSeq) return;
  if (!p) { clearDialogPolicy(); return; }
  state.dialogCap = policyBudgetCap(p);
  let html = '<div class="fp-h">Policy for this repo' +
    (p.sha ? ' <span class="muted">sha <code>' + esc(String(p.sha).slice(0, 12)) + "</code></span>" : "") +
    (state.dialogCap !== null ? '<span class="fp-cap">max budget ' + esc(fmtCost(state.dialogCap)) + "</span>" : "") + "</div>";
  if (p.error) html += '<div class="err small">' + esc(p.error) + "</div>";
  else {
    const rules = (p.rules || []).filter((r) => !/budget/i.test(r.rule || "") || state.dialogCap === null);
    html += rules.length
      ? '<ul class="fp-rules">' + rules.map((r) => '<li><span class="mono">' + esc(r.rule) + "</span> " + esc(clip(fmtValue(r.value), 90)) + "</li>").join("") + "</ul>"
      : '<div class="muted small">No rules beyond the defaults.</div>';
  }
  setHTML(box, html);
  applyBudgetCap(false);
}

function clearDialogPolicy() {
  state.dialogCap = null;
  $("#f-policy").hidden = true;
  applyBudgetCap(false);
}

/* Clamp the budget to the policy cap; say so next to the field. */
function applyBudgetCap(clamp) {
  const inp = $("#f-budget"), hint = $("#f-budget-hint"), cap = state.dialogCap;
  if (cap === null) { inp.removeAttribute("max"); hint.textContent = ""; hint.className = "hint"; return; }
  inp.max = String(cap);
  const v = Number(inp.value);
  if (inp.value === "" || isNaN(v) || v <= cap) { hint.textContent = "policy max " + fmtCost(cap); hint.className = "hint"; return; }
  if (clamp) { inp.value = String(cap); hint.textContent = "capped at " + fmtCost(cap) + " by policy"; }
  else hint.textContent = "over the policy max " + fmtCost(cap) + " — will be capped";
  hint.className = "hint warn";
}

function batchTasks() {
  return $("#f-task").value.split("\n").map((s) => s.trim()).filter(Boolean);
}

function syncBatchUi() {
  const on = $("#f-batch").checked, n = batchTasks().length;
  $("#new-dialog").classList.toggle("batch", on);
  $("#f-task").placeholder = on ? BATCH_PLACEHOLDER : TASK_PLACEHOLDER;
  $("#f-task-label").textContent = on ? "Tasks" : "Task";
  $("#f-task-hint").textContent = on ? (n ? plural(n, "task") + " → " + plural(n, "run") + " in one group" : "One task per line.") : "";
  $("#f-title-label").innerHTML = on ? "Group title" : "Title <em>(optional)</em>";
  $("#f-title").placeholder = on ? "e.g. Dependency bumps" : "Short name for the board";
  $("#f-model").disabled = on;
  $("#f-model").title = on ? "Batches use the agent's default model" : "";
  $("#f-submit").textContent = on ? (n ? "Start " + plural(n, "run") : "Start runs") : "Start run";
  $("#new-dialog h2").textContent = on ? "New batch" : "New run";
}

async function submitDialog() {
  const err = $("#f-err"), btn = $("#f-submit");
  const batch = $("#f-batch").checked;
  const repo = $("#f-repo").value.trim(), task = $("#f-task").value.trim();
  if (!repo) { err.textContent = "Repository path is required."; $("#f-repo").focus(); return; }
  if (!task) { err.textContent = batch ? "Write at least one task, one per line." : "Describe the task."; $("#f-task").focus(); return; }
  applyBudgetCap(true);
  const body = { repo_path: repo, agent: $("#f-agent").value };
  const title = $("#f-title").value.trim();
  const base = $("#f-base").value; if (base) body.base_branch = base;
  if ($("#f-notest").checked) body.test_cmd = "";
  else { const t = $("#f-test").value.trim(); if (t) body.test_cmd = t; }
  const budget = $("#f-budget").value;
  if (budget !== "" && !isNaN(Number(budget))) body.budget_usd = Number(budget);
  const workflow = $("#f-workflow").value; if (workflow) body.workflow = workflow;
  let tasks = null;
  if (batch) {
    tasks = batchTasks();
    body.tasks = tasks;
    body.title = title || "Batch of " + tasks.length + " tasks";
  } else {
    body.task = task;
    if (title) body.title = title;
    const model = $("#f-model").value.trim(); if (model) body.model = model;
  }
  const label = btn.textContent;
  err.textContent = ""; btn.disabled = true; btn.textContent = "Starting…";
  try {
    if (batch) {
      const res = await API.batch(body);
      lsSet(LS_REPO, repo); lsSet(LS_AGENT, body.agent);
      $("#f-task").value = ""; $("#f-title").value = "";
      closeDialog();
      if (res && res.group_key) showNewGroup(res.group_key, body.title, (res.runs || tasks).length);
      toast("Started <b>" + esc(plural((res && res.runs ? res.runs.length : tasks.length), "run")) + "</b> in " + esc(body.title), "ok");
    } else {
      const run = await API.create(body);
      lsSet(LS_REPO, repo); lsSet(LS_AGENT, body.agent);
      $("#f-task").value = ""; $("#f-title").value = "";
      closeDialog();
      if (run && run.id) { remember([run]); selectRun(run.id, { scroll: true }); }
    }
    pollBoardNow();
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = label; syncBatchUi();
  }
}

/* After a batch: expand its group (a placeholder until the server lists it) and bring it into view. */
function showNewGroup(key, title, n) {
  if (!state.groups.find((g) => g.group_key === key)) {
    state.pendingGroups.set(key, { group_key: key, title, total: n, needs_you: 0, ready: 0, failed: 0, active: n, done: 0 });
  }
  state.groupsOpen.add(key);
  state.groupLists.set(key, newList());
  state.flashGroup = key;
  renderGroups();
}

/* ───────────────────────── keyboard ───────────────────────── */

function isTyping(e) {
  const t = e.target;
  if ($("dialog[open]")) return true;
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
  else if (e.key === "/") { e.preventDefault(); $("#search").focus(); $("#search").select(); }
  else if (e.key === "Escape" && state.selectedId) selectRun(null);
}

/* ───────────────────────── polling ───────────────────────── */

const BOARD_MS = 2000, DETAIL_MS = 2000;
let boardTimer = null, detailTimer = null, boardBusy = false, boardAgain = false, detailBusy = false;

function filterParams() {
  return { q: state.filter.q, repo: state.filter.repo };
}

/* Fetch the pages of one list that are on screen (offset paging), de-duplicated. */
async function fetchList(list, params) {
  const n = list.pages;
  const pages = await Promise.all(Array.from({ length: n }, (_, i) =>
    API.runs(Object.assign({}, params, { limit: PAGE, offset: i * PAGE }))));
  const seen = new Set(), runs = [];
  for (const p of pages) for (const r of (p && p.runs) || []) if (!seen.has(r.id)) { seen.add(r.id); runs.push(r); }
  const first = pages[0] || {}, last = pages[pages.length - 1] || {};
  const total = typeof first.total === "number" ? first.total : runs.length;
  return { first, runs, total, more: last.next_offset !== null && last.next_offset !== undefined && runs.length < total };
}

function applyList(list, res) {
  if (!res) return;
  list.runs = res.runs; list.total = res.total; list.more = res.more; list.loaded = true;
  // shrink back if the list got shorter than the pages we were showing
  list.pages = Math.max(1, Math.min(list.pages, Math.ceil(res.total / PAGE)));
  remember(res.runs);
}

async function pollBoard() {
  clearTimeout(boardTimer);
  if (boardBusy) { boardAgain = true; return; }
  boardBusy = true;
  const gen = state.boardGen, f = filterParams(), L = state.lists;
  const gkeys = [...state.groupsOpen];
  const soft = (p) => p.catch((e) => { if (e.network) throw e; return null; });
  try {
    const [needs, progress, done, ...groups] = await Promise.all([
      fetchList(L.needs, Object.assign({ view: "needs_you" }, f)),
      soft(fetchList(L.progress, Object.assign({ view: "active" }, f))),
      state.doneOpen ? soft(fetchList(L.done, Object.assign({ view: "done" }, f))) : null,
      ...gkeys.map((k) => soft(fetchList(state.groupLists.get(k) || newList(), Object.assign({ view: "all", group: k }, f)))),
    ]);
    if (gen !== state.boardGen) return;   // filters changed while in flight; a fresh poll is queued
    setConnection(true);
    applyList(L.needs, needs);
    applyList(L.progress, progress);
    if (state.doneOpen) applyList(L.done, done);
    state.next3 = needs.first.next3 || [];
    state.groups = needs.first.groups || [];
    state.stats = needs.first.stats || state.stats;
    remember(state.next3);
    gkeys.forEach((k, i) => { const l = state.groupLists.get(k); if (l) applyList(l, groups[i]); });
    renderBoard();
  } catch (e) {
    if (e.network) setConnection(false);
  } finally {
    boardBusy = false;
    if (boardAgain) { boardAgain = false; pollBoardNow(); }
    else boardTimer = setTimeout(pollBoard, state.connOk ? BOARD_MS : 2500);
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
    if (id === state.selectedId) { state.detailMissing = false; remember([run]); renderDetail(run); }
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

/* Search / repo filter: restart every list at page 1 and drop in-flight responses. */
function setFilter(patch) {
  Object.assign(state.filter, patch);
  state.boardGen++;
  for (const l of Object.values(state.lists)) l.pages = 1;
  for (const l of state.groupLists.values()) l.pages = 1;
  if ($("#search").value.trim() !== state.filter.q) $("#search").value = state.filter.q;
  $("#repo-filter").value = state.filter.repo;
  renderFilterNote();
  pollBoardNow();
}

/* ───────────────────────── boot / events ───────────────────────── */

function wire() {
  $("#board").addEventListener("click", (e) => {
    const more = e.target.closest("[data-more]");
    if (more) { state.lists[more.dataset.more].pages++; more.disabled = true; pollBoardNow(); setTimeout(() => (more.disabled = false), 600); return; }
    if (e.target.closest("[data-clear-filter]")) { setFilter({ q: "", repo: "" }); return; }
    if (e.target.closest("#groups-all")) { state.groupsShowAll = !state.groupsShowAll; renderGroups(); return; }
    const group = e.target.closest(".group");
    if (group) {
      const key = group.dataset.key;
      const op = e.target.closest("[data-gop]");
      if (op) { if (!op.disabled) groupOp(key, op.dataset.gop, op); return; }
      if (e.target.closest("[data-gmore]")) { const l = state.groupLists.get(key); if (l) { l.pages++; pollBoardNow(); } return; }
      if (e.target.closest("[data-gtoggle]")) { toggleGroup(key); return; }
    }
    const card = e.target.closest(".card");
    if (card) selectRun(Number(card.dataset.id));
  });
  $("#board").addEventListener("keydown", (e) => {
    const h = e.target.closest && e.target.closest("[data-gtoggle]");
    if (h && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleGroup(h.closest(".group").dataset.key); }
  });
  $("#sec-done").addEventListener("toggle", (e) => {
    state.doneOpen = e.target.open;
    if (!state.doneOpen) state.lists.done.pages = 1;
    else pollBoardNow();
  });
  $("#n3-cards").addEventListener("click", (e) => {
    const b = e.target.closest("[data-n3]");
    if (b) { if (!b.disabled) onNext3(b.dataset.n3, Number(b.dataset.id), b); return; }
    const card = e.target.closest(".n3-card");
    if (card) selectRun(Number(card.dataset.id), { scroll: true });
  });

  // top bar: search (debounced), repo filter, audit
  let searchT = null;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchT);
    const v = e.target.value.trim();
    searchT = setTimeout(() => { if (v !== state.filter.q) setFilter({ q: v }); }, 300);
  });
  $("#search").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.target.value = ""; clearTimeout(searchT); if (state.filter.q) setFilter({ q: "" }); e.target.blur(); }
  });
  $("#repo-filter").addEventListener("change", (e) => setFilter({ repo: e.target.value }));
  $("#audit-btn").addEventListener("click", openAudit);

  // schedules
  $("#sched-btn").addEventListener("click", openSchedules);
  $("#sc-refresh").addEventListener("click", loadSchedules);
  $("#sc-close").addEventListener("click", () => $("#sched-dialog").close());
  $("#sc-new").addEventListener("click", () => toggleScheduleForm());
  $("#sc-cancel").addEventListener("click", () => toggleScheduleForm(false));
  $("#sc-form").addEventListener("submit", (e) => { e.preventDefault(); submitSchedule(); });
  $("#sc-repo").addEventListener("blur", checkScheduleRepo);
  $("#sc-repo").addEventListener("change", checkScheduleRepo);
  for (const id of ["#sc-kind-cron", "#sc-kind-change"]) $(id).addEventListener("change", syncScheduleKind);
  $("#sc-workflow").addEventListener("change", (e) => showWorkflowSteps(e.target, $("#sc-wf-steps")));
  $("#sched-dialog").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) { e.currentTarget.close(); return; }
    const a = e.target.closest("[data-sc-run]");
    if (a) { e.preventDefault(); $("#sched-dialog").close(); selectRun(Number(a.dataset.scRun), { scroll: true }); return; }
    const b = e.target.closest("button[data-sc-op]");
    if (b && !b.disabled) scheduleOp(Number(b.closest("tr").dataset.sc), b.dataset.scOp, b);
  });
  $("#sched-dialog").addEventListener("change", (e) => {
    const c = e.target.closest("input[data-sc-op]");
    if (c) scheduleOp(Number(c.closest("tr").dataset.sc), c.dataset.scOp, c);
  });
  $("#audit-refresh").addEventListener("click", loadAudit);
  $("#audit-close").addEventListener("click", () => $("#audit-dialog").close());
  $("#audit-dialog").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) { e.currentTarget.close(); return; }
    const a = e.target.closest("[data-audit-run]");
    if (a) { e.preventDefault(); $("#audit-dialog").close(); selectRun(Number(a.dataset.auditRun), { scroll: true }); }
  });

  $("#d-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) showTab(b.dataset.tab, true);
  });
  $("#d-actions").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-action]");
    if (b && !b.disabled) doAction(b.dataset.action, b);
  });
  $("#q-send").addEventListener("click", () => sendAnswer());
  $("#q-approve").addEventListener("click", () => sendAnswer("approve"));
  $("#q-answer").addEventListener("input", (e) => { if (state.selectedId) state.drafts.set(state.selectedId, e.target.value); });
  $("#q-answer").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendAnswer(); }
  });

  // review tab: policy panel
  $("#rv-policy").addEventListener("toggle", (e) => {
    if (!e.target.matches || !e.target.matches("details.eff-pol")) return;
    if (state.effPolicyOpen === e.target.open) return;
    state.effPolicyOpen = e.target.open;
    renderPolicy();
  }, true);
  $("#rv-policy").addEventListener("click", (e) => {
    if (e.target.closest("[data-override]") && state.detail) openOverrideDialog(state.detail);
  });

  // override dialog
  $("#override-form").addEventListener("submit", (e) => { e.preventDefault(); submitOverride(); });
  $("#ov-cancel").addEventListener("click", () => $("#override-dialog").close());
  $("#ov-reason").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitOverride(); }
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

  // new run dialog
  $("#new-run-btn").addEventListener("click", openDialog);
  setupNotifyButton();
  $("#f-cancel").addEventListener("click", closeDialog);
  $("#f-repo").addEventListener("blur", checkRepo);
  $("#f-repo").addEventListener("change", checkRepo);
  $("#f-base").addEventListener("change", (e) => { e.target._userPicked = true; });
  $("#f-notest").addEventListener("change", (e) => { $("#f-test").disabled = e.target.checked; });
  $("#f-batch").addEventListener("change", syncBatchUi);
  $("#f-workflow").addEventListener("change", (e) => showWorkflowSteps(e.target, $("#f-wf-steps")));
  $("#f-task").addEventListener("input", () => { if ($("#f-batch").checked) syncBatchUi(); });
  $("#f-budget").addEventListener("input", () => applyBudgetCap(false));
  $("#f-budget").addEventListener("change", () => applyBudgetCap(true));
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
