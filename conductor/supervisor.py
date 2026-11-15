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
            d = self.cfg.attempt_dir(run["id"], run["attempt"])
            if run.get("runner_pid") and _runner_alive(run["runner_pid"], d):
                self.store.add_event(run["id"], "info",
                                     "Conductor restarted; re-attached to running agent")
        self.janitor()

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        self._reap()
        for run in self.store.list(states.AGENT_ACTIVE):
            with self.lock(run["id"]):
                run = self.store.get(run["id"])
                if run["state"] in states.AGENT_ACTIVE:
                    self._monitor(run)
        for run in self.store.list([states.TESTING]):
            if run["id"] not in self._testing:
                self._testing.add(run["id"])
                threading.Thread(target=self._run_tests, args=(run["id"],),
                                 name=f"tests-{run['id']}", daemon=True).start()
        self._schedule()
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
    def _schedule(self) -> None:
        active = len(self.store.list(states.AGENT_ACTIVE))
        for run in self.store.list([states.QUEUED]):  # oldest first
            if active >= self.cfg.max_parallel:
                return
            if self._launch(run):
                active += 1

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
                    activity_at=now, started_at=run.get("started_at") or now):
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
            files = "\n".join(f"- {f}" for f in res["conflicts"])
            prompt = (f"The branch you worked on has merge conflicts with "
                      f"`{run['base_branch']}`, which changed since you started. I have "
                      f"started the merge for you; these files contain conflict markers:\n"
                      f"{files}\n\nResolve every conflict so that both your change and the "
                      f"upstream change keep working, remove all markers, run the tests, "
                      f"and summarise how you resolved each file.\n\n"
                      f"Your original task was:\n{run['task']}")
            self.store.add_event(run_id, "info",
                                 f"Merged {run['base_branch']} into the branch; conflicts in "
                                 f"{len(res['conflicts'])} file(s) handed to the agent")

        # The budget is per run, not per attempt: each attempt gets what is left.
        budget = run.get("budget_usd")
        if budget:
            budget = round(budget - (run.get("cost_usd") or 0), 2)
            if budget < 0.05:
                raise RuntimeError(f"Budget used up (${run['cost_usd']:.2f} of "
                                   f"${run['budget_usd']:.2f}); raise it to continue")
        resume = bool(run.get("resume") and run.get("session_id"))
        session_id = run["session_id"] if resume else str(uuid.uuid4())
        adapter = get_adapter(run["agent"])
        ok, why = adapter.available()
        if not ok:
            raise RuntimeError(why)
        spec = adapter.build(prompt=prompt, session_id=session_id, resume=resume,
                             model=run.get("model"), budget_usd=budget,
                             worktree=str(wt))

        d = self.cfg.attempt_dir(run_id, attempt)
        d.mkdir(parents=True, exist_ok=True)
        for f in ("stream.jsonl", "exit.json", "runner.json"):
            (d / f).unlink(missing_ok=True)
        (d / "prompt.txt").write_text(prompt)
        (d / "command.json").write_text(json.dumps({"argv": spec.argv, "resume": resume}))

        env = {**os.environ, **spec.env, "CONDUCTOR_RUN_ID": str(run_id)}
        env.pop("CLAUDECODE", None)  # let a nested Claude Code start normally
        proc = subprocess.Popen(
            [sys.executable, "-m", "conductor.runner", str(d), str(wt), "--", *spec.argv],
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
        if now - (run["attempt_started_at"] or now) > self.cfg.max_run_secs:
            self.store.add_event(run_id, "error",
                                 f"Exceeded max run time ({self.cfg.max_run_secs}s); stopping")
            self.store.update(run_id, error=f"Timed out after {self.cfg.max_run_secs}s")
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
        chunk = chunk[:end + 1]
        events, fields = [], {}
        cost_add = 0.0
        # Show paths relative to the worktree ("src/app.py", not the full path).
        # Only the parsed view changes; the transcript on disk stays verbatim.
        wt_prefix = (run.get("worktree_path") or "\0").rstrip("/") + "/"
        for raw in chunk.splitlines():
            p = streamparse.parse_line(raw.decode("utf-8", "replace").replace(wt_prefix, ""))
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
        self.store.ingest(run["id"], run["attempt"], run["stream_offset"] + len(chunk),
                          events, **fields)

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
            tail = _tail(d / "stderr.log", 600)
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
            if cmd is None:
                cmd = gitops.detect_test_cmd(wt) or ""
                self.store.update(run_id, test_cmd=cmd)
            if not cmd.strip():
                status, output = "none", "No test command configured for this repository."
            else:
                self.store.add_event(run_id, "test", f"Running tests: {cmd}")
                status, output = _run_test_cmd(
                    cmd, wt, self.cfg.test_timeout_secs,
                    on_start=lambda p: self._test_procs.__setitem__(run_id, p))
                # Tests may leave artifacts behind; the result is the committed state.
                gitops_safe(gitops.run_git, wt, "checkout", "--", ".")
                gitops_safe(gitops.run_git, wt, "clean", "-fdq")
                self.store.add_event(run_id, "test", f"Tests {status}",
                                     {"tail": output[-2000:]})
            self.store.update(run_id, test_status=status, test_output=output[-TEST_OUTPUT_LIMIT:])
            self._update_diff_info(self.store.get(run_id))
            now = time.time()
            self.store.transition(run_id, states.TESTING, states.FINISHED,
                                  f"Ready for review (tests: {status})",
                                  activity=f"Finished (tests {status})" if status != "none"
                                  else "Finished", activity_at=now, finished_at=now)
        except Exception as e:
            log.exception("tests failed for run %s", run_id)
            self.store.update(run_id, test_status="failed", test_output=f"conductor error: {e}")
            self.store.transition(run_id, states.TESTING, states.FINISHED,
                                  f"Test step errored: {e}")
        finally:
            self._testing.discard(run_id)
            self._test_procs.pop(run_id, None)

    # ------------------------------------------------------- derived info
    def _update_diff_info(self, run: dict) -> None:
        wt, base = run.get("worktree_path"), run.get("base_commit")
        if not wt or not base or not Path(wt).exists():
            return
        try:
            base = gitops.review_base(wt, run["base_branch"], fallback=base)
            self.store.update(
                run["id"], diff_stat=gitops.diff_stat(wt, base),
                changed_files=gitops.changed_files(wt, base),
                changed_ranges={"base": base, "files": gitops.changed_ranges(wt, base)},
                merge_check={**gitops.merge_check(run["repo_path"], run["base_branch"],
                                                  run["branch"]), "checked_at": time.time()})
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
                if files != (run.get("changed_files") or []) or \
                        ranges != run.get("changed_ranges"):
                    self.store.update(run["id"], changed_files=files, changed_ranges=ranges)

    def _refresh_merge_checks(self) -> None:
        """The target branch moves when other runs merge; re-check finished runs."""
        for run in self.store.list([states.FINISHED, states.FAILED]):
            try:
                target = gitops.rev_parse(run["repo_path"], run["base_branch"])
            except gitops.GitError:
                continue
            mc = run.get("merge_check") or {}
            if mc.get("target_commit") != target:
                try:
                    mc = gitops.merge_check(run["repo_path"], run["base_branch"], run["branch"])
                except gitops.GitError:
                    continue
                self.store.update(run["id"], merge_check={**mc, "checked_at": time.time()})
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
        test = self._test_procs.get(run["id"])
        if test and test.poll() is None:
            _kill_group(test.pid, grace=1.0)
        pid = run.get("runner_pid")
        d = self.cfg.attempt_dir(run["id"], run["attempt"])
        if pid and _runner_alive(pid, d):
            _kill_group(pid)


# ---------------------------------------------------------------- helpers
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


def _run_test_cmd(cmd: str, cwd: str, timeout: int, on_start=None) -> tuple[str, str]:
    # The test command is the user's own (or detected from their repo), run in
    # their own worktree, so a shell is appropriate here. It gets its own
    # process group so a timeout or a cancel kills the whole test tree.
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                         errors="replace", start_new_session=True)
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
