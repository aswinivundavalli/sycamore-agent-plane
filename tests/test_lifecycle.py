"""End-to-end lifecycle tests: Service + Supervisor + real git + the fake agent.

No HTTP and no background thread: tests call ``sup.tick()`` themselves, so
every step is deterministic. "Conductor restart" is simulated by throwing the
Supervisor/Store away and building new ones on the same database while agent
processes keep running, which is exactly what a real restart looks like.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
import warnings
from pathlib import Path

from conductor import gitops, states
from conductor.config import Config
from conductor.db import Store
from conductor.service import ActionError, Service
from conductor.supervisor import Supervisor, _runner_alive

ROOT = Path(__file__).resolve().parent.parent


class LifecycleHarness(unittest.TestCase):
    """Shared setup: temp demo repo, conductor home, fake agent, ticks."""
    def setUp(self):
        # Tests drop Store/Popen objects on purpose to simulate a crash.
        warnings.simplefilter("ignore", ResourceWarning)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        subprocess.run([str(ROOT / "scripts" / "make-demo-repo.sh"), str(self.repo)],
                       check=True, capture_output=True)
        self._env = {k: os.environ.get(k) for k in ("FAKE_AGENT_STEP_SECS", "FAKE_AGENT_STATE_DIR")}
        os.environ["FAKE_AGENT_STEP_SECS"] = "0.05"
        os.environ["FAKE_AGENT_STATE_DIR"] = str(self.tmp / "fake-state")
        self.cfg = Config()
        self.cfg.home = self.tmp / "home"
        self.cfg.tick_secs = 0.05
        self.cfg.max_parallel = 4
        self.boot()

    def boot(self):
        """(Re)create the conductor's in-memory objects, as a restart would."""
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        self.sup = Supervisor(self.cfg, self.store)
        self.svc = Service(self.cfg, self.store, self.sup)

    def tearDown(self):
        # Stop/cancel clean up on a background thread; let it finish before
        # the temp repo is deleted under it.
        import threading
        for t in threading.enumerate():
            if t.name.startswith("cleanup-run-"):
                t.join(timeout=15)
        out = subprocess.run(["pgrep", "-f", f"conductor.runner {self.cfg.home}"],
                             capture_output=True, text=True).stdout
        for pid in out.split():
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # ------------------------------------------------------------ helpers
    def create(self, task, **kw):
        return self.svc.create_run({"repo_path": str(self.repo), "task": task,
                                    "agent": "fake", **kw})["id"]

    def wait_for(self, run_id, wanted, timeout=20):
        wanted = {wanted} if isinstance(wanted, str) else set(wanted)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sup.tick()
            run = self.store.get(run_id)
            if run["state"] in wanted:
                return run
            time.sleep(0.05)
        self.fail(f"run {run_id} stuck in {self.store.get(run_id)['state']}, wanted {wanted}")

    def git(self, *args):
        return gitops.run_git(self.repo, *args)


