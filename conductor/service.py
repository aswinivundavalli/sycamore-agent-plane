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
import uuid
from pathlib import Path

from . import gitops, states
from . import cron
from . import policy as pol
from . import workflows as wfl
from .agents.registry import ADAPTER_NAMES
from .config import Config
from .db import Store
from .supervisor import Supervisor

log = logging.getLogger("conductor.service")


class ActionError(Exception):
    def __init__(self, message: str, status: int = 400, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


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
        sup.create_run_cb = self._run_from_schedule
        self.worker_token = _worker_token(cfg)

    # ------------------------------------------------------------- reading
    def get(self, run_id: int) -> dict:
        run = self.store.get(run_id)
        if not run:
            raise ActionError(f"run {run_id} not found", 404)
        return run

    def view(self, run: dict, overlaps: list[dict] | None = None) -> dict:
        v = dict(run)
        v["attention_score"], v["attention_reason"] = states.attention(run)
        v["actions"] = list(ACTIONS_BY_STATE.get(run["state"], []))
        mc = run.get("merge_check") or {}
        if run["state"] == states.FINISHED and (mc.get("conflicts") or mc.get("markers")):
            v["actions"] = [a for a in v["actions"] if a != "accept"] + ["resolve_conflicts"]
        if run["state"] == states.FINISHED and "accept" in v["actions"] and \
                states.blocking_violations(run):
            v["actions"] = [("accept_override" if a == "accept" else a) for a in v["actions"]]
        v["overlaps"] = overlaps or []
        v["test_output"] = run.get("test_output")
        return v

    def _overlaps(self, runs: list[dict]) -> dict[int, list[dict]]:
        """For each given run: other live runs on the same repo editing the same lines.

        The early warning for "two agents keep editing the same file",
        visible while both are still running. Candidates come from the
        ``run_files`` index (same repo, same path), so this costs the page
        being shown, not every pair of runs in the system. When both runs'
        ranges are measured from the same base commit, only overlapping or
        adjacent line ranges count; otherwise "same file" is reported.
        """
        live = [r["id"] for r in runs if r["state"] not in states.TERMINAL]
        found: dict[int, dict[int, dict]] = {}
        for c in self.store.overlap_candidates(live):
            if c["my_base"] and c["my_base"] == c["other_base"] and \
                    not gitops.ranges_overlap(c["my_ranges"], c["other_ranges"]):
                continue
            entry = found.setdefault(c["mine"], {}).setdefault(
                c["other"], {"run_id": c["other"], "title": c["other_title"],
                             "state": c["other_state"], "files": []})
            entry["files"].append(c["path"])
        return {rid: sorted(d.values(), key=lambda o: o["run_id"]) for rid, d in found.items()}

    def list_view(self, view: str | None = None, limit: int = 50, offset: int = 0,
                  repo: str | None = None, group: str | None = None,
                  q: str | None = None) -> dict:
        """The board. With ``view``: one page, most urgent first, plus the three
        runs to look at next and the groups. Without: every run (old clients)."""
        if view is None:
            runs, total = self.store.page("all", limit=100000, repo=repo, group=group, q=q)
            out = {"runs": self._views(runs), "total": total}
        else:
            limit = max(1, min(int(limit), 500))
            runs, total = self.store.page(view, limit=limit, offset=offset, repo=repo,
                                          group=group, q=q)
            nxt = offset + len(runs)
            out = {"runs": self._views(runs), "total": total,
                   "next_offset": nxt if nxt < total else None}
        top, _ = self.store.page("needs_you", limit=3)
        out["next3"] = self._views(top)
        out["groups"] = self.store.groups()
        out["stats"] = self.stats()
        return out

    def _views(self, runs: list[dict]) -> list[dict]:
        ov = self._overlaps(runs)
        return [self.view(r, ov.get(r["id"])) for r in runs]

    def detail(self, run_id: int) -> dict:
        run = self.get(run_id)
        v = self._views([run])[0]
        v["events"] = self.store.events(run_id, limit=300)
        v["audit"] = self.store.audit_log(run_id, limit=50)
        return v

    def stats(self, runs: list[dict] | None = None) -> dict:
        c = self.store.counts()
        recent = [r[0] for r in self.store.conn().execute(
            "SELECT repo_path FROM runs GROUP BY repo_path ORDER BY MAX(id) DESC LIMIT 10")]
        return {**c, **self.sup.stats_cache, "max_parallel": self.cfg.max_parallel,
                "recent_repos": recent}

    def policy_info(self, repo_path: str) -> dict:
        info = self.repo_info(repo_path)
        try:
            p, sources, sha = pol.load(self.cfg.home, Path(info["root"]))
        except pol.PolicyError as e:
            raise ActionError(f"Policy error: {e}")
        return {"policy": p.to_dict(), "rules": pol.describe(p), "sources": sources, "sha": sha}

    def audit_entries(self, run_id: int | None = None, limit: int = 200) -> dict:
        return {"entries": self.store.audit_log(run_id, limit=min(int(limit), 1000))}

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
    def create_run(self, body: dict, ctx: dict | None = None) -> dict:
        """``ctx`` lets a batch share the repository checks, policy and
        workflow lookups across its runs instead of repeating them per run."""
        ctx = {} if ctx is None else ctx
        task = (body.get("task") or "").strip()
        if not task:
            raise ActionError("task is required")
        if "info" not in ctx:
            ctx["info"] = self.repo_info(body.get("repo_path") or "")
        info = ctx["info"]
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
        # Guardrails, enforcement point 1: the repository's policy decides
        # what this run may ask for before it is ever queued.
        if "policy" not in ctx or body.get("policy_limits"):
            try:
                ctx["policy"] = pol.load(self.cfg.home, Path(info["root"]),
                                         body.get("policy_limits") or None)
            except pol.PolicyError as e:
                raise ActionError(f"Policy error: {e}")
        policy, _sources, sha = ctx["policy"]
        if body.get("budget_usd") not in (None, ""):
            errors = pol.check_create(policy, budget)
            if errors:
                raise ActionError("; ".join(errors), 422)
        budget = pol.effective_budget(policy, budget or None)
        title = (body.get("title") or "").strip() or _title_from_task(task)
        # A workflow is snapshotted onto the run, so editing its definition
        # later never changes a run already in flight.
        wf_fields = {}
        if body.get("workflow"):
            try:
                if "workflows" not in ctx:
                    ctx["workflows"] = wfl.load_all(self.cfg.home, Path(info["root"]))
                found = ctx["workflows"]
            except wfl.WorkflowError as e:
                raise ActionError(f"Workflow error: {e}")
            wf = found.get(body["workflow"])
            if wf is None:
                raise ActionError(f"unknown workflow {body['workflow']!r}; "
                                  f"available: {', '.join(found) or 'none'}")
            wf_fields = {"workflow": wf.name, "workflow_def": wf.to_dict(), "workflow_step": 0,
                         "next_prompt": wfl.render(wf, 0, task, None)}
        run = self.store.create_run(
            title=title, task=task, repo_path=info["root"], base_branch=base_branch,
            base_commit=ctx.setdefault(("base", base_branch),
                                       gitops.rev_parse(info["root"], base_branch)
                                       if ("base", base_branch) not in ctx else None),
            agent=agent, model=(body.get("model") or None), test_cmd=test_cmd,
            budget_usd=budget or None, activity="Waiting for a free slot",
            activity_at=time.time(), policy=policy.to_dict(), policy_sha=sha,
            group_key=body.get("group_key"), group_title=body.get("group_title"),
            schedule_id=body.get("schedule_id"), **wf_fields)
        extra = {}
        if run["branch"] in info["branches"]:
            # Left by an earlier conductor (its data was reset, the repository
            # kept its branches). Reusing it would start this run on that
            # run's work, so take the first free name instead.
            taken, n = set(info["branches"]), 2
            while f"{run['branch']}-{n}" in taken:
                n += 1
            extra["branch"] = f"{run['branch']}-{n}"
            self.store.add_event(run["id"], "info", f"Branch {run['branch']} already exists in "
                                 f"the repository; using {extra['branch']}")
        self.store.update(run["id"], worktree_path=str(self.cfg.worktree_for(run["id"])),
                          **extra)
        return self.get(run["id"])

    def answer(self, run_id: int, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ActionError("answer is empty")
        with self.sup.lock(run_id):
            run = self.get(run_id)
            self.store.add_event(run_id, "answer", text)
            if run.get("pre_attempt") == "workflow_gate" and run["state"] == states.WAITING_INPUT:
                # Approving a workflow gate starts the next step; anything
                # beyond "approve" is passed to that step as a note.
                wf = wfl.Workflow.from_dict(run["workflow_def"])
                note = None if text.lower().rstrip(".!") in ("approve", "approved", "yes", "ok",
                                                             "go", "continue") else text
                self.store.audit("user", "approve", run_id,
                                 {"step": wf.steps[run["workflow_step"] or 0].id, "note": note})
                if not self.sup.queue_workflow_step(run, wf, (run["workflow_step"] or 0) + 1,
                                                    note, from_state=states.WAITING_INPUT):
                    raise ActionError(f"run is {run['state']}, not waiting for approval", 409)
                return self.get(run_id)
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
            if not resume and run.get("workflow_def"):
                wf = wfl.Workflow.from_dict(run["workflow_def"])
                prompt = wfl.render(wf, run.get("workflow_step") or 0, run["task"],
                                    run.get("summary"))
                if note:
                    prompt += f"\n\nAdditional instructions: {note}"
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

    def accept(self, run_id: int, mode: str = "merge", override_reason: str | None = None,
               actor: str = "user") -> dict:
        if mode not in ("merge", "branch"):
            raise ActionError("mode must be 'merge' or 'branch'")
        override_reason = (override_reason or "").strip()
        with self.sup.lock(run_id):
            run = self.get(run_id)
            allowed = [states.FINISHED] if mode == "merge" else [states.FINISHED, states.FAILED]
            if run["state"] not in allowed:
                raise ActionError(f"cannot accept a run that is {run['state']}", 409)
            # Guardrails, enforcement point 4: a merge the policy blocks needs
            # a written reason, and that override is audited. Keeping the
            # branch merges nothing, so it is always allowed.
            blocking = states.blocking_violations(run) if mode == "merge" else []
            if blocking and not override_reason:
                self.store.audit(actor, "policy_block", run_id,
                                 {"violations": [v.get("detail") for v in blocking]})
                raise ActionError(
                    "Policy blocks this merge: " + "; ".join(
                        v.get("detail") or v.get("rule") for v in blocking[:5]) +
                    ". Merge with an override reason, keep the branch, or reject.", 409,
                    {"violations": blocking})
            if blocking:
                self.store.audit(actor, "override", run_id,
                                 {"reason": override_reason,
                                  "violations": [v.get("detail") for v in blocking],
                                  "policy_sha": run.get("policy_sha")})
                self.store.add_event(run_id, "info", f"Policy override: {override_reason}")
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
                                               run["branch"]), "checked_at": time.time(),
                          "policy": (run.get("merge_check") or {}).get("policy") or []}
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
            if mode == "merge":
                self.store.audit(actor, "merge", run_id, {"ref": ref, "target": run["base_branch"]})
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

    # ------------------------------------------------------------- workflows
    def workflows(self, repo_path: str | None) -> dict:
        root = Path(self.repo_info(repo_path)["root"]) if repo_path else None
        try:
            found = wfl.load_all(self.cfg.home, root)
        except wfl.WorkflowError as e:
            raise ActionError(f"Workflow error: {e}")
        return {"workflows": [{**w.to_dict(), "step_ids": [st.id for st in w.steps]}
                              for w in found.values()]}

    # ------------------------------------------------------------- schedules
    SCHEDULE_FIELDS = ("name", "repo_path", "base_branch", "task", "workflow", "agent",
                       "budget_usd", "kind", "cron", "watch_ref", "poll_secs", "overlap",
                       "catch_up")

    def create_schedule(self, body: dict) -> dict:
        b = {k: body.get(k) for k in self.SCHEDULE_FIELDS if body.get(k) not in (None, "")}
        for k in ("name", "repo_path", "task", "kind"):
            if not b.get(k):
                raise ActionError(f"{k} is required")
        info = self.repo_info(b["repo_path"])
        b["repo_path"] = info["root"]
        b["base_branch"] = b.get("base_branch") or info["current_branch"]
        if b["base_branch"] not in info["branches"]:
            raise ActionError(f"branch {b['base_branch']!r} does not exist")
        if b.get("overlap", "skip") not in ("skip", "queue") or \
                b.get("catch_up", "once") not in ("once", "skip"):
            raise ActionError("overlap must be skip|queue and catch_up once|skip")
        if b.get("workflow") and b["workflow"] not in wfl.load_all(self.cfg.home,
                                                                    Path(info["root"])):
            raise ActionError(f"unknown workflow {b['workflow']!r}")
        now = time.time()
        if b["kind"] == "cron":
            try:
                b["next_fire_at"] = cron.parse(b.get("cron") or "").next_after(now)
            except cron.CronError as e:
                raise ActionError(str(e))
        elif b["kind"] == "on_change":
            b["watch_ref"] = b.get("watch_ref") or b["base_branch"]
            try:
                sha = gitops.rev_parse(info["root"], b["watch_ref"])
            except gitops.GitError:
                raise ActionError(f"cannot resolve {b['watch_ref']!r} in {info['root']}")
            b["last_seen_sha"], b["next_fire_at"] = sha, now + int(b.get("poll_secs") or 60)
        else:
            raise ActionError("kind must be cron or on_change")
        return self.store.create_schedule(**b)

    def list_schedules(self) -> dict:
        return {"schedules": self.store.list_schedules()}

    def schedule_op(self, sid: int, op: str) -> dict:
        sc = self.store.get_schedule(sid)
        if not sc:
            raise ActionError(f"schedule {sid} not found", 404)
        now = time.time()
        if op == "pause":
            self.store.update_schedule(sid, enabled=0)
        elif op == "resume":
            nxt = (cron.parse(sc["cron"]).next_after(now) if sc["kind"] == "cron"
                   else now + (sc["poll_secs"] or 60))
            self.store.update_schedule(sid, enabled=1, next_fire_at=nxt)
        elif op == "run_now":
            run = self._run_from_schedule(sc, "Run now (manual)")
            self.store.update_schedule(sid, last_run_id=run["id"], last_fired_at=now,
                                       last_note="Run now (manual)")
            self.store.audit("user", "scheduled_run", run["id"], {"schedule": sc["name"],
                                                                  "reason": "run now"})
        elif op == "delete":
            self.store.delete_schedule(sid)
            return {"deleted": sid}
        else:
            raise ActionError(f"unknown schedule operation {op!r}")
        return self.store.get_schedule(sid)

    def _run_from_schedule(self, sc: dict, reason: str) -> dict:
        return self.create_run({
            "repo_path": sc["repo_path"], "base_branch": sc["base_branch"], "task": sc["task"],
            "workflow": sc.get("workflow"), "agent": sc.get("agent") or "claude",
            "budget_usd": sc.get("budget_usd"), "title": f"{sc['name']}: {reason}"[:120],
            "group_key": f"schedule:{sc['id']}", "group_title": sc["name"],
            "schedule_id": sc["id"]})

    # ------------------------------------------------------------- fleet
    def create_batch(self, body: dict) -> dict:
        """Many runs at once, one per task, shown as one group on the board."""
        tasks = [t.strip() for t in body.get("tasks") or [] if str(t).strip()]
        if not tasks:
            raise ActionError("tasks is required: a list of task descriptions")
        if len(tasks) > 1000:
            raise ActionError("at most 1000 tasks per batch")
        key = body.get("group_key") or f"batch:{uuid.uuid4().hex[:10]}"
        title = (body.get("title") or "").strip() or f"Batch of {len(tasks)}"
        common = {k: v for k, v in body.items() if k not in ("tasks", "title", "group_key")}
        ctx: dict = {}  # repository, policy and workflows resolved once for the batch
        ids = [self.create_run({**common, "task": t, "group_key": key,
                                "group_title": title}, ctx)["id"] for t in tasks]
        return {"group_key": key, "title": title, "runs": ids}

    GROUP_OPS = {
        "accept_ready": (states.FINISHED,),
        "reject_failed": (states.FAILED, states.INTERRUPTED),
        "restart_interrupted": (states.INTERRUPTED,),
    }

    def group_op(self, group_key: str, op: str) -> dict:
        """Bulk actions on a group. Each run goes through the same guarded
        action as a click would (policy, conflicts, compare-and-set); runs it
        does not apply to are skipped with the reason."""
        if op not in self.GROUP_OPS:
            raise ActionError(f"unknown group operation {op!r}")
        runs, _ = self.store.page("all", limit=100000, group=group_key)
        done, skipped = [], []
        for run in sorted(runs, key=lambda r: r["id"]):
            if run["state"] not in self.GROUP_OPS[op]:
                continue
            if op == "accept_ready" and run.get("test_status") not in ("passed", "none"):
                skipped.append({"id": run["id"], "reason": f"tests {run.get('test_status')}"})
                continue
            try:
                if op == "accept_ready":
                    self.accept(run["id"], "merge")
                elif op == "reject_failed":
                    self.reject(run["id"])
                else:
                    self.restart(run["id"])
                done.append(run["id"])
            except ActionError as e:
                skipped.append({"id": run["id"], "reason": str(e)})
        return {"done": done, "skipped": skipped}

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
        threading.Thread(target=go, name=f"cleanup-run-{run['id']}", daemon=True).start()


def _title_from_task(task: str) -> str:
    first = task.strip().splitlines()[0]
    return first if len(first) <= 72 else first[:69].rstrip() + "…"


def _worker_token(cfg: Config) -> str:
    """Shared secret workers present as a bearer token. CONDUCTOR_WORKER_TOKEN,
    or one generated once and kept in CONDUCTOR_HOME/worker_token (0600)."""
    tok = os.environ.get("CONDUCTOR_WORKER_TOKEN")
    if tok:
        return tok
    path = cfg.home / "worker_token"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        import secrets
        cfg.home.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok)
        return tok

