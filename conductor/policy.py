"""Per-repository guardrails for agent runs.

Layering
--------
A run's effective :class:`Policy` is built from up to four layers, applied in
order, where every layer can only TIGHTEN what came before it:

1. built-in defaults (:data:`DEFAULTS`): a few always-forbidden commands
   (``git push``, ``git reset --hard``, ``rm -rf /``, ``sudo``) and "kill" as
   the reaction to a forbidden command or path;
2. the conductor-wide ``<home>/policy.toml`` (the operator's rules);
3. the repository's ``<repo>/.conductor/policy.toml`` (committed in the target
   repository and reviewed like code);
4. limits passed for one run (a dict with the same shape as a parsed layer).

"Tighten only" means: forbidden lists and protected merge targets are unioned;
each layer's ``paths.allowed`` list is kept as a separate tuple and a path must
match at least one glob of EVERY such tuple (so a later layer can narrow the
allowed area but never widen it); numeric caps take the minimum of those set;
forbid/require booleans are OR-ed; for the ``on_*`` reactions "kill" beats
"flag" (the built-in "kill" applies when no layer sets one). A repository therefore cannot raise a conductor-wide cap, drop a
forbidden command, or soften "kill" to "flag".

Each layer is TOML (all sections and keys optional)::

    [paths]      allowed = ["src/**", "tests/**"]   forbidden = [".github/**"]
    [diff]       max_files = 20  max_lines = 600
                 forbid_test_deletion = true  require_test_changes = false
    [commands]   forbidden = ["git push", "curl"]
    [limits]     max_budget_usd = 5  max_runtime_secs = 3600
                 max_parallel_per_repo = 20
    [merge]      protected_targets = ["main", "release/*"]
                 require_approval_over_lines = 300
    [violation]  on_forbidden_command = "kill"   on_forbidden_path = "flag"

Unknown sections or keys, wrong types and bad enum values raise
:class:`PolicyError` naming the source file; nothing is silently ignored.

Enforcement points
------------------
* launch: :func:`claude_disallowed_tools` turns the policy into Claude Code
  permission rules (``Bash(git push:*)``, ``Edit(.github/**)``, ...) so the
  agent itself is refused before it acts;
* live: :func:`check_command` / :func:`check_path` are run on every streamed
  tool call; a hit with action "kill" stops the agent, "flag" just records it;
* pre-merge: :func:`check_diff` inspects the final diff (paths, size, tests,
  approval on protected targets) and blocks the merge or asks for approval.

Violations are dicts ``{"rule", "detail", "action"}`` with action one of
"kill" | "flag" | "block" | "approval"; callers add ``"at"``.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import shlex
import tomllib
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path

__all__ = [
    "PolicyError", "Policy", "DEFAULTS", "parse", "validate", "combine", "load",
    "glob_match", "branch_match", "is_test_path", "check_command", "check_path",
    "check_diff", "check_create", "effective_budget", "claude_disallowed_tools",
    "describe",
]


class PolicyError(ValueError):
    """A policy layer is malformed. The message names the source and the key."""

    def __init__(self, source: str, message: str):
        self.source = source
        super().__init__(f"{source}: {message}")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

ACTIONS = ("flag", "kill")  # ordered weakest -> strictest

_STRLIST, _INT, _NUM, _BOOL, _ACTION = "strlist", "int", "num", "bool", "action"
_WRITES = "writes"
# Where an agent may write on the host, weakest -> strictest. "worktree" runs
# the agent (and the repository's test command) in an OS sandbox in which
# everything outside its worktree is read-only and /tmp is private.
WRITES = ("unrestricted", "worktree")

SCHEMA: dict[str, dict[str, str]] = {
    "paths": {"allowed": _STRLIST, "forbidden": _STRLIST},
    "diff": {"max_files": _INT, "max_lines": _INT,
             "forbid_test_deletion": _BOOL, "require_test_changes": _BOOL},
    "commands": {"forbidden": _STRLIST},
    "limits": {"max_budget_usd": _NUM, "max_runtime_secs": _INT,
               "max_parallel_per_repo": _INT},
    "merge": {"protected_targets": _STRLIST, "require_approval_over_lines": _INT},
    "violation": {"on_forbidden_command": _ACTION, "on_forbidden_path": _ACTION},
    "sandbox": {"writes": _WRITES},
}

DEFAULT_FORBIDDEN_COMMANDS = ("git push", "git reset --hard", "rm -rf /", "sudo")
# An agent must never edit the policy that governs it (or the workflows it
# runs): the policy is read from the main checkout, and this keeps a weaker
# one from arriving there through a merged run.
DEFAULT_FORBIDDEN_PATHS = (".conductor/**",)

#: The built-in layer, in the same shape as a parsed TOML layer.
DEFAULTS: dict = {
    "paths": {"forbidden": list(DEFAULT_FORBIDDEN_PATHS)},
    "commands": {"forbidden": list(DEFAULT_FORBIDDEN_COMMANDS)},
    "diff": {"forbid_test_deletion": False},
    "violation": {"on_forbidden_command": "kill", "on_forbidden_path": "kill"},
}


def _check_value(source: str, section: str, key: str, kind: str, value):
    where = f"[{section}] {key}"
    if kind == _STRLIST:
        if not isinstance(value, list):
            raise PolicyError(source, f"{where} must be a list of strings, got {value!r}")
        out = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise PolicyError(source, f"{where} entries must be non-empty strings, got {item!r}")
            out.append(item.strip())
        return out
    if kind == _BOOL:
        if not isinstance(value, bool):
            raise PolicyError(source, f"{where} must be true or false, got {value!r}")
        return value
    if kind == _INT:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PolicyError(source, f"{where} must be an integer >= 0, got {value!r}")
        return value
    if kind == _NUM:
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise PolicyError(source, f"{where} must be a number >= 0, got {value!r}")
        return value
    if kind == _WRITES:
        if value not in WRITES:
            raise PolicyError(source, f"{where} must be one of {', '.join(map(repr, WRITES))}, got {value!r}")
        return value
    if kind == _ACTION:
        if value not in ACTIONS:
            raise PolicyError(source, f"{where} must be one of {', '.join(map(repr, ACTIONS))}, got {value!r}")
        return value
    raise AssertionError(kind)  # pragma: no cover


def validate(layer: dict, source: str) -> dict:
    """Validate an already-parsed layer (TOML or a run-limits dict).

    Returns a fresh plain dict ``{section: {key: value}}``.
    """
    if not isinstance(layer, dict):
        raise PolicyError(source, f"policy must be a table/dict, got {type(layer).__name__}")
    out: dict = {}
    for section, body in layer.items():
        if section not in SCHEMA:
            raise PolicyError(source, f"unknown section [{section}] (expected one of: {', '.join(SCHEMA)})")
        if not isinstance(body, dict):
            raise PolicyError(source, f"[{section}] must be a table, got {body!r}")
        keys = SCHEMA[section]
        clean = {}
        for key, value in body.items():
            if key not in keys:
                raise PolicyError(source, f"unknown key [{section}] {key} (expected one of: {', '.join(keys)})")
            clean[key] = _check_value(source, section, key, keys[key], value)
        out[section] = clean
    return out


def parse(text: str, source: str) -> dict:
    """Parse and validate one TOML policy layer; return it as a plain dict."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(source, f"invalid TOML: {e}") from None
    return validate(data, source)


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    paths_allowed: tuple[tuple[str, ...], ...] = ()
    paths_forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN_PATHS
    max_files: int | None = None
    max_lines: int | None = None
    forbid_test_deletion: bool = False
    require_test_changes: bool = False
    commands_forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN_COMMANDS
    max_budget_usd: float | None = None
    max_runtime_secs: int | None = None
    max_parallel_per_repo: int | None = None
    protected_targets: tuple[str, ...] = ()
    require_approval_over_lines: int | None = None
    on_forbidden_command: str = "kill"
    on_forbidden_path: str = "kill"
    sandbox_writes: str = "unrestricted"

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "paths_allowed":
                v = [list(t) for t in v]
            elif isinstance(v, tuple):
                v = list(v)
            out[f.name] = v
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        """Inverse of :meth:`to_dict`. Missing keys take the built-in default;
        unknown keys (e.g. from a newer version) are ignored."""
        kw = {}
        for f in fields(cls):
            if f.name not in d:
                continue
            v = d[f.name]
            if f.name == "paths_allowed":
                v = tuple(tuple(t) for t in (v or ()))
            elif f.name in ("paths_forbidden", "commands_forbidden", "protected_targets"):
                v = tuple(v or ())
            elif f.name == "max_budget_usd" and v is not None:
                v = float(v)
            kw[f.name] = v
        return cls(**kw)