class LifecycleTest(LifecycleHarness):
    # -------------------------------------------------------------- tests
    def test_happy_path_merge_and_cleanup(self):
        rid = self.create("Add module [[fake:write textkit/extra.py]]X = 1\n[[/fake]]")
        run = self.wait_for(rid, states.FINISHED)
        self.assertEqual(run["test_status"], "passed")
        self.assertEqual(run["diff_stat"]["files"], 1)
        self.assertTrue(run["summary"])
        self.assertEqual(states.attention(run)[0], 90)

        self.svc.accept(rid, "merge")
        run = self.store.get(rid)
        self.assertEqual(run["state"], states.ACCEPTED)
        self.assertEqual(self.git("show", "main:textkit/extra.py").strip(), "X = 1")
        self.assertFalse(Path(run["worktree_path"]).exists())
        self.assertNotIn(run["branch"], gitops.list_branches(self.repo))
        self.assertEqual(len(gitops.list_worktrees(self.repo)), 1)
        with self.assertRaises(ActionError) as cm:
            self.svc.accept(rid, "merge")  # double click
        self.assertEqual(cm.exception.status, 409)

    def test_keep_branch_and_reject(self):
        a = self.create("A [[fake:write a.txt]]a\n[[/fake]]")
        b = self.create("B [[fake:write b.txt]]b\n[[/fake]]")
        self.wait_for(a, states.FINISHED)
        self.wait_for(b, states.FINISHED)
        self.svc.accept(a, "branch")
        ra = self.store.get(a)
        self.assertIn(ra["branch"], gitops.list_branches(self.repo))
        self.assertFalse(Path(ra["worktree_path"]).exists())
        self.svc.reject(b)
        rb = self.store.get(b)
        self.assertEqual(rb["state"], states.REJECTED)
        self.assertNotIn(rb["branch"], gitops.list_branches(self.repo))
        self.assertFalse(Path(rb["worktree_path"]).exists())

    def test_question_answer_resumes_same_session(self):
        rid = self.create("Choose [[fake:ask]] [[fake:write choice.txt]]chosen\n[[/fake]]")
        run = self.wait_for(rid, states.WAITING_INPUT)
        self.assertIn("A or B", run["question"])
        self.assertEqual(states.attention(run)[0], 100)
        session = run["session_id"]
        self.svc.answer(rid, "A")
        run = self.wait_for(rid, states.FINISHED)
        self.assertEqual(run["session_id"], session)
        self.assertEqual(run["attempt"], 2)
        self.assertIn("choice.txt", run["changed_files"])

    def test_failure_then_restart(self):
        rid = self.create("Break [[fake:fail]]")
        run = self.wait_for(rid, states.FAILED)
        self.assertTrue(run["error"])
        self.svc.restart(rid)
        self.wait_for(rid, [states.FAILED, states.FINISHED])
        self.assertEqual(self.store.get(rid)["attempt"], 2)

    def test_same_file_conflict_is_caught_before_merge(self):
        a = self.create("A [[fake:write shared.txt]]from A\n[[/fake]]"
                        "[[fake:write a_only.txt]]a\n[[/fake]]")
        b = self.create("B [[fake:write shared.txt]]from B\n[[/fake]]")
        self.wait_for(a, states.FINISHED)
        self.wait_for(b, states.FINISHED)
        self.svc.accept(a, "merge")
        self.sup._last_slow = 0
        self.sup.tick()  # target moved: merge checks refresh
        view = self.svc.detail(b)
        self.assertEqual(view["merge_check"]["conflicts"], ["shared.txt"])
        self.assertIn("resolve_conflicts", view["actions"])
        self.assertNotIn("accept", view["actions"])
        with self.assertRaises(ActionError) as cm:
            self.svc.accept(b, "merge")
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.git("show", "main:shared.txt").strip(), "from A")

        # Hand the conflict back to the agent: main is merged into the branch,
        # the agent resolves the markers, tests re-run, and it now merges.
        self.svc.resolve_conflicts(b)
        run = self.wait_for(b, states.FINISHED)
        self.assertTrue(run["merge_check"]["clean"], run["merge_check"])
        # The review shows only B's own change, not what it pulled in from main.
        self.assertEqual([f["path"] for f in self.svc.diff(b)["files"]], ["shared.txt"])
        self.assertEqual(run["diff_stat"]["files"], 1)
        self.svc.accept(b, "merge")
        merged = self.git("show", "main:shared.txt")
        self.assertIn("from A", merged)
        self.assertIn("from B", merged)
        self.assertNotIn("<<<<<<<", merged)
        # The merge the agent finished is labelled as such, not as the task.
        self.assertIn("(conflicts resolved by the agent)",
                      self.git("log", "--format=%s", "-8", "main"))

    def test_overlap_warning_while_running(self):
        a = self.create("A [[fake:write same.txt]]a\n[[/fake]] [[fake:sleep 4]]")
        b = self.create("B [[fake:write same.txt]]b\n[[/fake]] [[fake:sleep 4]]")
        deadline = time.time() + 15
        while time.time() < deadline:
            self.sup._last_slow = 0
            self.sup.tick()
            runs = {r["id"]: r for r in self.svc.list_view()["runs"]}
            if runs[a]["overlaps"] and runs[b]["overlaps"]:
                break
            time.sleep(0.1)
        self.assertEqual(runs[a]["overlaps"][0]["run_id"], b)
        self.assertEqual(runs[b]["overlaps"][0]["files"], ["same.txt"])

    def test_same_file_different_regions_is_not_an_overlap(self):
        core = (self.repo / "textkit" / "core.py").read_text()
        top = core.replace('"""Core text helpers."""', '"""Core text helpers (A)."""')
        bottom = core.replace("return text[:width]", "return text[:width]  # (B)")
        self.assertNotEqual(top, core)
        self.assertNotEqual(bottom, core)
        a = self.create(f"A [[fake:write textkit/core.py]]{top}[[/fake]] [[fake:sleep 4]]")
        b = self.create(f"B [[fake:write textkit/core.py]]{bottom}[[/fake]] [[fake:sleep 4]]")
        c = self.create(f"C [[fake:write textkit/core.py]]{top}[[/fake]] [[fake:sleep 4]]")
        deadline = time.time() + 15
        while time.time() < deadline:
            self.sup._last_slow = 0
            self.sup.tick()
            runs = {r["id"]: r for r in self.svc.list_view()["runs"]}
            if all(runs[i].get("changed_ranges") for i in (a, b, c)) and runs[a]["overlaps"]:
                break
            time.sleep(0.1)
        overlapping = {o["run_id"] for o in runs[a]["overlaps"]}
        self.assertIn(c, overlapping)      # same lines: flagged
        self.assertNotIn(b, overlapping)   # same file, far apart: not flagged
        self.assertEqual(runs[b]["overlaps"], [])

    def test_old_database_is_migrated(self):
        import sqlite3
        path = self.tmp / "old.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                    "task TEXT NOT NULL, repo_path TEXT NOT NULL, base_branch TEXT NOT NULL, "
                    "state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        con.commit()
        con.close()
        Store(path)
        cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(runs)")}
        self.assertIn("changed_ranges", cols)

    def test_agent_survives_conductor_restart(self):
        rid = self.create("Slow [[fake:sleep 2]]")
        self.wait_for(rid, states.RUNNING)
        pid = self.store.get(rid)["runner_pid"]
        self.boot()  # conductor "dies" and comes back
        self.sup.recover()
        self.assertEqual(self.store.get(rid)["state"], states.RUNNING)
        self.assertEqual(self.store.get(rid)["runner_pid"], pid)
        run = self.wait_for(rid, states.FINISHED)
        self.assertEqual(run["attempt"], 1)  # never relaunched

    def test_dead_agent_is_interrupted_and_restarts_exactly_once(self):
        rid = self.create("Long [[fake:sleep 30]]")
        run = self.wait_for(rid, states.RUNNING)
        os.killpg(run["runner_pid"], signal.SIGKILL)
        time.sleep(0.2)
        self.boot()
        self.sup.recover()
        run = self.wait_for(rid, states.INTERRUPTED)
        self.svc.restart(rid)
        for _ in range(2):
            with self.assertRaises(ActionError):
                self.svc.restart(rid)
        run = self.wait_for(rid, states.RUNNING)
        self.assertEqual(run["attempt"], 2)
        out = subprocess.run(["pgrep", "-f", f"conductor.runner {self.cfg.runs_dir}/run-{rid}/"],
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(len(out), 1)

    def test_crash_during_launch_is_interrupted_not_relaunched(self):
        rid = self.create("Never launched")
        self.assertTrue(self.store.transition(rid, states.QUEUED, states.STARTING, attempt=1))
        self.boot()
        self.sup.recover()
        self.assertEqual(self.store.get(rid)["state"], states.INTERRUPTED)

    def test_cancel_kills_agent_and_removes_worktree(self):
        rid = self.create("Long [[fake:sleep 30]]")
        run = self.wait_for(rid, states.RUNNING)
        d = self.cfg.attempt_dir(rid, run["attempt"])
        self.svc.cancel(rid)
        deadline = time.time() + 10
        while time.time() < deadline and (_runner_alive(run["runner_pid"], d)
                                          or Path(run["worktree_path"]).exists()):
            time.sleep(0.1)
        self.assertFalse(_runner_alive(run["runner_pid"], d))
        self.assertFalse(Path(run["worktree_path"]).exists())
        self.assertEqual(self.store.get(rid)["state"], states.CANCELLED)

    def test_janitor_removes_stray_worktrees(self):
        stray = self.cfg.worktrees_dir / "run-999"
        stray.mkdir(parents=True)
        rid = self.create("x [[fake:write x.txt]]x\n[[/fake]]")
        run = self.wait_for(rid, states.FINISHED)
        # Simulate a crash between "rejected" and the cleanup that follows it.
        self.store.transition(rid, states.FINISHED, states.REJECTED)
        self.sup.janitor()
        self.assertFalse(stray.exists())
        self.assertFalse(Path(run["worktree_path"]).exists())
        self.assertNotIn(run["branch"], gitops.list_branches(self.repo))

    def test_watchdog_flags_stall_then_times_out(self):
        self.cfg.stall_secs = 1
        self.cfg.max_run_secs = 3
        rid = self.create("Hang [[fake:hang]]")
        self.wait_for(rid, states.RUNNING)
        deadline = time.time() + 10
        while time.time() < deadline and not self.store.get(rid)["stalled"]:
            self.sup.tick()
            time.sleep(0.1)
        run = self.store.get(rid)
        self.assertTrue(run["stalled"])
        self.assertIn("wedged", states.attention(run)[1])
        run = self.wait_for(rid, states.FAILED, timeout=15)
        self.assertIn("Timed out", run["error"])

    def test_cancel_during_tests_kills_test_process(self):
        marker = self.tmp / "tests-ran-to-completion"
        rid = self.create("x [[fake:write x.txt]]x\n[[/fake]]",
                          test_cmd=f"sleep 20 && touch {marker}")
        self.wait_for(rid, states.TESTING)
        deadline = time.time() + 5
        while rid not in self.sup._test_procs and time.time() < deadline:
            time.sleep(0.05)
        proc = self.sup._test_procs[rid]
        self.svc.cancel(rid)
        proc.wait(timeout=5)  # raises if the test process survived
        self.assertEqual(self.store.get(rid)["state"], states.CANCELLED)
        self.assertFalse(marker.exists())

    def test_budget_is_per_run_not_per_attempt(self):
        rid = self.create("Spend [[fake:cost 1.0]] [[fake:fail]]", budget_usd=1.0)
        self.wait_for(rid, states.FAILED)
        self.assertAlmostEqual(self.store.get(rid)["cost_usd"], 1.0)
        self.svc.restart(rid)
        run = self.wait_for(rid, states.FAILED)
        self.assertIn("Budget used up", run["error"])
        self.assertIsNone(run["runner_pid"])  # never launched

    def test_a_branch_left_by_an_earlier_conductor_is_not_reused(self):
        # A conductor whose data was reset starts numbering runs at 1 again,
        # but the repository still has the old conductor/run-1.
        self.git("switch", "-q", "-c", "conductor/run-1")
        (self.repo / "stale.txt").write_text("old run's work\n")
        self.git("add", "stale.txt")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "old")
        old = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "-")
        rid = self.create("New work [[fake:write new.txt]]n\n[[/fake]]")
        self.assertEqual(self.store.get(rid)["branch"], "conductor/run-1-2")
        run = self.wait_for(rid, states.FINISHED)
        self.assertEqual(run["changed_files"], ["new.txt"])
        self.assertEqual(self.git("rev-parse", "conductor/run-1").strip(), old)

    def test_skip_tests(self):
        rid = self.create("x [[fake:write x.txt]]x\n[[/fake]]", test_cmd="")
        self.assertEqual(self.wait_for(rid, states.FINISHED)["test_status"], "none")


if __name__ == "__main__":
    unittest.main()
