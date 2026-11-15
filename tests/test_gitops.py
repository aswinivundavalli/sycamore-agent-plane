"""Tests for conductor.gitops. Each test builds throwaway repos in a temp dir."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conductor import gitops
from conductor.gitops import GitError, MergeConflict, run_git

_saved_env: dict[str, str | None] = {}


def setUpModule():
    # Isolate from the developer's global/system git config (aliases, hooks,
    # default branch names, signing, ...).
    global _cfg_dir
    _cfg_dir = tempfile.mkdtemp(prefix="gitops-cfg-")
    empty = os.path.join(_cfg_dir, "gitconfig")
    Path(empty).write_text("")
    for k, v in {"GIT_CONFIG_GLOBAL": empty, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        _saved_env[k] = os.environ.get(k)
        os.environ[k] = v


def tearDownModule():
    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_cfg_dir, ignore_errors=True)


def write(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class GitTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gitops-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        write(self.repo / "a.txt", "one\ntwo\nthree\n")
        write(self.repo / "b.txt", "bee\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")
        self.base = gitops.rev_parse(self.repo, "main")

    def git(self, *args, cwd=None):
        return run_git(cwd or self.repo, *args)

    def make_wt(self, name="run-1", branch="conductor/run-1", base="main"):
        path = self.tmp / "worktrees" / name
        base_sha = gitops.create_worktree(self.repo, path, branch, base)
        return path, base_sha

    def commit_on_main(self, files: dict, msg="main change"):
        for k, v in files.items():
            write(self.repo / k, v)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)


class BasicsTest(GitTestCase):
    def test_repo_root_and_is_repo(self):
        (self.repo / "sub").mkdir()
        self.assertEqual(os.path.realpath(gitops.repo_root(self.repo / "sub")),
                         os.path.realpath(self.repo))
        self.assertTrue(gitops.is_repo(self.repo))
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertFalse(gitops.is_repo(plain))
        self.assertFalse(gitops.is_repo(self.tmp / "missing"))
        with self.assertRaises(GitError):
            gitops.repo_root(plain)

    def test_branches_and_rev_parse(self):
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.git("branch", "feature")
        self.assertEqual(gitops.list_branches(self.repo), ["feature", "main"])
        self.assertEqual(len(gitops.rev_parse(self.repo, "feature")), 40)
        with self.assertRaises(GitError):
            gitops.rev_parse(self.repo, "nope")
        self.git("checkout", "-q", "--detach")
        self.assertIsNone(gitops.current_branch(self.repo))

    def test_run_git_errors(self):
        with self.assertRaises(GitError):
            run_git(self.repo, "not-a-command")
        with self.assertRaises(GitError):
            run_git(self.tmp / "missing", "status")
        self.assertEqual(run_git(self.repo, "rev-parse", "--verify", "nope", check=False), "")


class WorktreeTest(GitTestCase):
    def test_create_idempotent_and_list(self):
        path, base = self.make_wt()
        self.assertEqual(base, self.base)
        self.assertTrue((path / "a.txt").exists())
        self.assertEqual(gitops.current_branch(path), "conductor/run-1")
        # Calling again is a no-op returning the same base.
        self.assertEqual(self.make_wt()[1], self.base)
        wts = gitops.list_worktrees(self.repo)
        self.assertEqual(len(wts), 2)
        mine = [w for w in wts if os.path.realpath(w["path"]) == os.path.realpath(path)]
        self.assertEqual(mine[0]["branch"], "conductor/run-1")
        self.assertEqual(len(mine[0]["head"]), 40)
        # The original base is still reported after main moves on.
        self.commit_on_main({"c.txt": "c\n"})
        self.assertEqual(self.make_wt()[1], self.base)

    def test_create_reuses_existing_branch(self):
        path, _ = self.make_wt()
        write(path / "new.txt", "x\n")
        gitops.commit_all(path, "work")
        # Simulate a crash that lost the directory but kept the branch.
        shutil.rmtree(path)
        path2, base = self.make_wt()
        self.assertEqual(base, self.base)
        self.assertTrue((path2 / "new.txt").exists())

    def test_create_wrong_branch_raises(self):
        self.make_wt()
        with self.assertRaises(GitError):
            gitops.create_worktree(self.repo, self.tmp / "worktrees" / "run-1",
                                   "other", "main")

    def test_remove_tolerant(self):
        path, _ = self.make_wt()
        write(path / "junk.txt", "untracked\n")
        gitops.remove_worktree(self.repo, path, delete_branch="conductor/run-1")
        self.assertFalse(path.exists())
        self.assertNotIn("conductor/run-1", gitops.list_branches(self.repo))
        self.assertEqual(len(gitops.list_worktrees(self.repo)), 1)
        # Second removal: nothing left to remove, must not raise.
        gitops.remove_worktree(self.repo, path, delete_branch="conductor/run-1")

    def test_remove_with_dir_already_deleted(self):
        path, _ = self.make_wt()
        shutil.rmtree(path)
        gitops.remove_worktree(self.repo, path, delete_branch="conductor/run-1")
        self.assertEqual(len(gitops.list_worktrees(self.repo)), 1)
        self.assertNotIn("conductor/run-1", gitops.list_branches(self.repo))

    def test_remove_with_repo_gone(self):
        path, _ = self.make_wt()
        shutil.rmtree(self.repo)
        gitops.remove_worktree(self.repo, path, delete_branch="conductor/run-1")
        self.assertFalse(path.exists())

    def test_create_after_stale_registration(self):
        path, _ = self.make_wt()
        shutil.rmtree(path)  # stale registration remains until pruned
        gitops.remove_worktree(self.repo, path)  # keeps the branch
        _, base = self.make_wt(name="run-1b")
        self.assertEqual(base, self.base)


class ChangesTest(GitTestCase):
    def test_commit_all_with_untracked(self):
        path, _ = self.make_wt()
        self.assertIsNone(gitops.commit_all(path, "nothing"))
        write(path / "new" / "file.txt", "hello\n")
        sha = gitops.commit_all(path, "add file")
        self.assertEqual(sha, gitops.rev_parse(path, "HEAD"))
        self.assertIn("new/file.txt", self.git("ls-files", cwd=path).splitlines())
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=path), "add file")
        self.assertIsNone(gitops.commit_all(path, "again"))

    def test_commit_all_fallback_identity(self):
        path, _ = self.make_wt()
        self.git("config", "--unset", "user.name")
        self.git("config", "--unset", "user.email")
        write(path / "x.txt", "x\n")
        # Make sure nothing in the environment supplies an identity either.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "EMAIL"))}
        with mock.patch.dict(os.environ, env, clear=True):
            gitops.commit_all(path, "anon")
            self.assertEqual(self.git("log", "-1", "--format=%an <%ae>", cwd=path),
                             "Conductor <conductor@localhost>")

    def test_commit_all_skips_hooks(self):
        path, _ = self.make_wt()
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        write(hook, "#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        write(path / "x.txt", "x\n")
        self.assertIsNotNone(gitops.commit_all(path, "despite hook"))

    def test_changed_files_includes_uncommitted(self):
        path, base = self.make_wt()
        write(path / "committed.txt", "c\n")
        gitops.commit_all(path, "c")
        write(path / "a.txt", "changed\n")          # unstaged edit
        write(path / "staged.txt", "s\n")
        self.git("add", "staged.txt", cwd=path)     # staged new file
        write(path / "untracked dir" / "u.txt", "u\n")
        write(path / ".gitignore", "*.log\n")
        write(path / "ignored.log", "nope\n")
        os.remove(path / "b.txt")                   # deletion
        self.assertEqual(gitops.changed_files(path, base), sorted([
            ".gitignore", "a.txt", "b.txt", "committed.txt", "staged.txt",
            "untracked dir/u.txt"]))

    def test_diff_files_statuses_and_stat(self):
        path, base = self.make_wt()
        write(path / "a.txt", "one\nTWO\nthree\nfour\n")       # M +2 -1
        os.remove(path / "b.txt")                               # D
        write(path / "added.txt", "x\ny\n")                    # A
        (path / "bin.dat").write_bytes(b"\x00\x01\x02binary")  # A, binary
        gitops.commit_all(path, "edits")
        # Rename with identical content.
        long_text = "".join(f"line {i}\n" for i in range(20))
        write(path / "old_name.txt", long_text)
        gitops.commit_all(path, "add old")
        self.git("mv", "old_name.txt", "new_name.txt", cwd=path)
        gitops.commit_all(path, "rename")
        files = {f["path"]: f for f in gitops.diff_files(path, base)}
        self.assertEqual(files["a.txt"]["status"], "M")
        self.assertEqual((files["a.txt"]["additions"], files["a.txt"]["deletions"]), (2, 1))
        self.assertEqual(files["b.txt"]["status"], "D")
        self.assertEqual(files["b.txt"]["deletions"], 1)
        self.assertEqual(files["added.txt"]["status"], "A")
        self.assertEqual(files["bin.dat"]["status"], "A")
        self.assertEqual((files["bin.dat"]["additions"], files["bin.dat"]["deletions"]), (0, 0))
        # Rename relative to an intermediate commit.
        mid = gitops.rev_parse(path, "HEAD~1")
        ren = gitops.diff_files(path, mid)
        self.assertEqual(ren, [{"path": "new_name.txt", "status": "R",
                                "additions": 0, "deletions": 0}])
        self.assertEqual(gitops.diff_stat(path, base),
                         {"files": 5, "additions": 2 + 2 + 20, "deletions": 1 + 1})
        text = gitops.diff(path, base)
        self.assertIn("+TWO", text)
        self.assertIn("diff --git a/b.txt b/b.txt", text)
        self.assertNotIn("\x1b[", text)

    def test_diff_ignores_uncommitted(self):
        path, base = self.make_wt()
        write(path / "a.txt", "dirty\n")
        self.assertEqual(gitops.diff(path, base), "")
        self.assertEqual(gitops.diff_stat(path, base), {"files": 0, "additions": 0, "deletions": 0})


class MergeTest(GitTestCase):
    BR = "conductor/run-1"

    def branch_change(self, files: dict, msg="branch change"):
        path = self.tmp / "worktrees" / "run-1"
        if not path.exists():
            self.make_wt()
        for k, v in files.items():
            write(path / k, v)
        gitops.commit_all(path, msg)
        return path

    def test_merge_check_clean_and_behind(self):
        self.branch_change({"a.txt": "one\ntwo\nthree\nbranch\n"})
        self.commit_on_main({"c.txt": "c\n"})
        self.commit_on_main({"d.txt": "d\n"})
        head_before = self.git("rev-parse", "HEAD")
        mc = gitops.merge_check(self.repo, "main", self.BR)
        self.assertEqual(mc, {"clean": True, "conflicts": [], "markers": [],
                              "target_commit": head_before, "behind": 2})
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_committed_conflict_markers_block_merge(self):
        self.branch_change({"a.txt": "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> main\n"})
        mc = gitops.merge_check(self.repo, "main", self.BR)
        self.assertFalse(mc["clean"])
        self.assertEqual(mc["conflicts"], [])
        self.assertEqual(mc["markers"], ["a.txt"])
        with self.assertRaises(gitops.MergeConflict) as cm:
            gitops.merge(self.repo, "main", self.BR, "merge it")
        self.assertEqual(cm.exception.files, ["a.txt"])

    def test_merge_check_conflict(self):
        self.branch_change({"a.txt": "branch\n", "b.txt": "branch bee\n", "ok.txt": "ok\n"})
        self.commit_on_main({"a.txt": "main\n", "b.txt": "main bee\n"})
        mc = gitops.merge_check(self.repo, "main", self.BR)
        self.assertFalse(mc["clean"])
        self.assertEqual(mc["conflicts"], ["a.txt", "b.txt"])
        self.assertEqual(mc["behind"], 1)
        # Nothing touched in either working tree.
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.repo / "a.txt").read_text(), "main\n")

    def test_merge_into_checked_out_target(self):
        self.branch_change({"feature.txt": "f\n"})
        self.commit_on_main({"c.txt": "c\n"})
        write(self.repo / "scratch.txt", "untracked is fine\n")
        sha = gitops.merge(self.repo, "main", self.BR, "Merge run 1")
        self.assertEqual(sha, self.git("rev-parse", "main"))
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Merge run 1")
        self.assertEqual(len(self.git("log", "-1", "--format=%p").split()), 2)
        # Working tree updated, untracked file untouched.
        self.assertEqual((self.repo / "feature.txt").read_text(), "f\n")
        self.assertTrue((self.repo / "scratch.txt").exists())
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        # Merging again is a no-op.
        self.assertEqual(gitops.merge(self.repo, "main", self.BR, "again"), sha)

    def test_merge_refuses_dirty_checkout(self):
        self.branch_change({"feature.txt": "f\n"})
        write(self.repo / "a.txt", "user edit in progress\n")
        before = self.git("rev-parse", "main")
        with self.assertRaisesRegex(GitError, "uncommitted changes"):
            gitops.merge(self.repo, "main", self.BR, "m")
        self.assertEqual(self.git("rev-parse", "main"), before)
        self.assertEqual((self.repo / "a.txt").read_text(), "user edit in progress\n")
        # Staged changes are refused too.
        self.git("add", "a.txt")
        with self.assertRaises(GitError):
            gitops.merge(self.repo, "main", self.BR, "m")

    def test_merge_into_unchecked_out_target(self):
        self.git("branch", "release")
        self.branch_change({"feature.txt": "f\n"})
        # User keeps hacking on main with a dirty tree; must not matter.
        write(self.repo / "a.txt", "dirty\n")
        old = self.git("rev-parse", "release")
        sha = gitops.merge(self.repo, "release", self.BR, "Merge into release")
        self.assertEqual(self.git("rev-parse", "release"), sha)
        parents = self.git("log", "-1", "--format=%P", sha).split()
        self.assertEqual(parents, [old, self.git("rev-parse", self.BR)])
        self.assertEqual(self.git("show", f"{sha}:feature.txt"), "f")
        self.assertEqual(self.git("log", "-1", "--format=%s", sha), "Merge into release")
        self.assertEqual((self.repo / "a.txt").read_text(), "dirty\n")
        self.assertEqual(gitops.current_branch(self.repo), "main")
        self.assertNotIn("feature.txt", os.listdir(self.repo))

    def test_merge_target_checked_out_elsewhere(self):
        self.git("branch", "release")
        other = self.tmp / "other"
        self.git("worktree", "add", "-q", str(other), "release")
        self.branch_change({"feature.txt": "f\n"})
        with self.assertRaisesRegex(GitError, "checked out at"):
            gitops.merge(self.repo, "release", self.BR, "m")

    def test_merge_conflict_raises(self):
        self.branch_change({"a.txt": "branch\n", "ok.txt": "ok\n"})
        self.commit_on_main({"a.txt": "main\n"})
        before = self.git("rev-parse", "main")
        with self.assertRaises(MergeConflict) as cm:
            gitops.merge(self.repo, "main", self.BR, "m")
        self.assertEqual(cm.exception.files, ["a.txt"])
        self.assertIsInstance(cm.exception, GitError)
        self.assertEqual(self.git("rev-parse", "main"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        # Same for the plumbing path.
        self.git("branch", "release")
        with self.assertRaises(MergeConflict):
            gitops.merge(self.repo, "release", self.BR, "m")

    def test_merge_target_into_worktree_conflict_leaves_markers(self):
        path = self.branch_change({"a.txt": "branch\n"})
        self.commit_on_main({"a.txt": "main\n", "c.txt": "c\n"})
        res = gitops.merge_target_into_worktree(path, "main")
        self.assertEqual(res, {"clean": False, "conflicts": ["a.txt"]})
        content = (path / "a.txt").read_text()
        self.assertIn("<<<<<<<", content)
        self.assertIn(">>>>>>>", content)
        # Agent resolves; commit_all concludes the merge.
        write(path / "a.txt", "resolved\n")
        sha = gitops.commit_all(path, "resolve conflicts")
        self.assertEqual(len(self.git("log", "-1", "--format=%P", sha, cwd=path).split()), 2)
        self.assertTrue(gitops.merge_check(self.repo, "main", self.BR)["clean"])
        self.assertEqual(gitops.merge_check(self.repo, "main", self.BR)["behind"], 0)

    def test_merge_target_into_worktree_clean(self):
        path = self.branch_change({"feature.txt": "f\n"})
        self.commit_on_main({"c.txt": "c\n"})
        res = gitops.merge_target_into_worktree(path, "main")
        self.assertEqual(res, {"clean": True, "conflicts": []})
        self.assertTrue((path / "c.txt").exists())
        self.assertEqual(len(self.git("log", "-1", "--format=%P", cwd=path).split()), 2)
        self.assertEqual(self.git("status", "--porcelain", cwd=path), "")
        # Already up to date: still clean, no new commit.
        head = self.git("rev-parse", "HEAD", cwd=path)
        self.assertTrue(gitops.merge_target_into_worktree(path, "main")["clean"])
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=path), head)


class MiscTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gitops-misc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_dir_size(self):
        write(self.tmp / "a", "12345")
        write(self.tmp / "sub" / "b", "123")
        os.symlink("/", self.tmp / "loop")  # must not be followed
        self.assertEqual(gitops.dir_size(self.tmp), 8 + os.lstat(self.tmp / "loop").st_size)
        self.assertEqual(gitops.dir_size(self.tmp / "missing"), 0)

    def layout(self, name, files: dict) -> Path:
        d = self.tmp / name
        d.mkdir()
        for k, v in files.items():
            write(d / k, v)
        return d

    def test_detect_test_cmd(self):
        npm = self.layout("npm", {"package.json": '{"scripts": {"test": "jest"}}'})
        self.assertEqual(gitops.detect_test_cmd(npm), "npm test --silent")
        npm_default = self.layout("npm0", {"package.json":
            '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}'})
        self.assertIsNone(gitops.detect_test_cmd(npm_default))
        py = self.layout("py", {"tests/test_x.py": ""})
        self.assertEqual(gitops.detect_test_cmd(py), "python3 -m unittest discover -s tests")
        pkg = self.layout("pkg", {"tests/__init__.py": "", "tests/test_x.py": ""})
        self.assertEqual(gitops.detect_test_cmd(pkg),
                         "python3 -m unittest discover -s tests -t .")
        root = self.layout("root", {"test_thing.py": ""})
        self.assertEqual(gitops.detect_test_cmd(root), "python3 -m unittest discover")
        mk = self.layout("make", {"Makefile": "build:\n\techo\n\ntest: build\n\techo ok\n"})
        self.assertEqual(gitops.detect_test_cmd(mk), "make test")
        self.assertEqual(gitops.detect_test_cmd(self.layout("go", {"go.mod": "module x\n"})),
                         "go test ./...")
        self.assertEqual(gitops.detect_test_cmd(self.layout("rs", {"Cargo.toml": ""})),
                         "cargo test")
        self.assertIsNone(gitops.detect_test_cmd(self.layout("empty", {})))

    def test_detect_pytest(self):
        d = self.layout("pyt", {"pyproject.toml": "[tool.pytest.ini_options]\n",
                                "tests/test_x.py": ""})
        with mock.patch("shutil.which", return_value="/usr/bin/pytest"):
            self.assertEqual(gitops.detect_test_cmd(d), "pytest -q")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(gitops.detect_test_cmd(d), "python3 -m unittest discover -s tests")


if __name__ == "__main__":
    unittest.main()


class ChangedRangesTest(GitTestCase):
    """Line-range overlap is what keeps the "also editing" warning useful on
    codebases built around a few big modules (the more-itertools field test:
    file-level matching flagged 10 of 10 run pairs; ranges flagged only the
    one pair that really conflicted)."""

    def setUp(self):
        super().setUp()
        write(self.repo / "big.txt", "".join(f"line {i}\n" for i in range(1, 101)))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "big file")
        self.base = gitops.rev_parse(self.repo, "main")

    def edit(self, name, edits):
        path, _ = self.make_wt(name, f"conductor/{name}")
        lines = (path / "big.txt").read_text().splitlines(keepends=True)
        for idx, text in edits:
            lines[idx - 1] = text
        (path / "big.txt").write_text("".join(lines))
        write(path / f"new-{name}.txt", "x\n")
        return gitops.changed_ranges(path, self.base)

    def test_ranges_and_overlap(self):
        a = self.edit("run-a", [(10, "A\n"), (11, "A\n")])
        b = self.edit("run-b", [(80, "B\n")])
        c = self.edit("run-c", [(12, "C\n")])  # adjacent to run-a's edit
        self.assertEqual(a["big.txt"], [[10, 11]])
        self.assertEqual(b["big.txt"], [[80, 80]])
        self.assertEqual(a["new-run-a.txt"], [gitops.WHOLE_FILE])  # untracked = whole file
        self.assertFalse(gitops.ranges_overlap(a["big.txt"], b["big.txt"]))
        self.assertTrue(gitops.ranges_overlap(a["big.txt"], c["big.txt"]))

    def test_pure_insertions_at_the_same_point_overlap(self):
        a_path, _ = self.make_wt("run-a", "conductor/run-a")
        b_path, _ = self.make_wt("run-b", "conductor/run-b")
        for p, text in ((a_path, "from a\n"), (b_path, "from b\n")):
            lines = (p / "big.txt").read_text().splitlines(keepends=True)
            lines.insert(12, text)
            (p / "big.txt").write_text("".join(lines))
        ra = gitops.changed_ranges(a_path, self.base)["big.txt"]
        rb = gitops.changed_ranges(b_path, self.base)["big.txt"]
        self.assertTrue(gitops.ranges_overlap(ra, rb))
        # ...and git agrees: the second merge conflicts.
        gitops.commit_all(a_path, "a")
        gitops.commit_all(b_path, "b")
        gitops.merge(self.repo, "main", "conductor/run-a", "merge a")
        self.assertFalse(gitops.merge_check(self.repo, "main", "conductor/run-b")["clean"])
