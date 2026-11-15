"""Repeatable workflows and scheduled/triggered runs, end to end (real git,
real processes, the scripted agent, explicit supervisor ticks)."""

from __future__ import annotations

import time
import unittest

from conductor import states
from conductor.service import ActionError

from tests.test_lifecycle import LifecycleHarness

CHAIN = '''name = "chain"
description = "three steps, an approval gate after the second"
[[steps]]
id = "build"
prompt = "{task} [[fake:append notes.txt]]build\\n[[/fake]]"
[[steps]]
id = "check"
prompt = "Check it [[fake:append notes.txt]]check\\n[[/fake]]"
tests = "skip"
gate = "approve"
[[steps]]
id = "polish"
prompt = "Polish it [[fake:append notes.txt]]polish\\n[[/fake]]"
'''


class AutomationTest(LifecycleHarness):
    def commit_repo_file(self, rel: str, text: str):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                 "-m", f"add {rel}")

    # ------------------------------------------------------------ workflows
    def test_workflow_runs_its_steps_on_one_branch_with_a_gate(self):
        self.commit_repo_file(".conductor/workflows/chain.toml", CHAIN)
        rid = self.create("Make the thing", workflow="chain")
        run = self.wait_for(rid, states.WAITING_INPUT, timeout=30)
        self.assertEqual(run["workflow_step"], 1)
        self.assertIn("Approve", run["question"])
        self.assertEqual(run["attention_score"], 100)
        self.svc.answer(rid, "approve")
        run = self.wait_for(rid, states.FINISHED, timeout=30)
        self.assertEqual(run["workflow_step"], 2)
        self.assertEqual(run["attempt"], 3)
        # every step built on the previous one, in the same worktree
        self.svc.accept(rid, "merge")
        self.assertEqual(self.git("show", "main:notes.txt").split(), ["build", "check", "polish"])
        msgs = [e["message"] for e in self.store.events(rid)]
        self.assertTrue(any('Workflow "chain" complete' in m for m in msgs))
        self.assertIn("approve", [a["action"] for a in self.store.audit_log(rid)])

    def test_gate_answer_with_instructions_reaches_the_next_step(self):
        self.commit_repo_file(".conductor/workflows/chain.toml", CHAIN)
        rid = self.create("Thing", workflow="chain")
        self.wait_for(rid, states.WAITING_INPUT, timeout=30)
        self.svc.answer(rid, "Keep it short")
        self.wait_for(rid, [states.RUNNING, states.FINISHED], timeout=30)
        prompt = (self.cfg.attempt_dir(rid, 3) / "prompt.txt").read_text()
        self.assertIn("Reviewer note before this step: Keep it short", prompt)
        self.assertIn('step 3 of 3 ("polish")', prompt)

    def test_builtin_workflows_are_listed(self):
        names = [w["name"] for w in self.svc.workflows(str(self.repo))["workflows"]]
        for n in ("bugfix", "dependency-bump", "feature"):
            self.assertIn(n, names)

    def test_unknown_workflow_is_refused(self):
        with self.assertRaises(ActionError):
            self.create("x", workflow="nope")

    # ------------------------------------------------------------ schedules
    def schedule(self, **kw):
        body = {"name": "nightly", "repo_path": str(self.repo), "task": "tidy up",
                "agent": "fake", "kind": "cron", "cron": "0 3 * * *", **kw}
        return self.svc.create_schedule(body)

    def make_due(self, sid, seconds_ago=1.0):
        self.store.update_schedule(sid, next_fire_at=time.time() - seconds_ago)

    def test_cron_schedule_fires_once_per_slot_even_across_a_restart(self):
        sc = self.schedule()
        self.assertGreater(sc["next_fire_at"], time.time())
        self.make_due(sc["id"])
        self.sup.tick()
        self.boot()  # conductor restarts right after firing
        self.sup.tick()
        self.sup.tick()
        runs = [r for r in self.store.list() if r.get("schedule_id") == sc["id"]]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["group_key"], f"schedule:{sc['id']}")
        after = self.store.get_schedule(sc["id"])
        self.assertGreater(after["next_fire_at"], time.time())
        self.assertEqual(after["last_run_id"], runs[0]["id"])

    def test_missed_fires_catch_up_once_or_are_skipped(self):
        once = self.schedule(name="once", catch_up="once")
        skip = self.schedule(name="skip", catch_up="skip")
        self.make_due(once["id"], seconds_ago=3 * 86400)  # down for three days
        self.make_due(skip["id"], seconds_ago=3 * 86400)
        self.sup.tick()
        self.sup.tick()
        by = lambda sid: [r for r in self.store.list() if r.get("schedule_id") == sid]  # noqa
        self.assertEqual(len(by(once["id"])), 1)
        self.assertIn("Caught up", self.store.get_schedule(once["id"])["last_note"])
        self.assertEqual(len(by(skip["id"])), 0)

    def test_overlap_skip_does_not_stack_runs(self):
        sc = self.schedule(task="slow [[fake:sleep 30]]")
        self.make_due(sc["id"])
        self.sup.tick()
        self.make_due(sc["id"])
        self.sup.tick()
        runs = [r for r in self.store.list() if r.get("schedule_id") == sc["id"]]
        self.assertEqual(len(runs), 1)
        self.assertIn("still active", self.store.get_schedule(sc["id"])["last_note"])

    def test_on_change_trigger_fires_when_the_branch_moves(self):
        sc = self.svc.create_schedule({"name": "on main", "repo_path": str(self.repo),
                                       "task": "audit deps", "agent": "fake",
                                       "kind": "on_change", "watch_ref": "main"})
        self.make_due(sc["id"])
        self.sup.tick()
        runs = lambda: [r for r in self.store.list() if r.get("schedule_id") == sc["id"]]  # noqa
        self.assertEqual(runs(), [])  # nothing moved yet
        self.commit_repo_file("CHANGELOG.md", "x\n")
        self.make_due(sc["id"])
        self.sup.tick()
        self.assertEqual(len(runs()), 1)
        self.assertIn("main moved", self.store.get_schedule(sc["id"])["last_note"])

    def test_pause_resume_run_now_and_validation(self):
        sc = self.schedule()
        self.svc.schedule_op(sc["id"], "pause")
        self.make_due(sc["id"])
        self.sup.tick()
        self.assertEqual([r for r in self.store.list() if r.get("schedule_id")], [])
        out = self.svc.schedule_op(sc["id"], "run_now")
        self.assertIsNotNone(out["last_run_id"])
        self.svc.schedule_op(sc["id"], "resume")
        self.assertGreater(self.store.get_schedule(sc["id"])["next_fire_at"], time.time())
        with self.assertRaises(ActionError):
            self.schedule(cron="61 * * * *")
        with self.assertRaises(ActionError):
            self.schedule(workflow="nope")


if __name__ == "__main__":
    unittest.main()
