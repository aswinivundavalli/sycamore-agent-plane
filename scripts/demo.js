#!/usr/bin/env node
/*
 * Scripted demo of the conductor: the walkthrough from the brief, end to end.
 *
 *   node scripts/demo.js                      # watch it in a visible browser (needs a display)
 *   node scripts/demo.js --record docs/demo.mp4   # headless, records a video
 *   node scripts/demo.js --interactive        # pauses at each step: press Enter to go on
 *   node scripts/demo.js --agent claude       # real Claude Code instead of the scripted agent
 *
 * It creates a throwaway demo repo and conductor home under --workdir
 * (default: a new temp dir), starts the conductor, and drives the real UI with
 * Playwright: create a run, run several in parallel, answer a question,
 * review and merge, catch a same-file conflict and hand it back to the agent,
 * then kill -9 the conductor mid-run and bring it back.
 *
 * Needs: node, the `playwright` package with Chromium, python3, git (and
 * ffmpeg to convert the recording to .mp4; otherwise it stays .webm).
 */
"use strict";

const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

let chromium;
try {
  ({ chromium } = require("playwright"));
} catch (e) {
  console.error("This script needs the `playwright` npm package (npm i -g playwright).");
  process.exit(1);
}

// ------------------------------------------------------------------ options
const argv = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = argv.indexOf("--" + name);
  if (i < 0) return dflt;
  const v = argv[i + 1];
  return v === undefined || v.startsWith("--") ? true : v;
};
const ROOT = path.resolve(__dirname, "..");
const AGENT = opt("agent", "fake");
const FAKE = AGENT === "fake";
const RECORD = opt("record", null);
const INTERACTIVE = !!opt("interactive", false);
const PORT = Number(opt("port", 7790));
const WORK = path.resolve(opt("workdir", fs.mkdtempSync(path.join(os.tmpdir(), "conductor-demo-"))));
const PACE = Number(opt("pace", 1)); // multiply all pauses
const BASE = `http://127.0.0.1:${PORT}`;
const REPO = path.join(WORK, "textkit");
const HOME = path.join(WORK, "conductor-home");

// ------------------------------------------------------------------ helpers
const sleep = (ms) => new Promise((r) => setTimeout(r, ms * PACE));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

