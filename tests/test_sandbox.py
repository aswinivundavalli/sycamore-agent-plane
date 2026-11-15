"""The write sandbox guardrail: `[sandbox] writes = "worktree"`.

The default policy deliberately keeps the gap (a shell command can write
anywhere the user can); these tests pin both sides of that: the flaw without
the setting, and the fix with it - for the agent's own commands, for a test
it plants that the conductor then runs, and fail-closed when no sandbox is
available.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import sandbox, states

from tests.test_lifecycle import LifecycleHarness

HAVE_SANDBOX = sandbox.available()[0]
SANDBOX_POLICY = '[sandbox]\nwrites = "worktree"\n'


class SandboxTest(LifecycleHarness):
    def setUp(self):
        super().setUp()
        # Escape targets live under the home directory, like real files would
        # (the sandbox gives the agent a private /tmp, so /tmp paths prove less).
        self.outside = Path(tempfile.mkdtemp(prefix="conductor-sbx-test-", dir=Path.home()))
        self.addCleanup(shutil.rmtree, self.outside, True)

    def set_policy(self, toml: str):
        (self.repo / ".conductor").mkdir(exist_ok=True)
        (self.repo / ".conductor" / "policy.toml").write_text(toml)
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
                 "-m", "policy")

    def escape_task(self) -> str:
        return (f"Escape [[fake:sh]]\necho escaped >> {self.outside}/bashrc\n"
                f"echo scratch > /tmp/conductor-sbx-{self.outside.name}.py\n[[/fake]]")

    def test_without_the_setting_the_gap_remains(self):
        """Documents the flaw: the default policy cannot see a shell write."""
        rid = self.create(self.escape_task())
        run = self.wait_for(rid, states.FINISHED)
        self.addCleanup(Path(f"/tmp/conductor-sbx-{self.outside.name}.py").unlink, True)
        self.assertTrue((self.outside / "bashrc").exists())
        self.assertFalse(run["violations"])
        self.assertEqual(run["attention_reason"], "Finished, tests pass: ready to merge")

    @unittest.skipUnless(HAVE_SANDBOX, "no OS sandbox on this machine")
    def test_with_the_setting_nothing_escapes_and_attempts_are_recorded(self):
        self.set_policy(SANDBOX_POLICY)
        rid = self.create(self.escape_task() + " [[fake:write ok.txt]]ok\n[[/fake]]")
        run = self.wait_for(rid, states.FINISHED)
        self.assertFalse((self.outside / "bashrc").exists())
        self.assertFalse(Path(f"/tmp/conductor-sbx-{self.outside.name}.py").exists())
        self.assertEqual(run["changed_files"], ["ok.txt"])  # inside the worktree still works
        rules = [(v["rule"], v["action"]) for v in run["violations"]]
        self.assertIn(("write_outside_worktree", "flag"), rules)
        self.assertEqual(run["attention_score"], 93)
        self.assertIn("refused", run["attention_reason"])
        notes = [e["message"] for e in self.store.events(rid)]
        self.assertTrue(any("Agent sandboxed" in m for m in notes))

    @unittest.skipUnless(HAVE_SANDBOX, "no OS sandbox on this machine")
    def test_a_planted_test_cannot_escape_when_the_conductor_runs_the_suite(self):
        test_file = (f"import unittest\n\n\nclass T(unittest.TestCase):\n"
                     f"    def test_x(self):\n"
                     f"        try:\n"
                     f"            open('{self.outside}/via-tests.txt', 'w').write('x')\n"
                     f"        except OSError:\n"
                     f"            pass\n")
        task = f"Plant [[fake:write tests/test_zz_plant.py]]{test_file}[[/fake]]"
        # without the setting, the conductor's own test run is the escape route
        rid = self.create(task)
        self.wait_for(rid, states.FINISHED)
        self.assertTrue((self.outside / "via-tests.txt").exists())
        (self.outside / "via-tests.txt").unlink()
        self.svc.reject(rid)
        # with it, the suite runs in the same sandbox as the agent
        self.set_policy(SANDBOX_POLICY)
        rid = self.create(task)
        run = self.wait_for(rid, states.FINISHED)
        self.assertEqual(run["test_status"], "passed")
        self.assertFalse((self.outside / "via-tests.txt").exists())

    def test_fails_closed_when_no_sandbox_is_available(self):
        self.set_policy(SANDBOX_POLICY)
        with mock.patch.object(sandbox, "available",
                               return_value=(False, "bubblewrap", "bubblewrap is not installed")):
            rid = self.create(self.escape_task())
            run = self.wait_for(rid, states.FAILED)
        self.assertIn("requires a write sandbox", run["error"])
        self.assertFalse((self.outside / "bashrc").exists())

    def test_the_setting_cannot_be_relaxed_by_a_later_layer(self):
        from conductor import policy as pol
        p = pol.combine([("home", {"sandbox": {"writes": "worktree"}}),
                         ("repo", {"sandbox": {"writes": "unrestricted"}})])
        self.assertEqual(p.sandbox_writes, "worktree")


if __name__ == "__main__":
    unittest.main()
