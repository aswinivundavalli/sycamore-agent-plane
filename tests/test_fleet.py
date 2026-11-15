"""Fleet-scale behaviour: repository policy enforced end to end, attention
ordering, paging, groups and bulk actions.

Same harness as test_lifecycle: real git, real processes, the scripted agent,
and the supervisor driven by explicit ticks.
"""

from __future__ import annotations

import time
import unittest

from conductor import states
from conductor.service import ActionError

from tests.test_lifecycle import LifecycleHarness


class FleetTest(LifecycleHarness):
    def set_repo_policy(self, toml: str):
        (self.repo / ".conductor").mkdir(exist_ok=True)
        (self.repo / ".conductor" / "policy.toml").write_text(toml)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com",
                 "commit", "-q", "-m", "repo policy")

    # ------------------------------------------------------------- policy
    def test_forbidden_command_is_stopped_live(self):
        # "git push" is forbidden by the built-in defaults.
        rid = self.create("Ship it [[fake:bash git push origin main]] [[fake:sleep 20]]")
        run = self.wait_for(rid, states.FAILED, timeout=30)
        self.assertIn("Stopped by policy", run["error"])
        self.assertEqual([v["action"] for v in run["violations"]], ["kill"])
        self.assertEqual(run["attention_score"], 95)
        self.assertIn("Stopped by policy", run["attention_reason"])
        actions = [a["action"] for a in self.store.audit_log(rid)]
        self.assertIn("policy_kill", actions)

    def test_forbidden_path_blocks_merge_until_overridden(self):
        self.set_repo_policy('[paths]\nforbidden = ["secrets/**"]\n'
                             '[violation]\non_forbidden_path = "flag"\n')
        rid = self.create("Add a key [[fake:write secrets/key.txt]]k\n[[/fake]]")
        run = self.wait_for(rid, states.FINISHED)
        rules = [v["rule"] for v in run["merge_check"]["policy"]]
        self.assertIn("forbidden_path", rules)
        self.assertEqual(run["attention_score"], 95)
        view = self.svc.detail(rid)
        self.assertIn("accept_override", view["actions"])
        self.assertNotIn("accept", view["actions"])
        with self.assertRaises(ActionError) as cm:
            self.svc.accept(rid, "merge")
        self.assertEqual(cm.exception.status, 409)
        self.assertTrue(cm.exception.data["violations"])
        self.svc.accept(rid, "merge", override_reason="Test fixture key, not a secret")
        self.assertEqual(self.store.get(rid)["state"], states.ACCEPTED)
        audit = {a["action"]: a for a in self.store.audit_log(rid)}
        self.assertEqual(audit["override"]["detail"]["reason"], "Test fixture key, not a secret")
        self.assertIn("policy_block", audit)

    def test_protected_target_needs_approval(self):
        self.set_repo_policy('[merge]\nprotected_targets = ["main"]\n'
                             'require_approval_over_lines = 0\n')
        rid = self.create("Small change [[fake:write x.txt]]x\n[[/fake]]")
        run = self.wait_for(rid, states.FINISHED)
        self.assertEqual([v["action"] for v in run["merge_check"]["policy"]], ["approval"])
        with self.assertRaises(ActionError):
            self.svc.accept(rid, "merge")
        self.svc.accept(rid, "branch")  # keeping the branch merges nothing: allowed
        self.assertEqual(self.store.get(rid)["state"], states.ACCEPTED)

    def test_budget_over_repo_cap_is_refused_at_create(self):
        self.set_repo_policy("[limits]\nmax_budget_usd = 1\n")
        with self.assertRaises(ActionError) as cm:
            self.create("expensive", budget_usd=5)
        self.assertEqual(cm.exception.status, 422)
        rid = self.create("cheap")  # no budget asked: gets the cap
        self.assertEqual(self.store.get(rid)["budget_usd"], 1)

    def test_parallel_cap_per_repo(self):
        self.set_repo_policy("[limits]\nmax_parallel_per_repo = 1\n")
        a = self.create("A [[fake:sleep 2]]")
        b = self.create("B [[fake:sleep 2]]")
        self.wait_for(a, states.RUNNING)
        for _ in range(10):
            self.sup.tick()
            time.sleep(0.05)
        self.assertEqual(self.store.get(b)["state"], states.QUEUED)
        self.wait_for(a, states.FINISHED)
        self.wait_for(b, states.FINISHED)

    def test_policy_is_frozen_on_the_run(self):
        rid = self.create("x")
        run = self.store.get(rid)
        self.assertTrue(run["policy_sha"])
        self.assertIn("git push", run["policy"]["commands_forbidden"])

    # ---------------------------------------------------- attention + groups
    def test_longest_waiting_comes_first_within_a_tier(self):
        a = self.create("A [[fake:ask]]")
        self.wait_for(a, states.WAITING_INPUT)
        time.sleep(0.2)
        b = self.create("B [[fake:ask]]")
        self.wait_for(b, states.WAITING_INPUT)
        page = self.svc.list_view(view="needs_you")
        self.assertEqual([r["id"] for r in page["runs"]][:2], [a, b])
        self.assertEqual([r["id"] for r in page["next3"]][:2], [a, b])

    def test_batch_groups_and_bulk_actions(self):
        tasks = [f"ok {i} [[fake:write f{i}.txt]]{i}\n[[/fake]]" for i in range(3)]
        tasks += ["bad 1 [[fake:fail]]", "bad 2 [[fake:fail]]"]
        out = self.svc.create_batch({"repo_path": str(self.repo), "title": "Sweep",
                                     "tasks": tasks, "agent": "fake"})
        for rid in out["runs"]:
            self.wait_for(rid, [states.FINISHED, states.FAILED])
        groups = {g["group_key"]: g for g in self.store.groups()}
        g = groups[out["group_key"]]
        self.assertEqual((g["title"], g["total"], g["ready"], g["failed"]), ("Sweep", 5, 3, 2))
        res = self.svc.group_op(out["group_key"], "reject_failed")
        self.assertEqual(len(res["done"]), 2)
        res = self.svc.group_op(out["group_key"], "accept_ready")
        self.assertEqual(len(res["done"]), 3, res)
        g = {g["group_key"]: g for g in self.store.groups()}[out["group_key"]]
        self.assertEqual(g["done"], 5)

    def test_paging(self):
        out = self.svc.create_batch({"repo_path": str(self.repo), "title": "Many",
                                     "tasks": [f"t{i}" for i in range(7)], "agent": "fake"})
        first = self.svc.list_view(view="all", limit=3)
        self.assertEqual(first["total"], 7)
        self.assertEqual(first["next_offset"], 3)
        rest = self.svc.list_view(view="all", limit=10, offset=3)
        self.assertIsNone(rest["next_offset"])
        ids = [r["id"] for r in first["runs"] + rest["runs"]]
        self.assertEqual(sorted(ids), sorted(out["runs"]))


if __name__ == "__main__":
    unittest.main()