async function api(method, p, body) {
  const res = await fetch(BASE + p, {
    method, headers: { "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${method} ${p}: ${data.error || res.status}`);
  return data;
}

async function waitFor(id, states, timeoutS = FAKE ? 90 : 900) {
  states = [].concat(states);
  const end = Date.now() + timeoutS * 1000;
  while (Date.now() < end) {
    try {
      const r = await api("GET", "/api/runs/" + id);
      if (states.includes(r.state)) return r;
    } catch (e) { /* conductor restarting */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`run #${id} did not reach ${states} within ${timeoutS}s`);
}

let server = null;
function startConductor() {
  const out = fs.openSync(path.join(WORK, "conductor.log"), "a");
  server = spawn("python3", ["-m", "conductor", "--port", String(PORT), "--home", HOME], {
    cwd: ROOT, stdio: ["ignore", out, out],
    env: { ...process.env, FAKE_AGENT_STEP_SECS: "1.2", FAKE_AGENT_STATE_DIR: path.join(WORK, "fake-state"),
           CONDUCTOR_MAX_PARALLEL: "4" },
  });
  log("conductor started, pid", server.pid);
}
async function waitUp() {
  for (let i = 0; i < 60; i++) {
    try { await api("GET", "/api/stats"); return; } catch (e) { await new Promise((r) => setTimeout(r, 250)); }
  }
  throw new Error("conductor did not come up; see " + path.join(WORK, "conductor.log"));
}

function runnerPid(runId) {
  try {
    const out = execFileSync("pgrep", ["-f", `conductor.runner ${HOME}/runs/run-${runId}/`]).toString().trim();
    return Number(out.split("\n")[0]);
  } catch (e) { return null; }
}

// Fake-agent tasks carry directives; the real agent gets plain English.
const T = {
  wordCount: FAKE
    ? "word_count miscounts text with repeated spaces or newlines. Fix it and add a regression test.\n" +
      "[[fake:sleep 6]][[fake:write textkit/core.py]]" + fs.readFileSync(path.join(__dirname, "demo-files", "core_fixed.py"), "utf8") +
      "[[/fake]][[fake:append tests/test_core.py]]" + fs.readFileSync(path.join(__dirname, "demo-files", "test_append.py"), "utf8") + "[[/fake]]"
    : "word_count miscounts text with repeated spaces or newlines. Fix it and add a regression test.",
  ellipsis: FAKE
    ? "Add an `ellipsis` option to truncate(). [[fake:ask: Should the cut be marked with the single character '…' or three dots '...'?]][[fake:sleep 4]]" +
      "[[fake:append textkit/core.py]]\n\ndef truncate_ellipsis(text, width):\n    \"\"\"Like truncate, but marks the cut with an ellipsis.\"\"\"\n    return text if len(text) <= width else text[: max(0, width - 1)] + \"…\"\n[[/fake]]"
    : "Add an `ellipsis` option to truncate(). Before you start, ask me whether to use the single character '…' or three dots '...', using the QUESTION: protocol, and wait for my answer.",
  changelogA: FAKE
    ? "Start a CHANGELOG with an entry for the word_count fix. [[fake:sleep 8]][[fake:write CHANGELOG.md]]# Changelog\n\n- Fix word_count on repeated whitespace\n[[/fake]]"
    : "Create CHANGELOG.md with a '# Changelog' heading and one bullet: 'Fix word_count on repeated whitespace'.",
  changelogB: FAKE
    ? "Start a CHANGELOG with an entry for the truncate ellipsis. [[fake:sleep 8]][[fake:write CHANGELOG.md]]# Changelog\n\n- Add ellipsis option to truncate\n[[/fake]]"
    : "Create CHANGELOG.md with a '# Changelog' heading and one bullet: 'Add ellipsis option to truncate'.",
  longOne: FAKE ? "Add a `textkit` command-line entry point. [[fake:sleep 35]][[fake:write textkit/__main__.py]]import sys\nfrom . import word_count\n\nprint(word_count(sys.stdin.read()))\n[[/fake]]"
    : "Add a `python -m textkit` entry point that prints the word count of stdin, with a test.",
  longTwo: FAKE ? "Add type hints to textkit/core.py. [[fake:sleep 120]]" : "Add type hints to every function in textkit/core.py.",
};

// ------------------------------------------------------------------ captions
async function caption(page, title, sub = "") {
  log("STEP:", title, sub ? "— " + sub : "");
  await page.evaluate(([t, s]) => {
    let el = document.getElementById("__demo_caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "__demo_caption";
      el.style.cssText = "position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:99999;" +
        "background:rgba(17,24,39,.92);color:#fff;padding:10px 18px;border-radius:10px;max-width:80%;" +
        "font:600 16px system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.25);text-align:center;pointer-events:none";
      document.body.appendChild(el);
    }
    el.innerHTML = "<div>" + t + "</div>" + (s ? '<div style="font-weight:400;font-size:13px;opacity:.85;margin-top:3px">' + s + "</div>" : "");
  }, [title, sub]).catch(() => {});
}

async function step(page, title, sub) {
  await caption(page, title, sub);
  if (INTERACTIVE) {
    await new Promise((r) => {
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      rl.question(`  [${title}] press Enter to continue… `, () => { rl.close(); r(); });
    });
  } else {
    await sleep(2500);
  }
}

async function select(page, id) {
  const card = page.locator(`#board .card[data-id="${id}"]`);
  await card.scrollIntoViewIfNeeded().catch(() => {});
  await card.click();
  await sleep(1200);
}
async function clickAction(page, action) {
  await page.locator(`#d-actions button[data-action="${action}"]`).click();
  await sleep(1500);
}

async function newRunViaUI(page, title, task) {
  await page.click("#new-run-btn");
  await sleep(600);
  await page.fill("#f-repo", REPO);
  await page.locator("#f-repo").press("Tab");
  await sleep(800);
  await page.locator("#f-task").pressSequentially(task.split("[[")[0].trim(), { delay: 12 });
  if (task.includes("[[")) await page.fill("#f-task", task); // directives typed instantly
  await page.fill("#f-title", title);
  await page.selectOption("#f-agent", AGENT);
  await sleep(900);
  await page.click("#f-submit");
  await sleep(1200);
  const runs = await api("GET", "/api/runs");
  return Math.max(...runs.runs.map((r) => r.id));
}

// ------------------------------------------------------------------ the demo
async function main() {
  log("workdir:", WORK);
  fs.mkdirSync(WORK, { recursive: true });
  if (!fs.existsSync(REPO)) execFileSync(path.join(__dirname, "make-demo-repo.sh"), [REPO]);
  startConductor();
  await waitUp();

  const browser = await chromium.launch({ headless: !!RECORD || !process.env.DISPLAY });
  const videoDir = path.join(WORK, "video");
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    ...(RECORD ? { recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } } } : {}),
  });
  const page = await ctx.newPage();
  await page.goto(BASE);
  await sleep(500);

  await step(page, "Conductor: one page for all your coding agents",
    `Demo repo: ${path.basename(REPO)} (Python, unittest suite). Agent: ${AGENT}.`);

  // 1. Create a run -----------------------------------------------------
  await step(page, "1 · Create a run", "Repo path + a task in plain English");
  const r1 = await newRunViaUI(page, "Fix word_count on repeated whitespace", T.wordCount);
  await step(page, "Each run gets its own git worktree and branch", "~/.conductor/worktrees/run-N on conductor/run-N");

  // 2. Parallel runs + a question ----------------------------------------
  await step(page, "2 · Start more runs in parallel", "They never touch each other's files or branches");
  const r2 = await newRunViaUI(page, "Add ellipsis option to truncate", T.ellipsis);
  const r3 = (await api("POST", "/api/runs", { repo_path: REPO, title: "Start a CHANGELOG (word_count)", task: T.changelogA, agent: AGENT })).id;
  const r4 = (await api("POST", "/api/runs", { repo_path: REPO, title: "Start a CHANGELOG (ellipsis)", task: T.changelogB, agent: AGENT })).id;
  await step(page, "The status line comes from the agent's own event stream",
    "Tool calls → \"Editing textkit/core.py\", \"Running: pytest\" — no need to open the log");
  await select(page, r1);
  await page.locator('#d-tabs button[data-tab="activity"]').click();
  await sleep(4000);

  await waitFor(r2, "waiting_input");
  await step(page, "3 · An agent is blocked on a question", "It jumps to the top of the board, under NEEDS YOU");
  await select(page, r2);
  await sleep(1500);
  await page.locator("#q-answer").pressSequentially(FAKE ? "Use the single character '…'." : "Use the single character '…'.", { delay: 25 });
  await sleep(600);
  await page.click("#q-send");
  await step(page, "The answer resumes the same agent session", "It picks up where it left off, in the same worktree");

  // Overlap warning while both CHANGELOG runs are live.
  await sleep(4000);
  await step(page, "Two runs are editing the same file", "Flagged while they run — not discovered at merge time");
  await select(page, r3);
  await sleep(2500);

  // 3. Review and merge --------------------------------------------------
  await waitFor(r1, "finished");
  await step(page, "4 · Review: diff, agent summary and test results", "The conductor runs the tests itself — it doesn't trust the agent's claim");
  await select(page, r1);
  await page.locator('#d-tabs button[data-tab="review"]').click();
  await sleep(2000);
  await page.mouse.wheel(0, 500);
  await sleep(2500);
  await page.mouse.wheel(0, -500);
  await step(page, "Accept → merge into main", "Worktree and branch are cleaned up automatically");
  await clickAction(page, "accept");
  await sleep(1500);

  // 4. Same-file conflict -----------------------------------------------
  await waitFor(r3, "finished");
  await waitFor(r4, "finished");
  await select(page, r3);
  await clickAction(page, "accept");
  await step(page, "5 · Merged the first CHANGELOG run…", "…so the second one now conflicts with main");
  await sleep(6000); // supervisor refreshes merge checks
  await select(page, r4);
  await page.locator('#d-tabs button[data-tab="review"]').click();
  await step(page, "Merge is blocked; the conflict is shown before you click", "One click hands it back to the same agent session");
  await clickAction(page, "resolve_conflicts");
  await waitFor(r4, "finished");
  await sleep(1500);
  await select(page, r4);
  await page.locator('#d-tabs button[data-tab="review"]').click();
  await step(page, "Resolved, re-tested, merges cleanly", "Both CHANGELOG entries kept");
  await clickAction(page, "accept");

  await waitFor(r2, "finished");
  await select(page, r2);
  await page.locator('#d-tabs button[data-tab="review"]').click();
  const mc2 = (await api("GET", "/api/runs/" + r2)).merge_check || {};
  await step(page, "The run that asked a question finished with your answer",
    `main moved ${mc2.behind || 0} commits under it, but it still merges cleanly → merge`);
  await clickAction(page, "accept");

  // 5. Kill the conductor ------------------------------------------------
  const r5 = (await api("POST", "/api/runs", { repo_path: REPO, title: "Add a CLI entry point", task: T.longOne, agent: AGENT })).id;
  const r6 = (await api("POST", "/api/runs", { repo_path: REPO, title: "Add type hints", task: T.longTwo, agent: AGENT })).id;
  await waitFor(r5, "running");
  await waitFor(r6, "running");
  await sleep(3000);
  await step(page, "6 · Two agents are mid-run. Now: kill -9 the conductor", "");
  server.kill("SIGKILL");
  await sleep(2500);
  await step(page, "The conductor is gone — the agents are not",
    "They run under a detached runner that writes to a file, not a pipe");
  const p6 = runnerPid(r6);
  if (p6) { try { process.kill(-p6, "SIGKILL"); } catch (e) {} }
  await step(page, "Meanwhile one agent dies too (think: laptop reboot)", "Run #" + r6 + "'s process is killed while nobody is watching");
  await sleep(2000);
  startConductor();
  await waitUp();
  await sleep(3000);
  await step(page, "Conductor back: every run, state and result is still there",
    `#${r5} re-attached and kept going · #${r6} is marked interrupted — never restarted on its own`);
  await select(page, r5);
  await page.locator('#d-tabs button[data-tab="activity"]').click();
  await sleep(3000);
  await select(page, r6);
  await step(page, "Restart resumes the interrupted agent's session", "Exactly once, however many times you click");
  await clickAction(page, "restart");
  await sleep(4000);

  await waitFor(r5, "finished");
  await select(page, r5);
  await clickAction(page, "accept");
  await step(page, "Clean-up: accepted, rejected and stopped runs leave no worktrees behind",
    "Disk use of live worktrees is always in the header");
  await sleep(3000);
  await step(page, "That's the loop: start · watch · answer · review · merge — and survive a restart", "");
  await sleep(2000);

  const video = page.video();
  await ctx.close();
  await browser.close();
  server.kill("SIGTERM");
  try { execFileSync("pkill", ["-f", `conductor.runner ${HOME}`]); } catch (e) {}

  if (RECORD && video) {
    const raw = await video.path();
    const outPath = path.resolve(RECORD);
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    if (outPath.endsWith(".mp4")) {
      try {
        execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", raw, "-c:v", "libx264", "-preset", "slow",
          "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart", outPath]);
      } catch (e) {
        const webm = outPath.replace(/\.mp4$/, ".webm");
        fs.copyFileSync(raw, webm);
        log("ffmpeg failed; saved", webm);
        return;
      }
    } else {
      fs.copyFileSync(raw, outPath);
    }
    log("recording saved:", outPath, (fs.statSync(outPath).size / 1e6).toFixed(1) + " MB");
  }
}

main().catch((e) => {
  console.error(e);
  if (server) server.kill("SIGTERM");
  process.exit(1);
});
