"""The supervisor: starts agents, watches them, and finishes their attempts.

One background thread runs ``tick()`` twice a second. Each tick is written to
be safe to run at any time, including right after a crash, because it derives
everything from the database and the files on disk rather than from memory:

  1. Monitor every run in starting/running: ingest new lines of its stream file
     (from the stored byte offset), then check whether the runner wrote
     exit.json (attempt over), is still alive (keep going), or vanished
     (interrupted).
  2. Run tests for runs in ``testing`` that have no test thread yet.
  3. Start queued runs while fewer than ``max_parallel`` agents are active.
  4. Periodically: refresh merge checks, changed-file lists (for overlap
     warnings) and clean up stray worktrees.

Because of (1), re-attaching after a conductor restart is not a special code
path: the first tick after startup simply picks up where the last one stopped.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import gitops, states
from . import cron
from . import policy as pol
from . import sandbox
from . import workflows as wfl
from .guard import LiveGuard
from .agents import stream as streamparse
from .agents.base import extract_question
from .agents.registry import get_adapter
from .config import Config
from .db import Store

log = logging.getLogger("conductor.supervisor")

TEST_OUTPUT_LIMIT = 20_000
READ_CHUNK = 1 << 20


class Supervisor:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._locks: dict[int, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._children: dict[int, subprocess.Popen] = {}  # runner pid -> Popen (to reap)
        self._testing: set[int] = set()
        self._guards: dict[int, LiveGuard] = {}  # run id -> live policy state
        # Set by the service: creates a run from a schedule (same validation
        # and policy as a run created by hand).
        self.create_run_cb = None
        from .fleet import Fleet  # remote workers (coordinator side)
        self.fleet = Fleet(self)
        self._test_procs: dict[int, subprocess.Popen] = {}  # run id -> running test cmd
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_slow = 0.0
        self._last_janitor = 0.0
        self.stats_cache: dict = {"worktree_bytes": 0, "worktrees": 0}

    # ------------------------------------------------------------------ util
    def lock(self, run_id: int) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(run_id, threading.RLock())

    def start(self) -> None:
        self.cfg.ensure_dirs()
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let one bad run kill the loop
                log.exception("supervisor tick failed")
            self._stop.wait(self.cfg.tick_secs)

    # -------------------------------------------------------------- recovery
    def recover(self) -> None:
        """Called once at startup, before the loop.

        Runs in ``starting`` whose runner never wrote runner.json were mid-launch
        when we died: mark them interrupted (we can't know whether the agent
        began, so we don't guess and we never relaunch on our own).
        Runs in ``running`` are left alone: the first tick re-attaches to them
        if their runner is alive, or marks them interrupted if not.
        Runs in ``testing`` lost their test thread; tests are idempotent, so the
        first tick simply runs them again.
        """
        for run in self.store.list([states.STARTING]):
            if run.get("worker_id"):
                continue  # the lease decides: a silent worker is expired, not guessed
            d = self.cfg.attempt_dir(run["id"], run["attempt"])
            info = _read_json(d / "runner.json")
            pid = run.get("runner_pid") or (info or {}).get("runner_pid")
            if pid and _runner_alive(pid, d):
                self.store.transition(run["id"], states.STARTING, states.RUNNING,
                                      "Re-attached to agent after conductor restart",
                                      runner_pid=pid)
            else:
                self.store.transition(run["id"], states.STARTING, states.INTERRUPTED,
                                      "Conductor stopped while launching the agent",
                                      activity="Interrupted during launch")
        for run in self.store.list([states.RUNNING]):
            if run.get("worker_id"):
                continue
            d = self.cfg.attempt_dir(run["id"], run["attempt"])
            if run.get("runner_pid") and _runner_alive(run["runner_pid"], d):
                self.store.add_event(run["id"], "info",
                                     "Conductor restarted; re-attached to running agent")
        self.janitor()

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        self._reap()
        for run in self.store.list(states.AGENT_ACTIVE):
            if run.get("worker_id"):
                continue  # a remote worker reports on it
            with self.lock(run["id"]):
                run = self.store.get(run["id"])
                if run["state"] in states.AGENT_ACTIVE:
                    self._monitor(run)
        self.fleet.expire_leases()
        for run in self.store.list([states.TESTING]):
            if run.get("worker_id"):
                continue  # the worker runs the tests
            if run["id"] not in self._testing:
                self._testing.add(run["id"])
                threading.Thread(target=self._run_tests, args=(run["id"],),
                                 name=f"tests-{run['id']}", daemon=True).start()
        self._schedule()
        self._fire_due_schedules()
        now = time.time()
        if now - self._last_slow > 5:
            self._last_slow = now
            self._refresh_changed_files()
            self._refresh_merge_checks()
        if now - self._last_janitor > 300:
            self.janitor()
        elif now - self.stats_cache.get("computed_at", 0) > 30:
            self._refresh_disk_stats()

    def _reap(self) -> None:
        for pid, p in list(self._children.items()):
            if p.poll() is not None:
                del self._children[pid]

    # -------------------------------------------------------------- schedule
    def policy_for(self, run: dict) -> pol.Policy:
        """The run's effective policy (stored at create; older runs: loaded now)."""
        if run.get("policy"):
            return pol.Policy.from_dict(run["policy"])
        return pol.load(self.cfg.home, Path(run["repo_path"]))[0]

    def _schedule(self) -> None:
        running = self.store.list(states.AGENT_ACTIVE)
        active = sum(1 for r in running if not r.get("worker_id"))  # local slots only
        per_repo: dict[str, int] = {}
        for r in running:
            per_repo[r["repo_path"]] = per_repo.get(r["repo_path"], 0) + 1
        for run in self.store.list([states.QUEUED]):  # oldest first
            if active >= self.cfg.max_parallel:
                return
            cap = self.policy_for(run).max_parallel_per_repo
            if cap is not None and per_repo.get(run["repo_path"], 0) >= cap:
                continue  # this repo is at its policy limit; others may still start
            if self._launch(run):
                active += 1
                per_repo[run["repo_path"]] = per_repo.get(run["repo_path"], 0) + 1

    def _launch(self, run: dict) -> bool:
        run_id = run["id"]
        with self.lock(run_id):
            attempt = run["attempt"] + 1
            now = time.time()
            # The compare-and-set below is what guarantees a run is never
            # started twice: only one caller can move it out of "queued".
            if not self.store.transition(
                    run_id, states.QUEUED, states.STARTING, f"Starting attempt {attempt}",
                    attempt=attempt, attempt_started_at=now, last_output_at=now,
                    stream_offset=0, stalled=0, exit_code=None, runner_pid=None,
                    attempt_cost_usd=0, error=None, activity="Preparing worktree",
                    activity_at=now, started_at=run.get("started_at") or now,
                    worker_id=None):  # a restarted remote run may come back local
                return False
            try:
                return self._launch_inner(self.store.get(run_id), attempt)
            except Exception as e:  # git failure, agent missing, ...
                log.exception("launch failed for run %s", run_id)
                self.store.transition(run_id, states.STARTING, states.FAILED,
                                      f"Could not start: {e}", error=f"Could not start: {e}",
                                      activity="Failed to start", finished_at=time.time())
                return False

    def _launch_inner(self, run: dict, attempt: int) -> bool:
        run_id = run["id"]
        wt = Path(run["worktree_path"] or self.cfg.worktree_for(run_id))
        if not (wt / ".git").exists():
            base = gitops.create_worktree(run["repo_path"], wt, run["branch"],
                                          run["base_commit"] or run["base_branch"])
            self.store.update(run_id, worktree_path=str(wt),
                              base_commit=run["base_commit"] or base)
            run = self.store.get(run_id)

        prompt = run["next_prompt"] or run["task"]
        if run.get("pre_attempt") == "merge_target":
            res = gitops.merge_target_into_worktree(wt, run["base_branch"])
            self.store.update(run_id, pre_attempt=None)
            if res["clean"]:
                self.store.add_event(run_id, "info",
                                     f"Merged {run['base_branch']} into the branch cleanly; "
                                     "no agent needed")
                self.store.transition(run_id, states.STARTING, states.RUNNING)
                self.store.transition(run_id, states.RUNNING, states.TESTING,
                                      "Re-running tests after merge", test_status="running",
                                      activity="Running tests")
                return True
            prompt = conflict_prompt(run, res["conflicts"])
            self.store.add_event(run_id, "info",
                                 f"Merged {run['base_branch']} into the branch; conflicts in "
                                 f"{len(res['conflicts'])} file(s) handed to the agent")

        budget = attempt_budget(run)
        resume = bool(run.get("resume") and run.get("session_id"))
        session_id = run["session_id"] if resume else str(uuid.uuid4())
        adapter = get_adapter(run["agent"])
        ok, why = adapter.available()
        if not ok:
            raise RuntimeError(why)
        spec = adapter.build(prompt=prompt, session_id=session_id, resume=resume,
                             model=run.get("model"), budget_usd=budget,
                             worktree=str(wt),
                             disallowed_tools=pol.claude_disallowed_tools(self.policy_for(run)))
        self._guards[run_id] = LiveGuard()

        d = self.cfg.attempt_dir(run_id, attempt)
        d.mkdir(parents=True, exist_ok=True)
        for f in ("stream.jsonl", "exit.json", "runner.json"):
            (d / f).unlink(missing_ok=True)
        (d / "prompt.txt").write_text(prompt)
        (d / "command.json").write_text(json.dumps({"argv": spec.argv, "resume": resume}))

        env = {**os.environ, **spec.env, "CONDUCTOR_RUN_ID": str(run_id)}
        env.pop("CLAUDECODE", None)  # let a nested Claude Code start normally
        argv, note = sandboxed(self.policy_for(run), spec.argv, wt, d / "tmp", env)
        if note:
            self.store.add_event(run_id, "info", note)
        proc = subprocess.Popen(
            [sys.executable, "-m", "conductor.runner", str(d), str(wt), "--", *argv],
            cwd=str(wt), env={**env, "PYTHONPATH": _pkg_root()},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)  # own process group: survives us, killable as a unit
        self._children[proc.pid] = proc
        threading.Thread(target=self._refresh_disk_stats, daemon=True).start()
        self.store.transition(
            run_id, states.STARTING, states.RUNNING,
            f"Agent started ({run['agent']}{', resuming session' if resume else ''})",
            runner_pid=proc.pid, session_id=session_id, next_prompt=None, resume=0,
            activity="Agent starting up", activity_at=time.time())
        return True

    # --------------------------------------------------------------- monitor
    def _monitor(self, run: dict) -> None:
        run_id, attempt = run["id"], run["attempt"]
        d = self.cfg.attempt_dir(run_id, attempt)
        if run["state"] == states.STARTING:
            return  # being launched right now by another thread
        self._ingest(run, d)
        exit_info = _read_json(d / "exit.json")
        if exit_info is not None:
            self._ingest(self.store.get(run_id), d)  # drain anything written at the end
            self._finish_attempt(self.store.get(run_id), exit_info, d)
            return
        pid = run.get("runner_pid")
        if not pid or not _runner_alive(pid, d):
            self._ingest(self.store.get(run_id), d)
            self.store.transition(
                run_id, states.RUNNING, states.INTERRUPTED,
                "Agent process is gone (killed or machine restarted)",
                activity="Interrupted: agent process disappeared", activity_at=time.time(),
                runner_pid=None)
            gitops_safe(gitops.commit_all, run["worktree_path"],
                        f"conductor: run {run_id} partial work (interrupted)")
            return
        # watchdog
        now = time.time()
        run = self.store.get(run_id)
        if not run["stalled"] and now - (run["last_output_at"] or now) > self.cfg.stall_secs:
            self.store.update(run_id, stalled=1)
            self.store.add_event(run_id, "info",
                                 f"No output for {self.cfg.stall_secs}s: flagged as stalled")
        limit = self.cfg.max_run_secs
        cap = self.policy_for(run).max_runtime_secs
        if cap:
            limit = min(limit, cap)
        if now - (run["attempt_started_at"] or now) > limit and not run.get("error"):
            self.store.add_event(run_id, "error",
                                 f"Exceeded max run time ({limit}s); stopping")
            self.store.update(run_id, error=f"Timed out after {limit}s")
            _kill_group(pid)

    def _ingest(self, run: dict, d: Path) -> None:
        path = d / "stream.jsonl"
        try:
            with open(path, "rb") as f:
                f.seek(run["stream_offset"])
                chunk = f.read(READ_CHUNK)
        except FileNotFoundError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self.ingest_chunk(run, chunk[:end + 1])

    def ingest_chunk(self, run: dict, chunk: bytes) -> None:
        """Parse whole lines of an agent's stream that start at the run's
        stored offset, record events, apply the live policy check, and move
        the offset on - one transaction. The same path serves local runs (read
        from the stream file) and remote workers (posted over HTTP)."""
        events, fields = [], {}
        cost_add = 0.0
        # Show paths relative to the worktree ("src/app.py", not the full path).
        # Only the parsed view changes; the transcript on disk stays verbatim.
        wt = run.get("worktree_path") or run.get("remote_worktree")
        wt_prefix = (wt or "\0").rstrip("/") + "/"
        guard = self._guards.setdefault(run["id"], LiveGuard())
        policy = self.policy_for(run)
        new_violations, kill_reason = [], None
        for raw in chunk.splitlines():
            line = raw.decode("utf-8", "replace")
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                for f in guard.inspect(obj, policy, run["agent"], wt,
                                       sandboxed=policy.sandbox_writes == "worktree"):
                    new_violations.append(f.violation)
                    events.append(("error" if f.kill else "info",
                                   f"Policy: {f.violation['detail']}", f.violation))
                    if f.kill and not kill_reason:
                        kill_reason = f.violation["detail"]
            p = streamparse.parse_line(line.replace(wt_prefix, ""))
            events.extend(p.events)
            if p.activity:
                fields["activity"] = p.activity
                fields["activity_at"] = time.time()
            if p.session_id and not run.get("session_id"):
                fields["session_id"] = p.session_id
            if p.result:
                text = p.result.get("text") or ""
                if text:
                    fields["summary"] = text
                cost = float(p.result.get("cost_usd") or 0)
                cost_add += cost
                fields["attempt_cost_usd"] = cost
        if cost_add:
            fields["cost_usd"] = (run.get("cost_usd") or 0) + cost_add
        fields["last_output_at"] = time.time()
        if run.get("stalled"):
            fields["stalled"] = 0
        if new_violations:
            fields["violations"] = (run.get("violations") or []) + new_violations
        if kill_reason and not run.get("error"):
            fields["error"] = f"Stopped by policy: {kill_reason}"
        self.store.ingest(run["id"], run["attempt"], run["stream_offset"] + len(chunk),
                          events, **fields)
        if kill_reason and not run.get("error"):
            self.store.audit("conductor", "policy_kill", run["id"], {"reason": kill_reason})
            self.kill(run)
        elif guard.scan_due:
            # A tool that can write just returned: check what is actually in
            # the worktree now (once per chunk, however many calls it held).
            after, guard.scan_due = guard.scan_after, False
            self._scan_worktree(self.store.get(run["id"]), guard, after)

    def _scan_worktree(self, run: dict, guard: LiveGuard, after: str = "") -> None:
        """Local runs only: a remote run's worktree is on its worker, which
        reports its changed files with every heartbeat (``Fleet.heartbeat``)."""
        wt = run.get("worktree_path")
        if not wt or not run.get("base_commit") or run["state"] != states.RUNNING:
            return
        try:
            if guard.base is None:
                guard.base = gitops.review_base(wt, run["base_branch"],
                                                fallback=run["base_commit"])
            files = gitops.changed_files(wt, guard.base)
        except gitops.GitError:
            return
        self.enforce_paths(run, files, after)

    def enforce_paths(self, run: dict, files: list[str], after: str = "") -> None:
        """Apply the policy's path rules to the files a running agent has
        actually changed, whatever wrote them, and act on the result
        (``kill`` stops the run now). Called after each tool call that can
        write (local runs), from the periodic refresh, and from worker
        heartbeats (remote runs)."""
        with self.lock(run["id"]):
            run = self.store.get(run["id"])
            if run is None or run["state"] != states.RUNNING:
                return
            guard = self._guards.setdefault(run["id"], LiveGuard())
            known = {v.get("path") for v in run.get("violations") or [] if v.get("path")}
            findings = guard.check_files(files, self.policy_for(run), known, after)
            if not findings:
                return
            said = [f"{f.violation['detail']}, {f.violation['how']}" for f in findings]
            for f, text in zip(findings, said):
                self.store.add_event(run["id"], "error" if f.kill else "info",
                                     f"Policy: {text}", f.violation)
            fields = {"violations": (run.get("violations") or [])
                      + [f.violation for f in findings]}
            kill = next((t for f, t in zip(findings, said) if f.kill), None)
            if kill and not run.get("error"):
                fields["error"] = f"Stopped by policy: {kill}"
            self.store.update(run["id"], **fields)
            if kill and not run.get("error"):
                self.store.audit("conductor", "policy_kill", run["id"], {"reason": kill})
                self.kill(run)

    def _finish_attempt(self, run: dict, exit_info: dict, d: Path) -> None:
        run_id = run["id"]
        code = exit_info.get("code")
        result = self._last_result(run_id, run["attempt"])
        now = time.time()
        self.store.update(run_id, exit_code=code, runner_pid=None)
        ok = code == 0 and result is not None and not result.get("is_error")
        if ok:
            question = extract_question(result.get("text") or "")
            if question:
                gitops_safe(gitops.commit_all, run["worktree_path"],
                            f"conductor: run {run_id} work in progress")
                self.store.transition(run_id, states.RUNNING, states.WAITING_INPUT,
                                      "Agent is waiting for your answer", question=question,
                                      activity="Waiting for your answer", activity_at=now)
                self.store.add_event(run_id, "question", question)
                return
            msg = f"conductor: run {run_id}: {run['title']}"
            if run.get("worktree_path") and gitops_safe(gitops.merge_in_progress,
                                                        run["worktree_path"]):
                msg = (f"conductor: run {run_id}: merge {run['base_branch']} "
                       f"(conflicts resolved by the agent)")
            gitops_safe(gitops.commit_all, run["worktree_path"], msg)
            self.store.transition(run_id, states.RUNNING, states.TESTING,
                                  "Agent finished; running tests", test_status="running",
                                  activity="Running tests", activity_at=now)
            return
        # failure: keep partial work on the branch so it can be inspected
        gitops_safe(gitops.commit_all, run["worktree_path"],
                    f"conductor: run {run_id} partial work (failed)")
        if run.get("error"):
            err = run["error"]  # e.g. the watchdog's timeout
        elif result and result.get("is_error"):
            err = _describe_failure(result, run)
        elif result is None:
            tail = exit_info.get("stderr_tail") or _tail(d / "stderr.log", 600)
            err = f"Agent exited with code {code} without a result" + (f": {tail}" if tail else "")
        else:
            err = f"Agent exited with code {code}"
        self._update_diff_info(self.store.get(run_id))
        self.store.transition(run_id, states.RUNNING, states.FAILED, f"Failed: {err}",
                              error=err, activity="Failed", activity_at=now, finished_at=now)

    def _last_result(self, run_id: int, attempt: int) -> dict | None:
        for e in reversed(self.store.events(run_id, limit=50, kinds=["result"])):
            if e["attempt"] == attempt:
                return e.get("data") or {}
        return None

    # ----------------------------------------------------------------- tests
    def _run_tests(self, run_id: int) -> None:
        try:
            run = self.store.get(run_id)
            wt = run["worktree_path"]
            cmd = run.get("test_cmd")
            step = _current_step(run)
            if step is not None and step.tests == "skip":
                cmd = ""  # this workflow step does not run the tests
            if cmd is None:
                cmd = gitops.detect_test_cmd(wt) or ""
                self.store.update(run_id, test_cmd=cmd)
            if not cmd.strip():
                status, output = "none", "No test command configured for this repository."
            else:
                self.store.add_event(run_id, "test", f"Running tests: {cmd}")
                status, output = _run_test_cmd(
                    cmd, wt, self.cfg.test_timeout_secs,
                    on_start=lambda p: self._test_procs.__setitem__(run_id, p),
                    sandbox_tmp=(self.cfg.attempt_dir(run_id, run["attempt"]) / "tmp-tests"
                                 if self.policy_for(run).sandbox_writes == "worktree" else None))
                # Tests may leave artifacts behind; the result is the committed state.
                gitops_safe(gitops.run_git, wt, "checkout", "--", ".")
                gitops_safe(gitops.run_git, wt, "clean", "-fdq")
                self.store.add_event(run_id, "test", f"Tests {status}",
                                     {"tail": output[-2000:]})
            self.store.update(run_id, test_status=status, test_output=output[-TEST_OUTPUT_LIMIT:])
            self._update_diff_info(self.store.get(run_id))
            now = time.time()
            if self.store.transition(run_id, states.TESTING, states.FINISHED,
                                     f"Ready for review (tests: {status})",
                                     activity=f"Finished (tests {status})" if status != "none"
                                     else "Finished", activity_at=now, finished_at=now):
                self._advance_workflow(run_id)
        except Exception as e:
            log.exception("tests failed for run %s", run_id)
            self.store.update(run_id, test_status="failed", test_output=f"conductor error: {e}")
            self.store.transition(run_id, states.TESTING, states.FINISHED,
                                  f"Test step errored: {e}")
        finally:
            self._testing.discard(run_id)
            self._test_procs.pop(run_id, None)

    # ------------------------------------------------------------ workflows
    def _advance_workflow(self, run_id: int) -> None:
        """After a workflow step is done: start the next step, wait at its
        gate, or stop. A step is the next attempt on the same run, in the
        same worktree and branch, so each step builds on the last one's work.
        """
        with self.lock(run_id):
            run = self.store.get(run_id)
            if run["state"] != states.FINISHED or not run.get("workflow_def"):
                return
            wf = wfl.Workflow.from_dict(run["workflow_def"])
            i = run.get("workflow_step") or 0
            step, last = wf.steps[i], i + 1 >= len(wf.steps)
            if last:
                self.store.add_event(run_id, "info",
                                     f'Workflow "{wf.name}" complete ({len(wf.steps)} steps)')
                return
            if step.tests == "required" and run.get("test_status") not in ("passed", "none") \
                    and step.on_fail == "stop":
                self.store.add_event(run_id, "info", f'Workflow stopped at step "{step.id}": '
                                     "tests must pass before the next step")
                return
            if states.blocking_violations(run):
                self.store.add_event(run_id, "info", f'Workflow stopped at step "{step.id}": '
                                     "policy findings need a human")
                return
            nxt = wf.steps[i + 1]
            if step.gate == "approve":
                q = (f'Step {i + 1} of {len(wf.steps)} ("{step.id}") is done. Approve to start '
                     f'step {i + 2} ("{nxt.id}"). Reply "approve", or add instructions for '
                     "the next step; reject the run to stop here.")
                if self.store.transition(run_id, states.FINISHED, states.WAITING_INPUT,
                                         f'Waiting for approval to start "{nxt.id}"',
                                         question=q, pre_attempt="workflow_gate",
                                         activity="Waiting for your approval",
                                         activity_at=time.time()):
                    self.store.add_event(run_id, "question", q)
                return
            self.queue_workflow_step(run, wf, i + 1)

    def queue_workflow_step(self, run: dict, wf: "wfl.Workflow", index: int,
                            note: str | None = None, from_state: str = states.FINISHED) -> bool:
        prompt = wfl.render(wf, index, run["task"], run.get("summary"))
        if note:
            prompt += f"\n\nReviewer note before this step: {note}"
        step = wf.steps[index]
        return self.store.transition(
            run["id"], from_state, states.QUEUED,
            f'Workflow step {index + 1} of {len(wf.steps)}: "{step.id}"',
            workflow_step=index, next_prompt=prompt,
            resume=int(step.session == "continue" and bool(run.get("session_id"))),
            pre_attempt=None, question=None, test_status=None, error=None,
            activity=f'Queued: step "{step.id}"', activity_at=time.time())

    # ------------------------------------------------------------ schedules
    def _fire_due_schedules(self) -> None:
        """Fire schedules whose time has come. Each fire is claimed with a
        compare-and-set on next_fire_at first, so it happens exactly once
        even across a restart."""
        if self.create_run_cb is None:
            return
        now = time.time()
        for sc in self.store.list_schedules(due_before=now):
            try:
                self._fire_schedule(sc, now)
            except Exception:  # one broken schedule must not stop the others
                log.exception("schedule %s failed", sc["id"])

    def _fire_schedule(self, sc: dict, now: float) -> None:
        due = sc["next_fire_at"]
        if sc["kind"] == "cron":
            new_next = cron.parse(sc["cron"]).next_after(now)
            late = now - due > 120  # missed while the conductor was down
            if late and sc["catch_up"] == "skip":
                self.store.claim_schedule(sc["id"], due, new_next,
                                          last_note="Missed while down; catch-up is off")
                return
            reason = "Caught up after downtime" if late else "On schedule"
        else:  # on_change: next_fire_at is the next poll
            new_next = now + max(10, sc["poll_secs"] or 60)
            try:
                sha = gitops.rev_parse(sc["repo_path"], sc["watch_ref"])
            except gitops.GitError as e:
                self.store.claim_schedule(sc["id"], due, new_next, last_note=f"Cannot read "
                                          f"{sc['watch_ref']}: {e}")
                return
            if sha == sc.get("last_seen_sha") or not sc.get("last_seen_sha"):
                # first poll just records where the ref is; no change, no run
                self.store.claim_schedule(sc["id"], due, new_next, last_seen_sha=sha)
                return
            reason = f"{sc['watch_ref']} moved to {sha[:10]}"
        if sc["overlap"] == "skip" and sc.get("last_run_id"):
            prev = self.store.get(sc["last_run_id"])
            if prev and prev["state"] in (states.QUEUED, *states.ACTIVE, states.WAITING_INPUT):
                extra = {"last_seen_sha": sha} if sc["kind"] == "on_change" else {}
                self.store.claim_schedule(sc["id"], due, new_next, **extra,
                                          last_note=f"Skipped: run #{prev['id']} still active")
                return
        extra = {"last_seen_sha": sha} if sc["kind"] == "on_change" else {}
        if not self.store.claim_schedule(sc["id"], due, new_next, last_fired_at=now,
                                         last_note=reason, **extra):
            return  # someone else fired it
        try:
            run = self.create_run_cb(sc, reason)
        except Exception as e:
            self.store.update_schedule(sc["id"], last_note=f"Could not create run: {e}")
            return
        self.store.update_schedule(sc["id"], last_run_id=run["id"])
        self.store.audit(f"schedule:{sc['id']}", "scheduled_run", run["id"],
                         {"schedule": sc["name"], "reason": reason})

    # ------------------------------------------------------- derived info
    def _update_diff_info(self, run: dict) -> None:
        wt, base = run.get("worktree_path"), run.get("base_commit")
        if not base:
            return
        # A local run is read from its worktree; a remote run from the branch
        # its worker pushed into the repository.
        local = bool(wt) and Path(wt).exists()
        if not local and not run.get("worker_id"):
            return
        cwd, ref = (wt, "HEAD") if local else (run["repo_path"], run["branch"])
        try:
            base = gitops.review_base(cwd, run["base_branch"], ref, fallback=base)
            ranges = gitops.changed_ranges(cwd, base, None if local else ref)
            files = gitops.diff_files(cwd, base, ref)
            # Pre-merge policy check on exactly what would be merged.
            findings = pol.check_diff(self.policy_for(run), files, run["base_branch"])
            now = time.time()
            self.store.update(
                run["id"], diff_stat=gitops.diff_stat(cwd, base, ref),
                changed_files=(gitops.changed_files(wt, base) if local
                               else sorted(f["path"] for f in files)),
                changed_ranges={"base": base, "files": ranges},
                merge_check={**gitops.merge_check(run["repo_path"], run["base_branch"],
                                                  run["branch"]), "checked_at": now,
                             "policy": [{**f, "at": now} for f in findings]})
            self.store.set_run_files(run["id"], run["repo_path"], base, ranges)
            if findings:
                self.store.add_event(run["id"], "info", "Policy: " + "; ".join(
                    f["detail"] for f in findings[:3]) + (" …" if len(findings) > 3 else ""),
                    {"policy": findings})
        except gitops.GitError as e:
            log.warning("diff info for run %s: %s", run["id"], e)

    def _refresh_changed_files(self) -> None:
        for run in self.store.list([states.RUNNING, states.WAITING_INPUT]):
            wt, base = run.get("worktree_path"), run.get("base_commit")
            if wt and base and Path(wt).exists():
                try:
                    base = gitops.review_base(wt, run["base_branch"], fallback=base)
                    files = gitops.changed_files(wt, base)
                    ranges = {"base": base, "files": gitops.changed_ranges(wt, base)}
                except gitops.GitError:
                    continue
                # Backstop for the check after each tool call: anything that
                # wrote without a tool result (a background process, say).
                self.enforce_paths(run, files)
                if files != (run.get("changed_files") or []) or \
                        ranges != run.get("changed_ranges"):
                    self.store.update(run["id"], changed_files=files, changed_ranges=ranges)
                    self.store.set_run_files(run["id"], run["repo_path"], base,
                                             ranges["files"])

    def _refresh_merge_checks(self) -> None:
        """The target branch moves when other runs merge; re-check finished runs."""
        targets: dict[tuple[str, str], str | None] = {}  # one rev-parse per repo+branch
        for run in self.store.list([states.FINISHED, states.FAILED]):
            key = (run["repo_path"], run["base_branch"])
            if key not in targets:
                try:
                    targets[key] = gitops.rev_parse(*key)
                except gitops.GitError:
                    targets[key] = None
            target = targets[key]
            if target is None:
                continue
            old = run.get("merge_check") or {}
            if old.get("target_commit") != target:
                try:
                    mc = gitops.merge_check(run["repo_path"], run["base_branch"], run["branch"])
                except gitops.GitError:
                    continue
                # The policy findings depend on the run's own diff, not the
                # target: keep them.
                self.store.update(run["id"], merge_check={**mc, "checked_at": time.time(),
                                                          "policy": old.get("policy") or []})
                if mc.get("conflicts"):
                    self.store.add_event(run["id"], "info",
                                         f"{run['base_branch']} moved; now conflicts in "
                                         + ", ".join(mc["conflicts"]))

    # --------------------------------------------------------------- janitor
    def janitor(self) -> None:
        """Remove worktrees nobody needs. This is what stops disks filling up.

        A worktree is kept only if its run exists and is not terminal.
        Everything else under worktrees/ is removed, and ``git worktree prune``
        runs on every repo we know, so stale registrations go too.
        """
        self._last_janitor = time.time()
        runs = {r["id"]: r for r in self.store.list()}
        removed = 0
        if self.cfg.worktrees_dir.exists():
            for entry in self.cfg.worktrees_dir.iterdir():
                try:
                    run_id = int(entry.name.removeprefix("run-"))
                except ValueError:
                    run_id = None
                run = runs.get(run_id)
                if run and run["state"] not in states.TERMINAL:
                    continue
                repo = run["repo_path"] if run else None
                # Keep the branch only when the user chose "keep branch only".
                keep = run and run["state"] == states.ACCEPTED and \
                    run.get("result_ref") == run.get("branch")
                delete_branch = run["branch"] if run and not keep else None
                self._remove_worktree(repo, entry, delete_branch)
                removed += 1
        for repo in {r["repo_path"] for r in runs.values()}:
            gitops_safe(gitops.prune, repo)
        if removed:
            log.info("janitor removed %d stray worktree(s)", removed)
        self._refresh_disk_stats()

    def _refresh_disk_stats(self) -> None:
        n, total = 0, 0
        if self.cfg.worktrees_dir.exists():
            for entry in self.cfg.worktrees_dir.iterdir():
                n += 1
                total += gitops.dir_size(entry)
        self.stats_cache = {"worktree_bytes": total, "worktrees": n,
                            "computed_at": time.time()}

    def _remove_worktree(self, repo: str | None, path: Path, delete_branch: str | None) -> None:
        if repo and gitops.is_repo(repo):
            gitops.remove_worktree(repo, path, delete_branch=delete_branch)
        else:
            import shutil
            shutil.rmtree(path, ignore_errors=True)

    def cleanup_run(self, run: dict, delete_branch: bool) -> None:
        if run.get("worker_id"):
            # The worktree lives on the worker; the branch was pushed here.
            self.fleet.command(run["worker_id"], "cleanup", run["id"])
            if delete_branch and gitops.is_repo(run["repo_path"]):
                gitops_safe(gitops.run_git, run["repo_path"], "branch", "-D", run["branch"])
            self.store.add_event(run["id"], "info", "Asked the worker to clean up" +
                                 (" and deleted the branch" if delete_branch else ""))
            return
        wt = run.get("worktree_path")
        if wt:
            self._remove_worktree(run["repo_path"], Path(wt),
                                  run["branch"] if delete_branch else None)
        elif delete_branch and gitops.is_repo(run["repo_path"]):
            gitops_safe(gitops.run_git, run["repo_path"], "branch", "-D", run["branch"])
        self.store.add_event(run["id"], "info",
                             "Cleaned up worktree" + (" and branch" if delete_branch else ""))
        threading.Thread(target=self._refresh_disk_stats, daemon=True).start()

    def kill(self, run: dict) -> None:
        """Stop whatever is running for this run: the agent and/or its tests."""
        if run.get("worker_id"):
            self.fleet.command(run["worker_id"], "kill", run["id"])
            return
        test = self._test_procs.get(run["id"])
        if test and test.poll() is None:
            _kill_group(test.pid, grace=1.0)
        pid = run.get("runner_pid")
        d = self.cfg.attempt_dir(run["id"], run["attempt"])
        if pid and _runner_alive(pid, d):
            _kill_group(pid)


# ---------------------------------------------------------------- helpers
def conflict_prompt(run: dict, conflicts: list[str]) -> str:
    files = "\n".join(f"- {f}" for f in conflicts)
    return (f"The branch you worked on has merge conflicts with `{run['base_branch']}`, "
            f"which changed since you started. I have started the merge for you; these "
            f"files contain conflict markers:\n{files}\n\nResolve every conflict so that "
            f"both your change and the upstream change keep working, remove all markers, "
            f"run the tests, and summarise how you resolved each file.\n\n"
            f"Your original task was:\n{run['task']}")


def attempt_budget(run: dict) -> float | None:
    """The budget is per run, not per attempt: each attempt gets what is left."""
    budget = run.get("budget_usd")
    if budget:
        budget = round(budget - (run.get("cost_usd") or 0), 2)
        if budget < 0.05:
            raise RuntimeError(f"Budget used up (${run['cost_usd']:.2f} of "
                               f"${run['budget_usd']:.2f}); raise it to continue")
    return budget


def _current_step(run: dict):
    if not run.get("workflow_def"):
        return None
    wf = wfl.Workflow.from_dict(run["workflow_def"])
    i = run.get("workflow_step") or 0
    return wf.steps[i] if i < len(wf.steps) else None


def _pkg_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _runner_alive(pid: int, attempt_dir: Path) -> bool:
    """True if ``pid`` is alive AND is our runner for this attempt.

    Checking the command line guards against pid reuse after a reboot.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    proc = Path(f"/proc/{pid}")
    if not proc.exists():  # non-Linux: best effort
        return True
    try:
        stat = (proc / "stat").read_text()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return False
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "conductor.runner" in cmdline and str(attempt_dir) in cmdline


def _kill_group(pid: int, grace: float = 3.0) -> None:
    """SIGTERM the runner's process group, then SIGKILL whatever is left."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    def finish():
        deadline = time.time() + grace
        while time.time() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    threading.Thread(target=finish, daemon=True).start()


def sandboxed(policy: "pol.Policy", argv: list[str], worktree, private_tmp,
              env: dict) -> tuple[list[str], str | None]:
    """Wrap an agent's command line in the OS sandbox when the policy asks for
    it (``[sandbox] writes = "worktree"``). Fails closed: raises RuntimeError
    if the policy requires a sandbox this machine cannot provide."""
    if policy.sandbox_writes != "worktree":
        return argv, None
    ok, kind, detail = sandbox.available()
    if not ok:
        raise RuntimeError('the repository policy requires a write sandbox '
                           f'([sandbox] writes = "worktree"), but none is available: {detail}')
    wrapped, extra = sandbox.wrap(argv, worktree, private_tmp)
    env.update(extra)
    return wrapped, (f"Agent sandboxed ({kind}): only its worktree is writable; "
                     "/tmp is private")


def _run_test_cmd(cmd: str, cwd: str, timeout: int, on_start=None,
                  sandbox_tmp=None) -> tuple[str, str]:
    # The test command is the user's own (or detected from their repo), run in
    # their own worktree, so a shell is appropriate here. It gets its own
    # process group so a timeout or a cancel kills the whole test tree.
    # With a write sandbox, it runs inside it too: the agent wrote these
    # tests, and the conductor must not run them with more rights than it had.
    argv, env = (cmd, None)
    if sandbox_tmp is not None:
        wrapped, extra = sandbox.wrap(["/bin/sh", "-c", cmd], cwd, sandbox_tmp)
        argv, env = wrapped, {**os.environ, **extra}
    p = subprocess.Popen(argv, shell=sandbox_tmp is None, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                         errors="replace", start_new_session=True, env=env)
    if on_start:
        on_start(p)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p.pid, grace=1.0)
        out, _ = p.communicate()
        return "failed", (out or "") + f"\n[conductor] tests timed out after {timeout}s"
    if p.returncode < 0:
        return "failed", (out or "") + f"\n[conductor] tests killed (signal {-p.returncode})"
    return ("passed" if p.returncode == 0 else "failed"), out or ""


def _tail(path: Path, n: int) -> str:
    try:
        data = path.read_bytes()[-n:]
    except FileNotFoundError:
        return ""
    return data.decode("utf-8", "replace").strip()


def _describe_failure(result: dict, run: dict) -> str:
    sub = result.get("subtype") or "error"
    if "budget" in sub:
        return f"Budget cap reached (${run.get('budget_usd')})"
    if "max_turns" in sub:
        return "Hit the turn limit"
    text = (result.get("text") or "").strip().splitlines()
    return f"{sub}" + (f": {text[0][:200]}" if text else "")


def gitops_safe(fn, *args, **kw):
    """Call a git helper whose failure must not derail state handling."""
    try:
        if args and args[0] is None:
            return None
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001 - logged, deliberately non-fatal
        log.warning("%s failed: %s", getattr(fn, "__name__", fn), e)
        return None
