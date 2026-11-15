"""Thin, explicit wrappers around the ``git`` CLI.

Every run gets its own branch (``conductor/run-<id>``) checked out in its own
worktree under ``$CONDUCTOR_HOME/worktrees``. The agent only ever touches that
worktree. This module creates/removes those worktrees, inspects what changed,
and merges the result back into the run's target branch.

Why merges avoid the user's working tree
-----------------------------------------
The repository a run targets is usually one the human is actively working in.
A normal ``git merge`` needs the target branch checked out and rewrites files
in that checkout, which can clobber edits in progress, trip editors/file
watchers, or leave the user's tree mid-merge if something goes wrong. So:

* ``merge_check`` uses ``git merge-tree --write-tree``, which computes the
  merge entirely in the object database: no index, no working tree, no locks
  on the user's checkout. It is cheap enough to run on every refresh.
* ``merge`` into a branch that is *not* checked out builds the merge commit
  with plumbing (``merge-tree`` -> ``commit-tree``) and advances the branch
  with ``update-ref`` using compare-and-swap on the old value, so a concurrent
  push/commit to that branch makes the merge fail instead of being lost.
* Only when the target branch is the one checked out in the main working tree
  do we run a real ``git merge`` there (git would otherwise leave the index and
  files stale relative to the moved branch). Even then we refuse if tracked
  files have uncommitted changes, so we never mix our merge with the user's
  unfinished work.

All functions take path-like arguments, return plain data, and raise
``GitError`` on failure. Git is always invoked with an argv list (never a
shell), a timeout, and ``GIT_TERMINAL_PROMPT=0`` so nothing ever blocks
waiting for credentials on a terminal nobody is watching.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

__all__ = [
    "GitError", "MergeConflict", "run_git", "repo_root", "is_repo",
    "current_branch", "list_branches", "rev_parse", "create_worktree",
    "remove_worktree", "commit_all", "changed_files", "diff", "diff_files",
    "diff_stat", "merge_check", "merge", "merge_target_into_worktree",
    "list_worktrees", "prune", "dir_size", "detect_test_cmd",
]

# Identity used for commits only when the repository has none configured.
FALLBACK_NAME = "Conductor"
FALLBACK_EMAIL = "conductor@localhost"


class GitError(Exception):
    """A git command failed (or git could not be run at all)."""


class MergeConflict(GitError):
    """A merge would conflict. ``files`` lists the conflicting paths."""

    def __init__(self, message: str, files: list[str] | None = None):
        super().__init__(message)
        self.files: list[str] = list(files or [])


# --------------------------------------------------------------------------
# Running git
# --------------------------------------------------------------------------

def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Stable, English messages; we never parse localized output.
    env["LC_ALL"] = "C"
    # Never pop up an editor for merge/commit messages.
    env["GIT_EDITOR"] = "true"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    return env


def _run(cwd, args, *, timeout=120, input=None) -> subprocess.CompletedProcess:
    # core.quotepath=false: print non-ASCII paths verbatim instead of "\303\251".
    argv = ["git", "-c", "core.quotepath=false", *[str(a) for a in args]]
    try:
        return subprocess.run(
            argv, cwd=os.fspath(cwd), env=_env(), input=input,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=None if input is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(argv[3:])} timed out after {timeout}s") from e
    except OSError as e:  # cwd missing, git not installed, ...
        raise GitError(f"git {' '.join(argv[3:])} could not run in {cwd}: {e}") from e


def run_git(cwd, *args, check=True, timeout=120, input=None) -> str:
    """Run ``git <args>`` in ``cwd`` and return stdout minus trailing newlines.

    With ``check=True`` a non-zero exit raises ``GitError`` carrying stderr.
    """
    p = _run(cwd, args, timeout=timeout, input=input)
    if check and p.returncode != 0:
        msg = (p.stderr or p.stdout).strip() or f"exit code {p.returncode}"
        raise GitError(f"git {' '.join(map(str, args))}: {msg}")
    return p.stdout.rstrip("\n")


def _ok(cwd, *args, timeout=120) -> bool:
    """True if the git command exits 0 (for predicates like --is-ancestor)."""
    return _run(cwd, args, timeout=timeout).returncode == 0


def _real(path) -> str:
    return os.path.realpath(os.fspath(path))


def _split_z(out: str) -> list[str]:
    return [p for p in out.split("\0") if p]


# --------------------------------------------------------------------------
# Basic queries
# --------------------------------------------------------------------------

def repo_root(path) -> str:
    """Absolute top-level directory of the working copy containing ``path``."""
    if not os.path.isdir(os.fspath(path)):
        raise GitError(f"{path} is not a directory")
    try:
        root = run_git(path, "rev-parse", "--show-toplevel")
    except GitError as e:
        raise GitError(f"{path} is not inside a git working copy") from e
    if not root:  # e.g. inside a bare repo / .git dir
        raise GitError(f"{path} is not inside a git working copy")
    return os.path.abspath(root)


def is_repo(path) -> bool:
    try:
        repo_root(path)
        return True
    except GitError:
        return False


def current_branch(repo) -> str | None:
    """Checked-out branch name, or None when HEAD is detached."""
    p = _run(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if p.returncode == 0:
        return p.stdout.strip() or None
    if p.returncode == 1:  # not a symbolic ref: detached
        return None
    raise GitError(f"git symbolic-ref HEAD: {p.stderr.strip()}")


def list_branches(repo) -> list[str]:
    out = run_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [b for b in out.splitlines() if b]


def rev_parse(repo, ref) -> str:
    """Full sha of the commit ``ref`` names."""
    p = _run(repo, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                    f"{ref}^{{commit}}"])
    sha = p.stdout.strip()
    if p.returncode != 0 or not sha:
        raise GitError(f"unknown revision {ref!r} in {repo}"
                       + (f": {p.stderr.strip()}" if p.stderr.strip() else ""))
    return sha


def _branch_exists(repo, branch) -> bool:
    return _ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")


def _identity_args(cwd) -> list[str]:
    """``-c`` overrides supplying a commit identity only where none is set."""
    args = []
    if not run_git(cwd, "config", "user.email", check=False).strip():
        args += ["-c", f"user.email={FALLBACK_EMAIL}"]
    if not run_git(cwd, "config", "user.name", check=False).strip():
        args += ["-c", f"user.name={FALLBACK_NAME}"]
    return args


# --------------------------------------------------------------------------
# Worktrees
# --------------------------------------------------------------------------

def list_worktrees(repo) -> list[dict]:
    """``[{"path", "branch", "head"}]``; branch is None when detached."""
    out = run_git(repo, "worktree", "list", "--porcelain", "-z")
    result: list[dict] = []
    cur: dict | None = None
    # -z: each attribute is NUL-terminated, records separated by an empty one.
    for field in out.split("\0"):
        if not field:
            cur = None
            continue
        key, _, value = field.partition(" ")
        if key == "worktree":
            cur = {"path": value, "branch": None, "head": None}
            result.append(cur)
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = value
        elif key == "branch":
            cur["branch"] = value.removeprefix("refs/heads/")
    return result


def prune(repo) -> None:
    """Forget worktrees whose directories are gone (best effort)."""
    try:
        run_git(repo, "worktree", "prune", timeout=60)
    except GitError:
        pass


def _find_worktree(repo, path) -> dict | None:
    want = _real(path)
    for wt in list_worktrees(repo):
        if _real(wt["path"]) == want:
            return wt
    return None


def create_worktree(repo, worktree_path, branch, base_ref) -> str:
    """Check out new branch ``branch`` (cut from ``base_ref``) at ``worktree_path``.

    Returns the base commit sha. Safe to call again after a crash:
      * already registered on ``branch``  -> nothing to do;
      * branch exists but no worktree     -> reuse the branch (keeps its work).
    In both reuse cases the returned base is ``merge-base base_ref branch``.
    """
    prune(repo)
    worktree_path = os.path.abspath(os.fspath(worktree_path))
    existing = _find_worktree(repo, worktree_path)
    if existing is not None:
        if existing["branch"] != branch:
            raise GitError(f"{worktree_path} is already a worktree for "
                           f"{existing['branch'] or 'a detached HEAD'}, not {branch}")
        return run_git(repo, "merge-base", base_ref, branch)

    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo, branch):
        run_git(repo, "worktree", "add", worktree_path, branch)
        return run_git(repo, "merge-base", base_ref, branch)
    base = rev_parse(repo, base_ref)
    # Use the resolved sha so a branch moving under us can't change the base.
    run_git(repo, "worktree", "add", "-b", branch, worktree_path, base)
    return base


def remove_worktree(repo, worktree_path, delete_branch: str | None = None) -> None:
    """Remove a worktree (and optionally its branch). Never raises.

    Tolerates: directory already deleted, worktree never registered, branch
    missing, or the repository itself being gone.
    """
    worktree_path = os.path.abspath(os.fspath(worktree_path))
    repo_ok = os.path.isdir(os.fspath(repo))
    if repo_ok:
        try:
            run_git(repo, "worktree", "remove", "--force", "--force",
                    worktree_path, check=False, timeout=120)
        except GitError:
            pass
    if os.path.lexists(worktree_path):
        shutil.rmtree(worktree_path, ignore_errors=True)
    if repo_ok:
        prune(repo)
        if delete_branch:
            try:
                run_git(repo, "branch", "-D", "--", delete_branch, check=False)
            except GitError:
                pass


# --------------------------------------------------------------------------
# Committing and inspecting changes
# --------------------------------------------------------------------------

def _merge_in_progress(worktree) -> bool:
    return _ok(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD")


JUNK_PATTERNS = ["__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 "node_modules", ".DS_Store", ".venv", "*.egg-info", ".coverage"]
JUNK_PATHSPECS = [f":(exclude,glob)**/{p}" for p in JUNK_PATTERNS] + \
                 [f":(exclude,glob)**/{p}/**" for p in JUNK_PATTERNS]


def commit_all(worktree, message) -> str | None:
    """Stage everything (incl. untracked) and commit. Returns the new HEAD sha,
    or None if there was nothing to commit. Hooks are skipped (--no-verify).

    If a merge is in progress the commit is made even when the resolved tree
    equals HEAD, since concluding the merge is itself the point.
    """
    # Never commit build/test junk an agent's tool runs leave behind, even in
    # repos with no .gitignore for it. Already-tracked files are unaffected.
    run_git(worktree, "add", "-A", "--", ".", *JUNK_PATHSPECS)
    staged = not _ok(worktree, "diff", "--cached", "--quiet")
    if not staged and not _merge_in_progress(worktree):
        return None
    run_git(worktree, *_identity_args(worktree), "commit", "--no-verify",
            "--no-edit", "-q", "-m", message)
    return rev_parse(worktree, "HEAD")


def changed_files(worktree, base_commit) -> list[str]:
    """Paths differing from ``base_commit``: committed, staged, unstaged and
    untracked (but not ignored). Renames show as their old and new paths."""
    tracked = run_git(worktree, "diff", "--name-only", "-z", "--no-renames",
                      "--no-ext-diff", base_commit, "--")
    untracked = run_git(worktree, "ls-files", "--others", "--exclude-standard", "-z",
                        "--", ".", *JUNK_PATHSPECS)
    return sorted(set(_split_z(tracked)) | set(_split_z(untracked)))


def _range_args(base_commit, ref) -> list[str]:
    return ["--no-color", "--no-ext-diff", "-M", base_commit, ref, "--"]


def diff(worktree, base_commit, ref="HEAD") -> str:
    """Unified diff of committed changes ``base_commit..ref``."""
    return run_git(worktree, "diff", *_range_args(base_commit, ref))


def diff_files(worktree, base_commit, ref="HEAD") -> list[dict]:
    """``[{"path", "status", "additions", "deletions"}]`` for base..ref.

    status is A/M/D/R (copies reported as A, type changes as M). Binary files
    report 0 additions/deletions.
    """
    # --name-status -z: "M\0path\0" or "R100\0old\0new\0"
    fields = run_git(worktree, "diff", "--name-status", "-z",
                     *_range_args(base_commit, ref)).split("\0")
    statuses: dict[str, str] = {}
    order: list[str] = []
    i = 0
    while i < len(fields):
        code = fields[i]
        if not code:
            i += 1
            continue
        letter = code[0]
        if letter in "RC":
            path = fields[i + 2]
            i += 3
        else:
            path = fields[i + 1]
            i += 2
        statuses[path] = {"C": "A", "T": "M"}.get(letter, letter)
        order.append(path)

    # --numstat -z: "add\tdel\tpath\0" or, for renames, "add\tdel\t\0old\0new\0".
    fields = run_git(worktree, "diff", "--numstat", "-z",
                     *_range_args(base_commit, ref)).split("\0")
    counts: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(fields):
        rec = fields[i]
        if not rec:
            i += 1
            continue
        add, dele, path = rec.split("\t", 2)
        if path:
            i += 1
        else:  # rename/copy: the next two fields are old and new path
            path = fields[i + 2]
            i += 3
        # Binary files show "-" for both counts.
        counts[path] = (int(add) if add.isdigit() else 0,
                        int(dele) if dele.isdigit() else 0)

    return [{"path": p, "status": statuses[p],
             "additions": counts.get(p, (0, 0))[0],
             "deletions": counts.get(p, (0, 0))[1]} for p in order]


def diff_stat(worktree, base_commit, ref="HEAD") -> dict:
    files = diff_files(worktree, base_commit, ref)
    return {"files": len(files),
            "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files)}


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def _merge_tree(repo, target, branch) -> tuple[str, list[str]]:
    """Merge ``branch`` into ``target`` in the object store only.

    Returns (tree_sha, conflicting_paths). The tree is only meaningful when
    there are no conflicts.
    """
    p = _run(repo, ["merge-tree", "--write-tree", "-z", "--name-only",
                    "--no-messages", target, branch])
    if p.returncode not in (0, 1):  # 1 = conflicts; anything else = error
        raise GitError(f"git merge-tree {target} {branch}: {p.stderr.strip()}")
    parts = _split_z(p.stdout)
    if not parts:
        raise GitError(f"git merge-tree {target} {branch}: no output")
    tree, conflicts = parts[0], sorted(set(parts[1:]))
    if p.returncode == 1 and not conflicts:
        raise GitError(f"git merge-tree {target} {branch}: conflict reported "
                       "but no files listed")
    return tree, conflicts


def merge_check(repo, target_branch, branch) -> dict:
    """Would merging ``branch`` into ``target_branch`` be clean? Touches no
    working tree or index."""
    target_commit = rev_parse(repo, target_branch)
    _, conflicts = _merge_tree(repo, target_branch, branch)
    behind = int(run_git(repo, "rev-list", "--count", f"{branch}..{target_branch}"))
    markers = conflict_markers(repo, target_branch, branch)
    return {"clean": not conflicts and not markers, "conflicts": conflicts,
            "markers": markers, "target_commit": target_commit, "behind": behind}


_MARKER_RE = r"^(<<<<<<<|>>>>>>>)( |$)"


def conflict_markers(repo, target_branch, branch) -> list[str]:
    """Files the branch changed that still contain merge conflict markers.

    A git merge happily "cleanly" merges a file full of ``<<<<<<<`` lines that
    an agent committed by mistake, so this is checked separately.
    """
    base = run_git(repo, "merge-base", target_branch, branch, check=False).strip()
    if not base:
        return []
    out = run_git(repo, "diff", "--name-only", "-z", "--diff-filter=AMR", base, branch)
    paths = [p for p in out.split("\0") if p]
    if not paths:
        return []
    found = run_git(repo, "grep", "-l", "-z", "-E", _MARKER_RE, branch, "--", *paths,
                    check=False)
    # git grep prints "<rev>:<path>"
    return sorted({f.split(":", 1)[1] if ":" in f else f for f in found.split("\0") if f})


def _checked_out_at(repo, branch) -> list[str]:
    return [wt["path"] for wt in list_worktrees(repo) if wt["branch"] == branch]


def merge(repo, target_branch, branch, message) -> str:
    """Merge ``branch`` into ``target_branch`` with a merge commit (--no-ff).

    Returns the sha ``target_branch`` points at afterwards. Raises
    MergeConflict if the merge would conflict (nothing is modified then).
    See the module docstring for why the main working tree is avoided.
    """
    target_ref = f"refs/heads/{target_branch}"
    if not _branch_exists(repo, target_branch):
        raise GitError(f"target branch {target_branch} does not exist")
    old = rev_parse(repo, target_ref)
    branch_sha = rev_parse(repo, branch)

    # Already contained in target: nothing to do, and no empty merge commit.
    if _ok(repo, "merge-base", "--is-ancestor", branch_sha, old):
        return old

    tree, conflicts = _merge_tree(repo, old, branch_sha)
    markers = conflict_markers(repo, target_branch, branch)
    if markers:
        raise MergeConflict(f"{branch} still contains conflict markers in: "
                            f"{', '.join(markers)}", markers)
    if conflicts:
        raise MergeConflict(f"merging {branch} into {target_branch} conflicts in "
                            f"{len(conflicts)} file(s): {', '.join(conflicts)}",
                            conflicts)

    root = _real(repo_root(repo))
    holders = _checked_out_at(repo, target_branch)
    if any(_real(p) == root for p in holders):
        return _merge_in_checkout(root, target_branch, branch_sha, branch, message)
    if holders:
        raise GitError(f"{target_branch} is checked out at {holders[0]}; "
                       "merge it there or switch that worktree to another branch")

    # Not checked out anywhere: build the commit with plumbing and advance the
    # ref with compare-and-swap so a concurrent update is never overwritten.
    new = run_git(repo, *_identity_args(repo), "commit-tree", tree,
                  "-p", old, "-p", branch_sha, "-m", message)
    run_git(repo, "update-ref", "-m", f"conductor: merge {branch}",
            target_ref, new, old)
    return new


def _merge_in_checkout(root, target_branch, branch_sha, branch, message) -> str:
    # Only tracked changes matter; untracked files are left alone by git unless
    # the merge would overwrite them, in which case git refuses and we abort.
    dirty = run_git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty or _merge_in_progress(root):
        raise GitError(f"{root} (where {target_branch} is checked out) has "
                       "uncommitted changes; commit or stash them first")
    p = _run(root, [*_identity_args(root), "merge", "--no-ff", "--no-edit",
                    "-m", message, branch_sha])
    if p.returncode != 0:
        if _merge_in_progress(root):
            _run(root, ["merge", "--abort"])
        raise GitError(f"git merge {branch} in {root} failed: "
                       f"{(p.stderr or p.stdout).strip()}")
    return rev_parse(root, "HEAD")


def merge_target_into_worktree(worktree, target_branch) -> dict:
    """Merge ``target_branch`` into the run's own worktree (to catch up / fix
    conflicts). Returns ``{"clean", "conflicts"}``.

    On conflict the markers are left in the files and the merge stays in
    progress so an agent can resolve them; ``commit_all`` concludes it. When
    clean the merge commit is made immediately.
    """
    p = _run(worktree, [*_identity_args(worktree), "merge", "--no-ff",
                        "--no-commit", "--no-edit", target_branch])
    unmerged = sorted(set(_split_z(run_git(
        worktree, "diff", "--name-only", "-z", "--diff-filter=U"))))
    if unmerged:
        return {"clean": False, "conflicts": unmerged}
    if p.returncode != 0:
        # Failed for some other reason (e.g. would overwrite local changes).
        if _merge_in_progress(worktree):
            _run(worktree, ["merge", "--abort"])
        raise GitError(f"git merge {target_branch} in {worktree} failed: "
                       f"{(p.stderr or p.stdout).strip()}")
    if _merge_in_progress(worktree):  # absent when "Already up to date."
        run_git(worktree, *_identity_args(worktree), "commit", "--no-edit",
                "--no-verify", "-q")
    return {"clean": True, "conflicts": []}


# --------------------------------------------------------------------------
# Filesystem helpers
# --------------------------------------------------------------------------

def dir_size(path) -> int:
    """Total bytes under ``path`` (symlinks not followed; vanishing files ok)."""
    total = 0
    stack = [os.fspath(path)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _unittest_cmd(d: Path, name: str) -> str:
    # -t . only works when the start dir is an importable package.
    if (d / name / "__init__.py").exists():
        return f"python3 -m unittest discover -s {name} -t ."
    return f"python3 -m unittest discover -s {name}"


def detect_test_cmd(path) -> str | None:
    """Best guess at the project's test command, or None."""
    import json

    d = Path(path)
    pkg = d / "package.json"
    if pkg.is_file():
        try:
            test = (json.loads(_read(pkg)).get("scripts") or {}).get("test")
        except (ValueError, AttributeError):
            test = None
        if isinstance(test, str) and test.strip() and "no test specified" not in test:
            return "npm test --silent"

    if any("pytest" in _read(d / f) for f in ("pyproject.toml", "pytest.ini", "setup.cfg")) \
            and shutil.which("pytest"):
        return "pytest -q"

    for name in ("tests", "test"):
        if (d / name).is_dir() and any((d / name).rglob("*.py")):
            return _unittest_cmd(d, name)
    if any(d.glob("test_*.py")):
        return "python3 -m unittest discover"

    if re.search(r"^test\s*:", _read(d / "Makefile"), re.MULTILINE):
        return "make test"
    if (d / "go.mod").is_file():
        return "go test ./..."
    if (d / "Cargo.toml").is_file():
        return "cargo test"
    return None
