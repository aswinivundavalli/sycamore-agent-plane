"""Coordinator side of remote workers: registry, leases, events, results.

A worker is a process on any machine that can reach the coordinator over
HTTP and the repository over git. It asks for work (a *lease*), runs the agent
in its own worktree, streams the agent's raw output back, pushes the run's
branch into the repository, and reports the exit and the test result. The
coordinator keeps every decision: state changes, policy, attention, merges.

    worker                                   coordinator
    ------                                   -----------
    register {name, capacity}           ->   worker id
    heartbeat {running, ranges} (5 s)   <-   commands: kill / cleanup
    lease {free}                        <-   run specs (queued -> starting, CAS)
    started {attempt, session_id}       ->   starting -> running
    events {attempt, offset, data}      ->   Supervisor.ingest_chunk (offset-idempotent)
    finish {attempt, code, ...}         <-   next step: run tests (cmd) or nothing
    tests {attempt, status, output}     ->   testing -> finished, diff + policy check

Every call carries the run's attempt; a call for an attempt that is no longer
current is refused (409), so a worker that lost its lease cannot overwrite a
newer attempt. Event posts carry the byte offset of their first line: data the
coordinator already has is skipped, a gap is refused with the offset it does
have, so retries after any crash are safe.

A worker silent for longer than the lease (default 30 s) is marked lost and its
runs become *interrupted* - never restarted on their own, exactly like a local
agent whose process vanished.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

from . import gitops, states
from . import policy as pol
from .agents.base import extract_question  # noqa: F401  (documents the shared contract)
from .guard import LiveGuard

log = logging.getLogger("conductor.fleet")

LEASE_SECS = float(os.environ.get("CONDUCTOR_LEASE_SECS", "30"))


class FleetError(Exception):
    def __init__(self, message: str, status: int = 409, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


def remote_for(repo_path: str) -> str:
    """Where workers fetch the repository from and push run branches to.

    By default the repository path itself (workers on this machine, or on
    machines that mount it). CONDUCTOR_REPO_REMOTES maps a path to a git URL
    for workers elsewhere: {"/srv/repos/app": "ssh://git@host/app.git"}.
    """
    try:
        mapping = json.loads(os.environ.get("CONDUCTOR_REPO_REMOTES", "{}"))
    except ValueError:
        mapping = {}
    return mapping.get(repo_path, repo_path)


class Fleet:
    def __init__(self, sup):
        self.sup = sup
        self.store = sup.store
        self.cfg = sup.cfg
        self._commands: dict[str, list[dict]] = defaultdict(list)
        self._lock = threading.Lock()

    # ------------------------------------------------------------- registry
    def register(self, body: dict) -> dict:
        wid = str(body.get("worker_id") or "") or f"w-{uuid.uuid4().hex[:10]}"
        name = str(body.get("name") or wid)[:80]
        cap = max(1, min(int(body.get("capacity") or 4), 512))
        labels = [str(x) for x in body.get("labels") or []][:20]
        self.store.upsert_worker(wid, name, cap, labels)
        log.info("worker %s (%s) registered, capacity %d", wid, name, cap)
        return {"worker_id": wid, "lease_secs": LEASE_SECS}

    def command(self, wid: str, op: str, run_id: int) -> None:
        with self._lock:
            self._commands[wid].append({"op": op, "run_id": run_id})

    def heartbeat(self, wid: str, body: dict) -> dict:
        running = [int(r) for r in body.get("running") or []]
        if not self.store.touch_worker(wid, len(running)):
            raise FleetError("unknown worker; register again", 404)
        now = time.time()
        # Changed line ranges of in-flight runs feed the overlap index, so
        # "also editing" warnings work for remote runs while they run.
        for rid, info in (body.get("ranges") or {}).items():
            run = self.store.get(int(rid))
            if run and run.get("worker_id") == wid and run["state"] not in states.TERMINAL:
                files = info.get("files") or {}
                self.store.update(run["id"], changed_files=sorted(files),
                                  changed_ranges={"base": info.get("base"), "files": files},
                                  lease_expires_at=now + LEASE_SECS)
                self.store.set_run_files(run["id"], run["repo_path"], info.get("base"), files)
        with self._lock:
            cmds, self._commands[wid] = self._commands[wid], []
        # Anything the worker still holds but the coordinator has finished
        # with (e.g. after a restart lost the queued command) is cleaned too.
        for rid in running:
            run = self.store.get(rid)
            if run is None or run.get("worker_id") != wid:
                cmds.append({"op": "cleanup", "run_id": rid})
            elif run["state"] in states.TERMINAL and \
                    not any(c["run_id"] == rid for c in cmds):
                cmds.append({"op": "cleanup", "run_id": rid})
        return {"commands": cmds}

    def workers(self) -> dict:
        return {"workers": self.store.workers(), "lease_secs": LEASE_SECS}

    # ---------------------------------------------------------------- lease
    def lease(self, wid: str, body: dict) -> dict:
        """Hand out up to ``free`` queued runs. Fair across repositories (one
        busy repo cannot starve the others) and within each repository's
        max_parallel_per_repo policy. The queued -> starting compare-and-set
        guarantees a run is leased to exactly one worker (or the local one)."""
        if not self.store.touch_worker(wid, int(body.get("running") or 0)):
            raise FleetError("unknown worker; register again", 404)
        free = max(0, min(int(body.get("free") or 0), 64))
        if not free:
            return {"runs": []}
        active = self.store.list(states.AGENT_ACTIVE)
        per_repo: dict[str, int] = defaultdict(int)
        for r in active:
            per_repo[r["repo_path"]] += 1
        by_repo: dict[str, list[dict]] = defaultdict(list)
        for r in self.store.list([states.QUEUED]):  # oldest first
            if r.get("pre_attempt") == "workflow_gate":
                continue
            by_repo[r["repo_path"]].append(r)
        # round-robin across repos, least busy repo first
        order = sorted(by_repo, key=lambda repo: per_repo[repo])
        specs = []
        while free and any(by_repo[r] for r in order):
            for repo in order:
                if not free or not by_repo[repo]:
                    continue
                run = by_repo[repo].pop(0)
                cap = self.sup.policy_for(run).max_parallel_per_repo
                if cap is not None and per_repo[repo] >= cap:
                    by_repo[repo] = []
                    continue
                spec = self._claim(wid, run)
                if spec:
                    specs.append(spec)
                    per_repo[repo] += 1
                    free -= 1
        return {"runs": specs}

    def _claim(self, wid: str, run: dict) -> dict | None:
        from .supervisor import attempt_budget
        run_id = run["id"]
        with self.sup.lock(run_id):
            attempt = run["attempt"] + 1
            now = time.time()
            try:
                budget = attempt_budget(run)
            except RuntimeError as e:
                if self.store.transition(run_id, states.QUEUED, states.STARTING,
                                         f"Starting attempt {attempt}", attempt=attempt):
                    self.store.transition(run_id, states.STARTING, states.FAILED,
                                          f"Could not start: {e}", error=f"Could not start: {e}",
                                          finished_at=now)
                return None
            if not self.store.transition(
                    run_id, states.QUEUED, states.STARTING,
                    f"Leased to worker {wid} (attempt {attempt})",
                    attempt=attempt, attempt_started_at=now, last_output_at=now,
                    stream_offset=0, stalled=0, exit_code=None, runner_pid=None,
                    attempt_cost_usd=0, error=None, activity=f"Starting on {wid}",
                    activity_at=now, started_at=run.get("started_at") or now,
                    worker_id=wid, worktree_path=None, lease_expires_at=now + LEASE_SECS):
                return None
            run = self.store.get(run_id)
        self.sup._guards[run_id] = LiveGuard()
        policy = self.sup.policy_for(run)
        return {
            "run_id": run_id, "attempt": attempt, "title": run["title"], "task": run["task"],
            "repo_remote": remote_for(run["repo_path"]), "base_branch": run["base_branch"],
            "base_commit": run["base_commit"], "branch": run["branch"],
            "prompt": run["next_prompt"] or run["task"], "pre_attempt": run.get("pre_attempt"),
            "resume": bool(run.get("resume") and run.get("session_id")),
            "session_id": run.get("session_id"), "agent": run["agent"],
            "model": run.get("model"), "budget_usd": budget,
            "disallowed_tools": pol.claude_disallowed_tools(policy),
            "max_runtime_secs": min(self.cfg.max_run_secs,
                                    policy.max_runtime_secs or self.cfg.max_run_secs),
        }

    # -------------------------------------------------------------- reports
    def _current(self, wid: str, run_id: int, attempt: int, states_in) -> dict:
        run = self.store.get(run_id)
        if run is None:
            raise FleetError("unknown run", 404)
        if run.get("worker_id") != wid or run["attempt"] != int(attempt):
            raise FleetError("stale: this attempt is no longer leased to you")
        if run["state"] not in states_in:
            raise FleetError(f"run is {run['state']}", data={"state": run["state"]})
        self.store.update(run_id, lease_expires_at=time.time() + LEASE_SECS)
        return run

    def started(self, wid: str, run_id: int, body: dict) -> dict:
        with self.sup.lock(run_id):
            run = self._current(wid, run_id, body.get("attempt"), [states.STARTING])
            if body.get("merged_clean"):
                # resolve_conflicts: the target merged into the branch with no
                # conflicts, so no agent is needed; just re-test.
                self.store.add_event(run_id, "info", f"Merged {run['base_branch']} into the "
                                     "branch cleanly; no agent needed")
                self.store.transition(run_id, states.STARTING, states.RUNNING,
                                      pre_attempt=None)
                self.store.transition(run_id, states.RUNNING, states.TESTING,
                                      "Re-running tests after merge", test_status="running",
                                      activity="Running tests")
                return {"state": states.TESTING, "test_cmd": self._test_cmd(run)}
            if body.get("conflicts"):
                self.store.add_event(run_id, "info", f"Merged {run['base_branch']} into the "
                                     f"branch; conflicts in {len(body['conflicts'])} file(s) "
                                     "handed to the agent")
            if body.get("prompt"):
                self.store.add_event(run_id, "info", "Prompt for this attempt recorded by the "
                                     "worker", {"prompt": str(body["prompt"])[:4000]})
            self.store.transition(
                run_id, states.STARTING, states.RUNNING,
                f"Agent started on {wid} ({run['agent']}"
                f"{', resuming session' if body.get('resume') else ''})",
                session_id=body.get("session_id") or run.get("session_id"),
                remote_worktree=str(body.get("worktree") or "") or None,
                next_prompt=None, resume=0, pre_attempt=None,
                activity="Agent starting up", activity_at=time.time())
        return {"state": states.RUNNING}

    def events(self, wid: str, run_id: int, body: dict) -> dict:
        """Append stream lines. Idempotent by byte offset."""
        data = str(body.get("data") or "").encode("utf-8")
        offset = int(body.get("offset") or 0)
        with self.sup.lock(run_id):
            run = self._current(wid, run_id, body.get("attempt"),
                                [states.RUNNING, states.STARTING])
            have = run["stream_offset"]
            if offset > have:
                raise FleetError("gap in the stream", data={"offset": have})
            data = data[have - offset:]
            end = data.rfind(b"\n")
            if end >= 0:
                self.sup.ingest_chunk(run, data[:end + 1])
            return {"offset": self.store.get(run_id)["stream_offset"]}

    def finish(self, wid: str, run_id: int, body: dict) -> dict:
        with self.sup.lock(run_id):
            run = self._current(wid, run_id, body.get("attempt"), [states.RUNNING])
            exit_info = {"code": body.get("code"), "stderr_tail": body.get("stderr_tail")}
            self.store.update(run_id, runner_pid=None)
            self.sup._finish_attempt(run, exit_info, Path("/nonexistent"))
            run = self.store.get(run_id)
            if run["state"] == states.TESTING:
                return {"state": states.TESTING, "test_cmd": self._test_cmd(run)}
            return {"state": run["state"]}

    def tests(self, wid: str, run_id: int, body: dict) -> dict:
        with self.sup.lock(run_id):
            run = self._current(wid, run_id, body.get("attempt"), [states.TESTING])
            status = body.get("status") if body.get("status") in ("passed", "failed",
                                                                     "none") else "failed"
            output = str(body.get("output") or "")
            if status != "none":
                self.store.add_event(run_id, "test", f"Tests {status} (on {wid})",
                                     {"tail": output[-2000:]})
            self.store.update(run_id, test_status=status, test_output=output[-20_000:],
                              test_cmd=body.get("test_cmd", run.get("test_cmd")))
            self.sup._update_diff_info(self.store.get(run_id))
            now = time.time()
            if self.store.transition(run_id, states.TESTING, states.FINISHED,
                                     f"Ready for review (tests: {status})",
                                     activity=f"Finished (tests {status})" if status != "none"
                                     else "Finished", activity_at=now, finished_at=now):
                self.sup._advance_workflow(run_id)
            return {"state": self.store.get(run_id)["state"]}

    def failed(self, wid: str, run_id: int, body: dict) -> dict:
        """The worker could not prepare the attempt (git error, agent missing)."""
        with self.sup.lock(run_id):
            run = self._current(wid, run_id, body.get("attempt"),
                                [states.STARTING, states.RUNNING, states.TESTING])
            err = f"Could not start on {wid}: {str(body.get('error') or 'unknown')[:300]}"
            self.store.transition(run_id, [run["state"]], states.FAILED, err, error=err,
                                  activity="Failed to start", finished_at=time.time())
        return {"state": states.FAILED}

    def lost(self, wid: str, run_id: int, body: dict) -> dict:
        """The worker restarted and the agent process is gone: interrupted,
        never relaunched on its own (same rule as a local agent)."""
        with self.sup.lock(run_id):
            run = self._current(wid, run_id, body.get("attempt"),
                                [states.STARTING, states.RUNNING, states.TESTING])
            self.store.transition(run_id, [run["state"]], states.INTERRUPTED,
                                  f"Agent process on {wid} is gone (worker restarted)",
                                  activity="Interrupted: agent process disappeared",
                                  activity_at=time.time())
        return {"state": states.INTERRUPTED}

    def _test_cmd(self, run: dict) -> str:
        from .supervisor import _current_step
        step = _current_step(run)
        if step is not None and step.tests == "skip":
            return ""
        cmd = run.get("test_cmd")
        if cmd is None:
            cmd = gitops.detect_test_cmd(run["repo_path"]) or ""
        return cmd

    # --------------------------------------------------------------- expiry
    def expire_leases(self) -> None:
        """Runs whose worker went silent past the lease become interrupted."""
        now = time.time()
        lost = {w["id"] for w in self.store.workers()
                if w["state"] == "online" and now - w["last_seen"] > LEASE_SECS}
        for wid in lost:
            self.store.mark_worker_lost(wid)
            log.warning("worker %s lost (silent for over %ss)", wid, LEASE_SECS)
        if not lost and not any(w["state"] == "lost" for w in self.store.workers()):
            return
        gone = {w["id"] for w in self.store.workers() if w["state"] == "lost"}
        for run in self.store.list([states.STARTING, states.RUNNING, states.TESTING]):
            wid = run.get("worker_id")
            if wid in gone:
                with self.sup.lock(run["id"]):
                    if self.store.transition(
                            run["id"], [run["state"]], states.INTERRUPTED,
                            f"Worker {wid} stopped responding",
                            activity="Interrupted: worker lost", activity_at=now):
                        self.store.audit(f"worker:{wid}", "worker_lost", run["id"], None)