def _union(a: tuple[str, ...], b) -> tuple[str, ...]:
    out = list(a)
    for x in b:
        if x not in out:
            out.append(x)
    return tuple(out)


def _min(a, b):
    if b is None:
        return a
    return b if a is None else min(a, b)


def combine(layers: list[tuple[str, dict]]) -> Policy:
    """Tighten-only merge of ``[(source, layer_dict), ...]`` in order.

    The built-in defaults are always applied first. For the ``on_*``
    reactions the built-in "kill" is a fallback: the result is the strictest
    value any layer sets ("kill" beats "flag"), or "kill" when no layer sets
    one, so a layer can only choose "flag" if no other layer said "kill"
    (passing ``("built-in", DEFAULTS)`` explicitly pins "kill"). Each layer
    is validated; errors name its source.
    """
    acc = {
        "paths_allowed": (), "paths_forbidden": (), "max_files": None,
        "max_lines": None, "forbid_test_deletion": False,
        "require_test_changes": False, "commands_forbidden": (),
        "max_budget_usd": None, "max_runtime_secs": None,
        "max_parallel_per_repo": None, "protected_targets": (),
        "require_approval_over_lines": None,
        "on_forbidden_command": None, "on_forbidden_path": None,
        "sandbox_writes": "unrestricted",
    }
    builtin = {k: v for k, v in DEFAULTS.items() if k != "violation"}
    for source, raw in [("built-in", builtin), *layers]:
        layer = validate(raw, source)
        paths = layer.get("paths", {})
        if "allowed" in paths:
            acc["paths_allowed"] += (tuple(dict.fromkeys(paths["allowed"])),)
        acc["paths_forbidden"] = _union(acc["paths_forbidden"], paths.get("forbidden", ()))
        diff = layer.get("diff", {})
        acc["max_files"] = _min(acc["max_files"], diff.get("max_files"))
        acc["max_lines"] = _min(acc["max_lines"], diff.get("max_lines"))
        acc["forbid_test_deletion"] |= diff.get("forbid_test_deletion", False)
        acc["require_test_changes"] |= diff.get("require_test_changes", False)
        acc["commands_forbidden"] = _union(acc["commands_forbidden"],
                                           layer.get("commands", {}).get("forbidden", ()))
        limits = layer.get("limits", {})
        if limits.get("max_budget_usd") is not None:
            acc["max_budget_usd"] = _min(acc["max_budget_usd"], float(limits["max_budget_usd"]))
        acc["max_runtime_secs"] = _min(acc["max_runtime_secs"], limits.get("max_runtime_secs"))
        acc["max_parallel_per_repo"] = _min(acc["max_parallel_per_repo"],
                                            limits.get("max_parallel_per_repo"))
        merge = layer.get("merge", {})
        acc["protected_targets"] = _union(acc["protected_targets"], merge.get("protected_targets", ()))
        acc["require_approval_over_lines"] = _min(acc["require_approval_over_lines"],
                                                  merge.get("require_approval_over_lines"))
        writes = layer.get("sandbox", {}).get("writes")
        if writes and WRITES.index(writes) > WRITES.index(acc["sandbox_writes"]):
            acc["sandbox_writes"] = writes  # once confined, no later layer can relax it
        viol = layer.get("violation", {})
        for key in ("on_forbidden_command", "on_forbidden_path"):
            if key in viol and (acc[key] is None
                                or ACTIONS.index(viol[key]) > ACTIONS.index(acc[key])):
                acc[key] = viol[key]
    for key in ("on_forbidden_command", "on_forbidden_path"):
        if acc[key] is None:
            acc[key] = DEFAULTS["violation"][key]
    return Policy(**acc)


