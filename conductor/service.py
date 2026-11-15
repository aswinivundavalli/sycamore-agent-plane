"""User-facing operations on runs: create, answer, restart, accept, reject...

The HTTP layer is a thin shell over this module. Each operation takes the
run's lock and moves state with a compare-and-set, so a double-clicked button
or two browser tabs can never accept a run twice or start it twice.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from . import gitops, states
from .agents.registry import ADAPTER_NAMES
from .config import Config
from .db import Store
from .supervisor import Supervisor

log = logging.getLogger("conductor.service")


class ActionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


ACTIONS_BY_STATE = {
    states.QUEUED: ["cancel"],
    states.STARTING: ["cancel"],
    states.RUNNING: ["cancel"],
    states.TESTING: ["cancel"],
    states.WAITING_INPUT: ["answer", "reject"],
    states.FINISHED: ["accept", "accept_branch", "reject", "restart", "retest"],
    states.FAILED: ["restart", "accept_branch", "reject"],
    states.INTERRUPTED: ["restart", "reject"],
}


class Service:
    def __init__(self, cfg: Config, store: Store, sup: Supervisor):
        self.cfg, self.store, self.sup = cfg, store, sup

    # ------------------------------------------------------------- reading
    def get(self, run_id: int) -> dict:
        run = self.store.get(run_id)
        if not run:
            raise ActionError(f"run {run_id} not found", 404)
        return run

    def view(self, run: dict, others: list[dict] | None = None) -> dict:
        v = dict(run)
        v["attention_score"], v["attention_reason"] = states.attention(run)
        v["actions"] = list(ACTIONS_BY_STATE.get(run["state"], []))
        mc = run.get("merge_check") or {}
        if run["state"] == states.FINISHED and (mc.get("conflicts") or mc.get("markers")):
            v["actions"] = [a for a in v["actions"] if a != "accept"] + ["resolve_conflicts"]
        v["overlaps"] = self._overlaps(run, others) if others is not None else []
        v["test_output"] = run.get("test_output")
        return v

    def _overlaps(self, run: dict, others: list[dict]) -> list[dict]:
        """Other live runs on the same repo editing the same lines.

        This is the early warning for "two agents keep editing the same file":
        visible while both are still running, not discovered at merge time.
        File-level matching is too noisy to be useful on codebases built
        around a few large modules (every run touches them), so when both
        runs' changes are measured against the same base commit, only
        overlapping or adjacent line ranges count. Otherwise it falls back
        to "same file".
        """
        mine = set(run.get("changed_files") or [])
        if not mine or run["state"] in states.TERMINAL:
            return []
        my_ranges = run.get("changed_ranges") or {}
        out = []
        for o in others:
            if o["id"] == run["id"] or o["state"] in states.TERMINAL:
                continue
            if o["repo_path"] != run["repo_path"]:
                continue
            common = mine & set(o.get("changed_files") or [])
            their_ranges = o.get("changed_ranges") or {}
            if common and my_ranges.get("base") and \
                    my_ranges.get("base") == their_ranges.get("base"):
                mf, tf = my_ranges.get("files") or {}, their_ranges.get("files") or {}
                common = {f for f in common if f not in mf or f not in tf
                          or gitops.ranges_overlap(mf[f], tf[f])}
            if common:
                out.append({"run_id": o["id"], "title": o["title"], "state": o["state"],
                            "files": sorted(common)})
        return out

    def list_view(self) -> dict:
        runs = self.store.list()
        views = [self.view(r, runs) for r in runs]
        views.sort(key=lambda v: (-v["attention_score"], -v["id"]))
        return {"runs": views, "stats": self.stats(runs)}

    def detail(self, run_id: int) -> dict:
        runs = self.store.list()
        run = next((r for r in runs if r["id"] == run_id), None)
        if not run:
            raise ActionError(f"run {run_id} not found", 404)
        v = self.view(run, runs)
        v["events"] = self.store.events(run_id, limit=300)
        return v

    def stats(self, runs: list[dict] | None = None) -> dict:
        runs = runs if runs is not None else self.store.list()
        counts: dict[str, int] = {}
        for r in runs:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        recent: list[str] = []
        for r in runs:  # newest first
            if r["repo_path"] not in recent:
                recent.append(r["repo_path"])
        return {"counts": counts, **self.sup.stats_cache,
                "max_parallel": self.cfg.max_parallel, "recent_repos": recent[:10],
                "needs_you": sum(1 for r in runs if states.attention(r)[0] >= 60)}

    def diff(self, run_id: int) -> dict:
        run = self.get(run_id)
        wt, base = run.get("worktree_path"), run.get("base_commit")
        if not base:
            return {"diff": "", "files": [], "merge_check": run.get("merge_check")}
        # After cleanup the worktree is gone, but the branch (or merge) may remain.
        cwd = wt if wt and Path(wt).exists() else run["repo_path"]
        ref = "HEAD" if cwd == wt else run.get("result_ref") or run["branch"]
        try:
            if cwd != wt and run["state"] == states.ACCEPTED and ref != run["branch"]:
                # merged: show the merge commit's first-parent diff
                text = gitops.run_git(cwd, "diff", "--no-color", "-M", f"{ref}^1", ref)
                files = gitops.diff_files(cwd, f"{ref}^1", ref)
            else:
                base = gitops.review_base(cwd, run["base_branch"], ref, fallback=base)
                text = gitops.diff(cwd, base, ref)
                files = gitops.diff_files(cwd, base, ref)
        except gitops.GitError as e:
            return {"diff": "", "files": [], "merge_check": run.get("merge_check"),
                    "error": f"Diff unavailable: {e}"}
        return {"diff": text, "files": files, "merge_check": run.get("merge_check")}

    def log(self, run_id: int, attempt: int | None = None, limit: int = 500) -> dict:
        run = self.get(run_id)
        attempt = attempt or run["attempt"]
        path = self.cfg.attempt_dir(run_id, attempt) / "stream.jsonl"
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 2_000_000))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except FileNotFoundError:
            lines = []
        return {"attempt": attempt, "lines": lines[-limit:], "path": str(path)}

    def repo_info(self, path: str) -> dict:
        path = os.path.expanduser((path or "").strip())
        if not path:
            raise ActionError("repo path is required")
        if not os.path.isdir(path):
            raise ActionError(f"{path} is not a directory")
        try:
            root = gitops.repo_root(path)
        except gitops.GitError:
            raise ActionError(f"{path} is not a git working copy")
        try:
            gitops.rev_parse(root, "HEAD")
        except gitops.GitError:
            raise ActionError(f"{root} has no commits yet")
        return {"path": path, "root": root, "branches": gitops.list_branches(root),
                "current_branch": gitops.current_branch(root),
                "test_cmd_guess": gitops.detect_test_cmd(root)}

    # ------------------------------------------------------------- writing
    def create_run(self, body: dict) -> dict:
        task = (body.get("task") or "").strip()
        if not task:
            raise ActionError("task is required")
        info = self.repo_info(body.get("repo_path") or "")
        base_branch = (body.get("base_branch") or info["current_branch"] or "").strip()
        if not base_branch:
            raise ActionError("repo is on a detached HEAD; pick a base branch")
        if base_branch not in info["branches"]:
            raise ActionError(f"branch {base_branch!r} does not exist in {info['root']}")
        agent = body.get("agent") or "claude"
        if agent not in ADAPTER_NAMES:
            raise ActionError(f"unknown agent {agent!r}; choose one of {ADAPTER_NAMES}")
        # None/absent = auto-detect, "" = no tests, anything else = run that.
        test_cmd = body.get("test_cmd")
        if test_cmd is not None:
            test_cmd = test_cmd.strip()
        if body.get("no_tests"):
            test_cmd = ""
        budget = body.get("budget_usd")
        budget = float(budget) if budget not in (None, "") else self.cfg.default_budget_usd
        title = (body.get("title") or "").strip() or _title_from_task(task)
        run = self.store.create_run(
            title=title, task=task, repo_path=info["root"], base_branch=base_branch,
            base_commit=gitops.rev_parse(info["root"], base_branch),
            agent=agent, model=(body.get("model") or None), test_cmd=test_cmd,
            budget_usd=budget or None, activity="Waiting for a free slot",
            activity_at=time.time())
        self.store.update(run["id"], worktree_path=str(self.cfg.worktree_for(run["id"])))
        return self.get(run["id"])

    def answer(self, run_id: int, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ActionError("answer is empty")
        with self.sup.lock(run_id):
            run = self.get(run_id)
            self.store.add_event(run_id, "answer", text)
            if not self.store.transition(run_id, states.WAITING_INPUT, states.QUEUED,
                                         "Answer received; resuming the agent",
                                         next_prompt=text, resume=1, question=None,
                                         activity="Resuming with your answer",
                                         activity_at=time.time()):
                raise ActionError(f"run is {run['state']}, not waiting for input", 409)
        return self.get(run_id)

    def restart(self, run_id: int, note: str | None = None) -> dict:
        with self.sup.lock(run_id):
            run = self.get(run_id)
            # Resume the agent's own session when we have one: it keeps its
            # context and sees its earlier work in the worktree.
            resume = bool(run.get("session_id"))
            if run["state"] == states.FAILED and run.get("error", "").startswith("Could not start"):
                resume = False
            prompt = (note or "").strip() or (
                "You were interrupted before finishing. The worktree still holds your "
                "work so far. Check its state, then continue and complete the original task."
                if resume else run["task"])
            if not resume and note:
                prompt = f"{run['task']}\n\nAdditional instructions: {note}"
            if not self.store.transition(
                    run_id, [states.INTERRUPTED, states.FAILED, states.FINISHED], states.QUEUED,
                    "Restart requested", next_prompt=prompt, resume=int(resume),
                    error=None, test_status=None, activity="Queued for restart",
                    activity_at=time.time()):
                raise ActionError(f"cannot restart a run that is {run['state']}", 409)
        return self.get(run_id)

    def cancel(self, run_id: int) -> dict:
        with self.sup.lock(run_id):
            run = self.get(run_id)
            from_states = [states.QUEUED, states.STARTING, states.RUNNING, states.TESTING]
            if not self.store.transition(run_id, from_states, states.CANCELLED,
                                         "Stopped by user", activity="Stopped",
                                         activity_at=time.time(), finished_at=time.time()):
                raise ActionError(f"cannot stop a run that is {run['state']}", 409)
            self.sup.kill(run)
        self._cleanup_later(run, delete_branch=True)
        return self.get(run_id)

    def reject(self, run_id: int) -> dict:
        with self.sup.lock(run_id):
            run = self.get(run_id)
            if not self.store.transition(
                    run_id, [states.FINISHED, states.FAILED, states.INTERRUPTED,
                             states.WAITING_INPUT], states.REJECTED, "Rejected by user",
                    activity="Rejected", activity_at=time.time(), finished_at=time.time()):
                raise ActionError(f"cannot reject a run that is {run['state']}", 409)
            self.sup.cleanup_run(run, delete_branch=True)
        return self.get(run_id)

    def accept(self, run_id: int, mode: str = "merge") -> dict:
        if mode not in ("merge", "branch"):
            raise ActionError("mode must be 'merge' or 'branch'")
        with self.sup.lock(run_id):
            run = self.get(run_id)
            allowed = [states.FINISHED] if mode == "merge" else [states.FINISHED, states.FAILED]
            if run["state"] not in allowed:
                raise ActionError(f"cannot accept a run that is {run['state']}", 409)
            if mode == "branch":
                ref = run["branch"]
                msg = f"Accepted: branch {ref} kept for you to merge"
            else:
                # Merge first, then record. If we crash in between, accepting
                # again is safe: merging an already-merged branch is a no-op.
                try:
                    ref = gitops.merge(run["repo_path"], run["base_branch"], run["branch"],
                                       f"Merge conductor run #{run_id}: {run['title']}")
                except gitops.MergeConflict as e:
                    mc = {**gitops.merge_check(run["repo_path"], run["base_branch"],
                                               run["branch"]), "checked_at": time.time()}
                    self.store.update(run_id, merge_check=mc)
                    what = (f"Conflict markers are still in: {', '.join(mc['markers'])}"
                            if mc.get("markers") else
                            f"Merging into {run['base_branch']} conflicts in: "
                            f"{', '.join(e.files)}")
                    raise ActionError(f"{what}. Ask the agent to resolve the conflicts, "
                                      f"or keep the branch and merge it yourself.", 409)
                except gitops.GitError as e:
                    raise ActionError(str(e), 409)
                msg = f"Merged into {run['base_branch']} at {ref[:10]}"
            self.store.transition(run_id, allowed, states.ACCEPTED, msg, result_ref=ref,
                                  activity=msg, activity_at=time.time(),
                                  finished_at=time.time())
            self.sup.cleanup_run(run, delete_branch=(mode == "merge"))
        return self.get(run_id)

    def resolve_conflicts(self, run_id: int) -> dict:
        with self.sup.lock(run_id):
            run = self.get(run_id)
            mc = run.get("merge_check") or {}
            pre, prompt = "merge_target", None
            if mc.get("markers") and not mc.get("conflicts"):
                # Nothing to merge; the branch itself still has markers in it.
                pre = None
                prompt = ("These files still contain merge conflict markers (<<<<<<< / "
                          ">>>>>>>): " + ", ".join(mc["markers"]) + ". Resolve them so both "
                          "sides' intent is kept, remove every marker, run the tests, and "
                          "summarise what you did.")
            if not self.store.transition(
                    run_id, states.FINISHED, states.QUEUED,
                    f"Queued: resolve conflicts with {run['base_branch']}",
                    pre_attempt=pre, resume=int(bool(run.get("session_id"))),
                    next_prompt=prompt, test_status=None,
                    activity="Queued to resolve conflicts", activity_at=time.time()):
                raise ActionError(f"run is {run['state']}, not finished", 409)
        return self.get(run_id)

    def retest(self, run_id: int) -> dict:
        with self.sup.lock(run_id):
            run = self.get(run_id)
            if not self.store.transition(run_id, states.FINISHED, states.TESTING,
                                         "Re-running tests", test_status="running",
                                         activity="Running tests", activity_at=time.time()):
                raise ActionError(f"run is {run['state']}, not finished", 409)
        return self.get(run_id)

    def _cleanup_later(self, run: dict, delete_branch: bool) -> None:
        """Give a just-killed agent a moment to exit before deleting its files."""
        def go():
            d = self.cfg.attempt_dir(run["id"], run["attempt"])
            from .supervisor import _runner_alive
            deadline = time.time() + 5
            while run.get("runner_pid") and _runner_alive(run["runner_pid"], d) \
                    and time.time() < deadline:
                time.sleep(0.2)
            try:
                self.sup.cleanup_run(run, delete_branch)
            except Exception as e:  # noqa: BLE001 - janitor retries later
                log.warning("cleanup of run %s failed (janitor will retry): %s", run["id"], e)
        threading.Thread(target=go, daemon=True).start()


def _title_from_task(task: str) -> str:
    first = task.strip().splitlines()[0]
    return first if len(first) <= 72 else first[:69].rstrip() + "…"
