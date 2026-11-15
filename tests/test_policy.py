"""Tests for conductor.policy (layered, tighten-only guardrails)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conductor import policy as P
from conductor.policy import Policy, PolicyError


def pol(*layers: dict) -> Policy:
    return P.combine([(f"layer{i}", l) for i, l in enumerate(layers)])


class ParseTests(unittest.TestCase):
    FULL = """
[paths]
allowed = ["src/**", "tests/**"]
forbidden = [".github/**", "**/migrations/**"]
[diff]
max_files = 20
max_lines = 600
forbid_test_deletion = true
require_test_changes = false
[commands]
forbidden = ["git push", "curl"]
[limits]
max_budget_usd = 5
max_runtime_secs = 3600
max_parallel_per_repo = 20
[merge]
protected_targets = ["main", "release/*"]
require_approval_over_lines = 300
[violation]
on_forbidden_command = "kill"
on_forbidden_path = "flag"
"""

    def test_full_layer(self):
        d = P.parse(self.FULL, "x.toml")
        self.assertEqual(d["paths"]["allowed"], ["src/**", "tests/**"])
        self.assertEqual(d["diff"]["max_files"], 20)
        self.assertEqual(d["limits"]["max_budget_usd"], 5)
        self.assertEqual(d["violation"]["on_forbidden_path"], "flag")
        self.assertIsInstance(d, dict)

    def test_empty_is_fine(self):
        self.assertEqual(P.parse("", "x.toml"), {})

    def assertPolicyError(self, text, *needles):
        with self.assertRaises(PolicyError) as cm:
            P.parse(text, "/r/.conductor/policy.toml")
        msg = str(cm.exception)
        self.assertIn("/r/.conductor/policy.toml", msg)
        for n in needles:
            self.assertIn(n, msg)
        self.assertIsInstance(cm.exception, ValueError)

    def test_unknown_section(self):
        self.assertPolicyError("[pathz]\nallowed=[]", "pathz")

    def test_unknown_key(self):
        self.assertPolicyError("[diff]\nmax_filez = 3", "max_filez")

    def test_top_level_key(self):
        self.assertPolicyError("max_files = 3", "max_files")

    def test_bad_types(self):
        self.assertPolicyError('[diff]\nmax_files = "20"', "max_files", "'20'")
        self.assertPolicyError("[diff]\nmax_files = -1", "max_files")
        self.assertPolicyError("[diff]\nmax_files = true", "max_files")
        self.assertPolicyError("[diff]\nforbid_test_deletion = 1", "forbid_test_deletion")
        self.assertPolicyError('[paths]\nforbidden = ".github/**"', "forbidden")
        self.assertPolicyError("[paths]\nforbidden = [1]", "forbidden")
        self.assertPolicyError('[limits]\nmax_budget_usd = "5"', "max_budget_usd")
        self.assertPolicyError("[paths]\nallowed = 3", "allowed")

    def test_bad_enum(self):
        self.assertPolicyError('[violation]\non_forbidden_command = "warn"',
                               "on_forbidden_command", "warn")

    def test_invalid_toml(self):
        self.assertPolicyError("[diff\n", "invalid TOML")

    def test_run_limits_validated(self):
        with self.assertRaises(PolicyError) as cm:
            P.combine([("run", {"limits": {"max_budget": 3}})])
        self.assertIn("run", str(cm.exception))
        self.assertIn("max_budget", str(cm.exception))


class CombineTests(unittest.TestCase):
    def test_builtin_defaults(self):
        p = P.combine([])
        self.assertEqual(p.commands_forbidden, ("git push", "git reset --hard", "rm -rf /", "sudo"))
        self.assertFalse(p.forbid_test_deletion)
        self.assertEqual((p.on_forbidden_command, p.on_forbidden_path), ("kill", "kill"))
        self.assertIsNone(p.max_files)
        self.assertIsNone(p.max_budget_usd)
        self.assertEqual(p.paths_allowed, ())
        self.assertEqual(p, Policy())

    def test_repo_cannot_raise_cap(self):
        p = pol({"limits": {"max_budget_usd": 5, "max_runtime_secs": 600}, "diff": {"max_lines": 100}},
                {"limits": {"max_budget_usd": 50, "max_runtime_secs": 60}, "diff": {"max_lines": 1000}})
        self.assertEqual(p.max_budget_usd, 5.0)
        self.assertIsInstance(p.max_budget_usd, float)
        self.assertEqual(p.max_runtime_secs, 60)
        self.assertEqual(p.max_lines, 100)

    def test_cap_set_by_later_layer_only(self):
        self.assertEqual(pol({}, {"diff": {"max_files": 3}}).max_files, 3)

    def test_cannot_drop_forbidden_command(self):
        p = pol({"commands": {"forbidden": ["curl", "git push"]}},
                {"commands": {"forbidden": ["wget"]}})
        self.assertEqual(p.commands_forbidden,
                         ("git push", "git reset --hard", "rm -rf /", "sudo", "curl", "wget"))

    def test_forbidden_paths_and_targets_union(self):
        p = pol({"paths": {"forbidden": ["a/**"]}, "merge": {"protected_targets": ["main"]}},
                {"paths": {"forbidden": ["b/**", "a/**"]}, "merge": {"protected_targets": ["release/*", "main"]}})
        self.assertEqual(p.paths_forbidden, (".conductor/**", "a/**", "b/**"))
        self.assertEqual(p.protected_targets, ("main", "release/*"))

    def test_allowed_paths_only_narrow(self):
        p = pol({"paths": {"allowed": ["src/**"]}},
                {"paths": {"allowed": ["src/**", "docs/**"]}})
        self.assertEqual(p.paths_allowed, (("src/**",), ("src/**", "docs/**")))
        self.assertIsNone(P.check_path(p, "src/a.py"))
        # docs/ is allowed by the repo layer but not the conductor-wide one.
        self.assertEqual(P.check_path(p, "docs/x.md")["rule"], "outside_allowed_paths")

    def test_flag_cannot_weaken_kill(self):
        p = pol({"violation": {"on_forbidden_command": "kill", "on_forbidden_path": "kill"}},
                {"violation": {"on_forbidden_command": "flag", "on_forbidden_path": "flag"}})
        self.assertEqual((p.on_forbidden_command, p.on_forbidden_path), ("kill", "kill"))
        p = pol({"violation": {"on_forbidden_path": "flag"}},
                {"violation": {"on_forbidden_path": "kill"}})
        self.assertEqual(p.on_forbidden_path, "kill")

    def test_flag_when_chosen(self):
        p = pol({"violation": {"on_forbidden_path": "flag"}})
        self.assertEqual(p.on_forbidden_path, "flag")
        self.assertEqual(p.on_forbidden_command, "kill")

    def test_booleans_or(self):
        p = pol({"diff": {"forbid_test_deletion": True, "require_test_changes": False}},
                {"diff": {"forbid_test_deletion": False, "require_test_changes": True}})
        self.assertTrue(p.forbid_test_deletion)
        self.assertTrue(p.require_test_changes)

    def test_round_trip(self):
        p = pol(P.parse(ParseTests.FULL, "x"), {"paths": {"allowed": ["src/**"]}})
        d = p.to_dict()
        self.assertIsInstance(d["paths_allowed"][0], list)
        self.assertIsInstance(d["commands_forbidden"], list)
        import json
        d2 = json.loads(json.dumps(d))
        self.assertEqual(Policy.from_dict(d2), p)
        self.assertEqual(Policy.from_dict(d2).to_dict(), d)
        self.assertEqual(Policy.from_dict(Policy().to_dict()), Policy())


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.repo = Path(self.tmp.name) / "repo"
        self.home.mkdir()
        (self.repo / ".conductor").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_files(self):
        p, sources, sha = P.load(self.home, self.repo)
        self.assertEqual(p, Policy())
        self.assertEqual(sources, ["built-in"])
        self.assertRegex(sha, r"^[0-9a-f]{12}$")

    def test_layers_and_sha(self):
        _, _, sha0 = P.load(self.home, self.repo)
        (self.home / "policy.toml").write_text('[limits]\nmax_budget_usd = 5\n')
        repo_file = self.repo / ".conductor" / "policy.toml"
        repo_file.write_text('[limits]\nmax_budget_usd = 9\n[paths]\nforbidden = [".github/**"]\n')
        p, sources, sha1 = P.load(self.home, self.repo, {"diff": {"max_files": 4}})
        self.assertEqual(sources, ["built-in", str(self.home / "policy.toml"), str(repo_file), "run"])
        self.assertEqual(p.max_budget_usd, 5.0)
        self.assertEqual(p.paths_forbidden, (".conductor/**", ".github/**"))
        self.assertEqual(p.max_files, 4)
        self.assertNotEqual(sha0, sha1)
        self.assertEqual(P.load(self.home, self.repo, {"diff": {"max_files": 4}})[2], sha1)
        # Repo layer only: no home file in sources.
        (self.home / "policy.toml").unlink()
        _, sources, _ = P.load(self.home, self.repo)
        self.assertEqual(sources, ["built-in", str(repo_file)])

    def test_bad_file_names_source(self):
        f = self.repo / ".conductor" / "policy.toml"
        f.write_text("[diff]\nmax_files = 'x'\n")
        with self.assertRaises(PolicyError) as cm:
            P.load(self.home, self.repo)
        self.assertIn(str(f), str(cm.exception))


class GlobTests(unittest.TestCase):
    def test_double_star(self):
        g = P.glob_match
        self.assertTrue(g("migrations/x.py", "**/migrations/**"))
        self.assertTrue(g("a/b/migrations/x.py", "**/migrations/**"))
        self.assertFalse(g("a/migrationsx/x.py", "**/migrations/**"))
        self.assertTrue(g("a/b.py", "a/**/b.py"))
        self.assertTrue(g("a/x/y/b.py", "a/**/b.py"))
        self.assertTrue(g("src/x.py", "**/*.py"))
        self.assertTrue(g("x.py", "**/*.py"))

    def test_star_does_not_cross_slash(self):
        g = P.glob_match
        self.assertTrue(g("src/a.py", "src/*.py"))
        self.assertFalse(g("src/sub/a.py", "src/*.py"))
        self.assertTrue(g("src/a.py", "src/?.py"))
        self.assertFalse(g("src/ab.py", "src/?.py"))

    def test_dir_star_star(self):
        g = P.glob_match
        self.assertTrue(g(".github/workflows/ci.yml", ".github/**"))
        self.assertTrue(g(".github/x", ".github/**"))
        self.assertFalse(g(".githubx/x", ".github/**"))
        self.assertFalse(g("a/.github/x", ".github/**"))  # has a slash: anchored

    def test_basename(self):
        g = P.glob_match
        self.assertTrue(g("a/b/key.pem", "*.pem"))
        self.assertTrue(g("key.pem", "*.pem"))
        self.assertFalse(g("a/key.pem.txt", "*.pem"))
        self.assertTrue(g("a/node_modules/x.js", "node_modules"))
        self.assertTrue(g("./a/key.pem", "*.pem"))

    def test_char_class(self):
        self.assertTrue(P.glob_match("v1.txt", "v[0-9].txt"))
        self.assertFalse(P.glob_match("va.txt", "v[!a].txt"))

    def test_branch_match(self):
        self.assertTrue(P.branch_match("main", "main"))
        self.assertFalse(P.branch_match("feature/main", "main"))
        self.assertTrue(P.branch_match("release/1.2", "release/*"))
        self.assertFalse(P.branch_match("release/1/2", "release/*"))

    def test_is_test_path(self):
        yes = ["tests/x.py", "a/test/x.txt", "pkg/test_foo.py", "foo_test.py", "x/foo_test.go",
               "a.test.js", "b/c.test.tsx", "x.spec.rb", "src/__tests__/a.js"]
        no = ["src/foo.py", "testing.py", "src/contest.py", "latest/x.py", "foo_test.js"]
        for p in yes:
            self.assertTrue(P.is_test_path(p), p)
        for p in no:
            self.assertFalse(P.is_test_path(p), p)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.p = pol({"commands": {"forbidden": ["curl"]}})

    def hit(self, cmd):
        return P.check_command(self.p, cmd)

    def test_positives(self):
        for cmd in ["git push", "git push origin main", "git  push -f", "git\tpush",
                    "cd x && git push", "make; git push", "false || git push", "ls | git push",
                    "sleep 1 & git push", "echo hi\ngit push", "env FOO=1 git push",
                    "FOO=1 BAR=2 git push", "bash -c 'git push'", "sh -c \"cd x && git push\"",
                    "bash -lc 'git push'", "nohup git push", "time git push", "command git push",
                    "exec git push", "xargs -n 1 git push", "/usr/bin/git push", "git -C repo push",
                    "echo $(git push)", "(git push)", "git push > /dev/null 2>&1",
                    "sudo rm x", "rm -rf /", "git reset --hard HEAD~1", "curl https://x",
                    "eval 'git push'", "bash -c 'bash -c \"git push\"'"]:
            v = self.hit(cmd)
            self.assertIsNotNone(v, cmd)
            self.assertEqual(v["rule"], "forbidden_command")
            self.assertEqual(v["action"], "kill")

    def test_negatives(self):
        for cmd in ["git pushd", "echo git push", 'echo "git push"', "echo 'git push && sudo'",
                    "rm -rf /tmp/x", "git status", "git reset --soft HEAD", "grep -r sudo .",
                    "pushd x", "git commit -m 'git push later'", "", "   "]:
            self.assertIsNone(self.hit(cmd), cmd)

    def test_detail(self):
        v = self.hit("cd x && git push origin main")
        self.assertEqual(v["detail"], "ran `git push origin main` (forbidden: git push)")

    def test_unbalanced_quotes_fallback(self):
        self.assertIsNotNone(self.hit('git push "oops'))
        self.assertIsNone(self.hit('echo "oops'))

    def test_flag_action(self):
        p = pol({"violation": {"on_forbidden_command": "flag"}})
        self.assertEqual(P.check_command(p, "sudo ls")["action"], "flag")


class PathTests(unittest.TestCase):
    def setUp(self):
        self.p = pol({"paths": {"allowed": ["src/**", "tests/**"],
                                "forbidden": [".github/**", "**/migrations/**", "*.pem"]}})

    def test_ok(self):
        self.assertIsNone(P.check_path(self.p, "src/a.py"))
        self.assertIsNone(P.check_path(self.p, "./src/a.py"))
        self.assertIsNone(P.check_path(self.p, "src/x/../b.py"))

    def test_forbidden(self):
        v = P.check_path(self.p, ".github/workflows/ci.yml")
        self.assertEqual(v["rule"], "forbidden_path")
        self.assertIn(".github/workflows/ci.yml", v["detail"])
        self.assertEqual(v["action"], "kill")
        self.assertEqual(P.check_path(self.p, "src/app/migrations/1.py")["rule"], "forbidden_path")
        self.assertEqual(P.check_path(self.p, "src/k.pem")["rule"], "forbidden_path")

    def test_outside_allowed(self):
        v = P.check_path(self.p, "docs/x.md")
        self.assertEqual(v["rule"], "outside_allowed_paths")

    def test_outside_worktree(self):
        for path in ["/etc/passwd", "../x", "src/../../x", "..", "./../x"]:
            self.assertEqual(P.check_path(self.p, path)["rule"], "outside_worktree", path)

    def test_no_rules(self):
        self.assertIsNone(P.check_path(Policy(), "anything/at/all"))

    def test_flag(self):
        p = pol({"paths": {"forbidden": ["x"]}, "violation": {"on_forbidden_path": "flag"}})
        self.assertEqual(P.check_path(p, "x")["action"], "flag")


def F(path, status="M", a=1, d=0):
    return {"path": path, "status": status, "additions": a, "deletions": d}


class DiffTests(unittest.TestCase):
    def rules(self, p, files, target="feature"):
        return [v["rule"] for v in P.check_diff(p, files, target)]

    def test_clean(self):
        self.assertEqual(P.check_diff(Policy(), [F("a.py", a=1000)], "main"), [])

    def test_paths(self):
        p = pol({"paths": {"allowed": ["src/**"], "forbidden": [".github/**"]}})
        vs = P.check_diff(p, [F("src/a.py"), F(".github/ci.yml"), F("docs/x.md")], "f")
        self.assertEqual([v["rule"] for v in vs], ["forbidden_path", "outside_allowed_paths"])
        self.assertTrue(all(v["action"] == "block" for v in vs))
        self.assertIn(".github/ci.yml", vs[0]["detail"])
        self.assertIn("docs/x.md", vs[1]["detail"])

    def test_size(self):
        p = pol({"diff": {"max_files": 2, "max_lines": 10}})
        self.assertEqual(self.rules(p, [F("a", a=3), F("b", d=3)]), [])
        self.assertEqual(self.rules(p, [F("a", a=5), F("b", d=5), F("c", a=1)]), ["max_files", "max_lines"])
        vs = P.check_diff(p, [F("a", a=6, d=5)], "f")
        self.assertEqual(vs[0]["rule"], "max_lines")
        self.assertIn("11", vs[0]["detail"])

    def test_test_deletion(self):
        p = pol({"diff": {"forbid_test_deletion": True}})
        self.assertEqual(self.rules(p, [F("tests/test_a.py", "D")]), ["test_deleted"])
        self.assertEqual(self.rules(p, [F("src/a.py", "D"), F("tests/test_a.py", "M")]), [])
        self.assertEqual(self.rules(Policy(), [F("tests/test_a.py", "D")]), [])

    def test_tests_required(self):
        p = pol({"diff": {"require_test_changes": True}})
        self.assertEqual(self.rules(p, [F("src/a.py")]), ["tests_required"])
        self.assertEqual(self.rules(p, [F("src/a.py"), F("src/a_test.go", "A")]), [])
        self.assertEqual(self.rules(p, []), [])

    def test_approval_only_on_protected(self):
        p = pol({"merge": {"protected_targets": ["main", "release/*"], "require_approval_over_lines": 100}})
        big = [F("a", a=150)]
        vs = P.check_diff(p, big, "main")
        self.assertEqual(vs, [{"rule": "approval_required", "detail": vs[0]["detail"], "action": "approval"}])
        self.assertEqual(self.rules(p, big, "release/2.0"), ["approval_required"])
        self.assertEqual(self.rules(p, big, "feature/x"), [])
        self.assertEqual(self.rules(p, [F("a", a=100)], "main"), [])

    def test_approval_zero(self):
        p = pol({"merge": {"protected_targets": ["main"], "require_approval_over_lines": 0}})
        self.assertEqual(self.rules(p, [F("a", a=1)], "main"), ["approval_required"])

    def test_order(self):
        p = pol({"paths": {"forbidden": ["x"]}, "diff": {"max_files": 0, "require_test_changes": True},
                 "merge": {"protected_targets": ["main"], "require_approval_over_lines": 0}})
        self.assertEqual(self.rules(p, [F("x")], "main"),
                         ["forbidden_path", "max_files", "tests_required", "approval_required"])


class MiscTests(unittest.TestCase):
    def test_check_create(self):
        p = pol({"limits": {"max_budget_usd": 5}})
        self.assertEqual(P.check_create(p, 8), ["Budget $8 exceeds this repository's cap of $5"])
        self.assertEqual(P.check_create(p, 5), [])
        self.assertEqual(P.check_create(p, None), [])
        self.assertEqual(P.check_create(Policy(), 1000), [])
        self.assertIn("$5.50", P.check_create(p, 5.5)[0])

    def test_effective_budget(self):
        p = pol({"limits": {"max_budget_usd": 5}})
        self.assertEqual(P.effective_budget(p, 3), 3)
        self.assertEqual(P.effective_budget(p, 8), 5)
        self.assertEqual(P.effective_budget(p, None), 5)
        self.assertEqual(P.effective_budget(Policy(), 8), 8)
        self.assertIsNone(P.effective_budget(Policy(), None))

    def test_claude_disallowed_tools(self):
        p = pol({"paths": {"forbidden": [".github/**", ".github/**"]}, "commands": {"forbidden": ["curl"]}})
        self.assertEqual(P.claude_disallowed_tools(p), [
            "Bash(git push:*)", "Bash(git reset --hard:*)", "Bash(rm -rf /:*)", "Bash(sudo:*)",
            "Bash(curl:*)", "Edit(.conductor/**)", "Write(.conductor/**)",
            "Edit(.github/**)", "Write(.github/**)"])

    def test_describe(self):
        rules = {r["rule"]: r["value"] for r in P.describe(Policy())}
        self.assertEqual(rules["Forbidden paths"], ".conductor/**")  # built-in
        self.assertNotIn("Max budget", rules)
        self.assertIn("git push", rules["Forbidden commands"])
        p = pol({"paths": {"forbidden": [".github/**", "*.pem"]}, "limits": {"max_budget_usd": 5},
                 "merge": {"protected_targets": ["main"], "require_approval_over_lines": 300}})
        rules = {r["rule"]: r["value"] for r in P.describe(p)}
        self.assertEqual(rules["Forbidden paths"], ".conductor/**, .github/**, *.pem")
        self.assertEqual(rules["Max budget"], "$5")
        self.assertEqual(rules["Protected merge targets"], "main")
        self.assertEqual(rules["Approval required"], "over 300 lines")

    def test_agents_cannot_edit_the_policy(self):
        """Built-in: a run may not change .conductor/ (its own policy, workflows)."""
        v = P.check_path(Policy(), ".conductor/policy.toml")
        self.assertEqual((v["rule"], v["action"]), ("forbidden_path", "kill"))
        found = P.check_diff(Policy(), [{"path": ".conductor/workflows/x.toml", "status": "A",
                                         "additions": 3, "deletions": 0}], "main")
        self.assertEqual([f["rule"] for f in found], ["forbidden_path"])


if __name__ == "__main__":
    unittest.main()