def policy_sha(policy: Policy) -> str:
    blob = json.dumps(policy.to_dict(), sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def load(home: Path, repo: Path, run_limits: dict | None = None) -> tuple[Policy, list[str], str]:
    """Build the effective policy for ``repo``.

    Returns ``(policy, sources, sha)`` where ``sources`` lists what was
    actually read, in order: ``"built-in"``, then each existing policy file
    path, then ``"run"`` when ``run_limits`` is non-empty. Missing files are
    fine; malformed ones raise :class:`PolicyError`.
    """
    layers: list[tuple[str, dict]] = []
    sources = ["built-in"]
    for path in (Path(home) / "policy.toml", Path(repo) / ".conductor" / "policy.toml"):
        if not path.is_file():
            continue
        src = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise PolicyError(src, f"cannot read: {e}") from None
        layers.append((src, parse(text, src)))
        sources.append(src)
    if run_limits:
        layers.append(("run", validate(run_limits, "run")))
        sources.append("run")
    policy = combine(layers)
    return policy, sources, policy_sha(policy)


# --------------------------------------------------------------------------
# Globs and paths
# --------------------------------------------------------------------------

def _segment_regex(seg: str) -> str:
    out, i, n = [], 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            while i < n and seg[i] == "*":
                i += 1
            out.append("[^/]*")
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < n and seg[j] in "!^":
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
            else:
                body = seg[i + 1:j]
                neg = body[:1] in ("!", "^")
                if neg:
                    body = body[1:]
                body = body.replace("\\", "\\\\")
                out.append("[" + ("^/" if neg else "") + body + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _translate(pattern: str) -> str:
    segs = [s for s in pattern.split("/")]
    parts: list[str] = []
    for idx, seg in enumerate(segs):
        last = idx == len(segs) - 1
        if seg == "**":
            parts.append(".+" if last else "(?:[^/]+/)*")
        else:
            parts.append(_segment_regex(seg) + ("" if last else "/"))
    return "".join(parts)


@lru_cache(maxsize=1024)
def _compile(pattern: str, anchored: bool) -> re.Pattern:
    p = pattern.strip()
    if not anchored:
        if p.endswith("/") and not p.endswith("**/"):
            p += "**"          # "build/" -> everything under build/
        if p.startswith("/"):
            p = p.lstrip("/")  # "/x" anchors at the root
        elif "/" not in p:
            # gitignore-like: a bare name matches at any depth, as a file or
            # as a directory whose contents are then matched too.
            seg = _segment_regex(p)
            return re.compile(rf"(?:[^/]+/)*{seg}(?:/.*)?\Z", re.S)
    return re.compile(_translate(p) + r"\Z", re.S)


def _norm(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def glob_match(path: str, pattern: str) -> bool:
    """Match a repo-relative POSIX path against a gitignore-like glob.

    ``**`` spans any number of directories (including none), ``*`` and ``?``
    stay within one segment, ``dir/**`` matches everything under ``dir``, and
    a pattern with no ``/`` matches a file or directory name at any depth.
    """
    return _compile(pattern, False).match(_norm(path)) is not None


def branch_match(branch: str, pattern: str) -> bool:
    """Match a branch name against a glob anchored at the start
    (``release/*`` matches ``release/1.2``; ``main`` only ``main``)."""
    return _compile(pattern, True).match(branch) is not None


_TEST_FILE = re.compile(
    r"(?:test_[^/]*\.py|[^/]*_test\.py|[^/]*_test\.go|[^/]*\.test\.(?:js|ts|jsx|tsx)"
    r"|[^/]*\.spec\.[^/]+)\Z")


def is_test_path(path: str) -> bool:
    parts = _norm(path).split("/")
    if any(p in ("tests", "test", "__tests__") for p in parts[:-1]):
        return True
    return _TEST_FILE.match(parts[-1]) is not None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

_SEP_CHARS = set("();&|\n`")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_XARGS_ARG_OPTS = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "-i", "-l"}


def _split_simple(command: str) -> list[list[str]]:
    """Split a shell command line into simple commands (lists of words)."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n`")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return [c.split() for c in re.split(r"&&|\|\||[;|&\n`()]", command) if c.split()]
    cmds: list[list[str]] = []
    cur: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok and ("<" in tok or ">" in tok) and set(tok) <= set("<>&|"):
            skip_next = True  # redirect: drop the target word
            continue
        if tok and set(tok) <= _SEP_CHARS:
            if cur:
                cmds.append(cur)
            cur = []
            continue
        cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


def _strip_wrappers(words: list[str]) -> list[str]:
    w = list(words)
    while w:
        if _ASSIGN.match(w[0]):
            w = w[1:]
            continue
        head = posixpath.basename(w[0])
        if head == "env":
            w = w[1:]
            while w and (w[0].startswith("-") or _ASSIGN.match(w[0])):
                takes_arg = w[0] in ("-u", "--unset", "-C", "--chdir", "-S")
                w = w[2:] if takes_arg else w[1:]
            continue
        if head in ("command", "nohup", "time", "exec"):
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[2:] if (head == "exec" and w[0] == "-a") else w[1:]
            continue
        if head == "xargs":
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[2:] if w[0] in _XARGS_ARG_OPTS else w[1:]
            continue
        break
    return w


def _normalize_git(words: list[str]) -> list[str]:
    """Drop git's global options so ``git -C x push`` reads as ``git push``."""
    out, i = [words[0]], 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-C", "-c") else 1
    return out + words[i:]


def _expand(words: list[str], depth: int = 0) -> list[tuple[list[str], list[str]]]:
    """(display words, normalized words) for a simple command and any shell
    scripts it runs via ``bash -c`` / ``eval``."""
    norm = _strip_wrappers(words)
    if not norm:
        return []
    norm = [posixpath.basename(norm[0]) if "/" in norm[0] else norm[0]] + norm[1:]
    out = [(words, _normalize_git(norm) if norm[0] == "git" else norm)]
    if depth > 5:
        return out
    inner = None
    if norm[0] in _SHELLS:
        for i, a in enumerate(norm[1:], 1):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                if i + 1 < len(norm):
                    inner = norm[i + 1]
                break
            if not a.startswith("-"):
                break
    elif norm[0] == "eval" and len(norm) > 1:
        inner = " ".join(norm[1:])
    if inner:
        for sub in _split_simple(inner):
            out.extend(_expand(sub, depth + 1))
    return out


def check_command(policy: Policy, command: str) -> dict | None:
    """First forbidden command found in a shell command line, or None."""
    entries = []
    for e in policy.commands_forbidden:
        try:
            ew = shlex.split(e)
        except ValueError:
            ew = e.split()
        if ew:
            entries.append((e, ew))
    for simple in _split_simple(command or ""):
        for shown, words in _expand(simple):
            for entry, ew in entries:
                if words[:len(ew)] == ew:
                    ran = " ".join(shown)
                    if len(ran) > 200:
                        ran = ran[:197] + "..."
                    return {"rule": "forbidden_command",
                            "detail": f"ran `{ran}` (forbidden: {entry})",
                            "action": policy.on_forbidden_command}
    return None


# --------------------------------------------------------------------------
# Paths and diffs
# --------------------------------------------------------------------------

def _path_finding(policy: Policy, path: str) -> tuple[str, str] | None:
    p = (path or "").strip()
    if not p:
        return None
    if p.startswith("/") or re.match(r"[A-Za-z]:[\\/]", p) or p.startswith("~"):
        return "outside_worktree", f"edits {p} (outside the worktree)"
    norm = posixpath.normpath(_norm(p))
    if norm == ".." or norm.startswith("../"):
        return "outside_worktree", f"edits {p} (outside the worktree)"
    if norm == ".":
        return None
    for g in policy.paths_forbidden:
        if glob_match(norm, g):
            return "forbidden_path", f"edits {norm} (forbidden: {g})"
    for layer in policy.paths_allowed:
        if not any(glob_match(norm, g) for g in layer):
            return "outside_allowed_paths", f"edits {norm} (allowed: {', '.join(layer)})"
    return None


def check_path(policy: Policy, path: str) -> dict | None:
    """Violation for an agent touching ``path`` (relative to the worktree)."""
    f = _path_finding(policy, path)
    if f is None:
        return None
    return {"rule": f[0], "detail": f[1], "action": policy.on_forbidden_path}


def _lines(f: dict) -> int:
    total = 0
    for k in ("additions", "deletions"):
        try:
            total += int(f.get(k) or 0)
        except (TypeError, ValueError):
            pass
    return total


def check_diff(policy: Policy, files: list[dict], target_branch: str) -> list[dict]:
    """Pre-merge findings for a diff (``gitops.diff_files`` output)."""
    out: list[dict] = []

    def v(rule, detail, action="block"):
        out.append({"rule": rule, "detail": detail, "action": action})

    for f in files:
        found = _path_finding(policy, f.get("path", ""))
        if found:
            v(*found)
            out[-1]["path"] = f.get("path", "")
    n_files = len(files)
    total = sum(_lines(f) for f in files)
    if policy.max_files is not None and n_files > policy.max_files:
        v("max_files", f"changes {n_files} files (cap {policy.max_files})")
    if policy.max_lines is not None and total > policy.max_lines:
        v("max_lines", f"changes {total} lines (cap {policy.max_lines})")
    if policy.forbid_test_deletion:
        for f in files:
            if f.get("status") == "D" and is_test_path(f.get("path", "")):
                v("test_deleted", f"deletes test {f['path']}")
    if policy.require_test_changes and files and not any(
            f.get("status") != "D" and is_test_path(f.get("path", "")) for f in files):
        v("tests_required", "no test files changed (this repository requires test changes)")
    target = target_branch or ""
    if target and any(branch_match(target, g) for g in policy.protected_targets):
        cap = policy.require_approval_over_lines
        if cap is None or cap == 0 or total > cap:
            why = f"over {cap} lines" if cap else "for every change"
            v("approval_required",
              f"merges {total} lines into protected {target} (approval required {why})",
              "approval")
    return out


# --------------------------------------------------------------------------
# Creation-time helpers, launch flags, UI
# --------------------------------------------------------------------------

def _money(x: float) -> str:
    return f"${int(x)}" if float(x).is_integer() else f"${x:.2f}"


def check_create(policy: Policy, budget_usd: float | None) -> list[str]:
    """Human-readable reasons a run cannot be created as requested."""
    errors = []
    cap = policy.max_budget_usd
    if cap is not None and budget_usd is not None and budget_usd > cap:
        errors.append(f"Budget {_money(budget_usd)} exceeds this repository's cap of {_money(cap)}")
    return errors


def effective_budget(policy: Policy, requested: float | None) -> float | None:
    cap = policy.max_budget_usd
    if requested is None:
        return cap
    return requested if cap is None else min(requested, cap)


def claude_disallowed_tools(policy: Policy) -> list[str]:
    """Claude Code permission rules (for ``--disallowedTools``)."""
    rules = [f"Bash({c}:*)" for c in policy.commands_forbidden]
    for g in policy.paths_forbidden:
        rules += [f"Edit({g})", f"Write({g})"]
    return list(dict.fromkeys(rules))


def describe(policy: Policy) -> list[dict]:
    """``[{"rule", "value"}]`` for the rules that are set, for display."""
    rows: list[tuple[str, str]] = []
    if policy.paths_allowed:
        rows.append(("Allowed paths", " AND ".join(
            ", ".join(t) if len(policy.paths_allowed) == 1 else f"({', '.join(t)})"
            for t in policy.paths_allowed)))
    if policy.paths_forbidden:
        rows.append(("Forbidden paths", ", ".join(policy.paths_forbidden)))
    if policy.max_files is not None:
        rows.append(("Max files changed", str(policy.max_files)))
    if policy.max_lines is not None:
        rows.append(("Max lines changed", str(policy.max_lines)))
    if policy.forbid_test_deletion:
        rows.append(("Test deletion", "forbidden"))
    if policy.require_test_changes:
        rows.append(("Test changes", "required"))
    if policy.commands_forbidden:
        rows.append(("Forbidden commands", ", ".join(policy.commands_forbidden)))
    if policy.max_budget_usd is not None:
        rows.append(("Max budget", _money(policy.max_budget_usd)))
    if policy.max_runtime_secs is not None:
        rows.append(("Max runtime", f"{policy.max_runtime_secs} s"))
    if policy.max_parallel_per_repo is not None:
        rows.append(("Max parallel runs per repository", str(policy.max_parallel_per_repo)))
    if policy.protected_targets:
        rows.append(("Protected merge targets", ", ".join(policy.protected_targets)))
        cap = policy.require_approval_over_lines
        rows.append(("Approval required", f"over {cap} lines" if cap else "for every merge"))
    if policy.sandbox_writes == "worktree":
        rows.append(("Agent writes", "worktree only (OS sandbox; /tmp private)"))
    rows.append(("On forbidden command", policy.on_forbidden_command))
    rows.append(("On forbidden path", policy.on_forbidden_path))
    return [{"rule": r, "value": v} for r, v in rows]
