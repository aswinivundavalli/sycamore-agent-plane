"""Live policy enforcement: inspect every tool call an agent makes as it streams.

This is the third of the policy's enforcement points (see ``policy``):

  1. at create:  budget and limits validated (``policy.check_create``)
  2. at launch:  forbidden commands and paths become the agent's own deny
                 rules (``policy.claude_disallowed_tools``), so it cannot run them
  3. live:       this module - every streamed tool call is checked, and after
                 any call that can write, the files actually changed in the
                 worktree are checked against the path rules (``check_files``)
  4. pre-merge:  the diff is checked (``policy.check_diff``)

The worktree check matters because the deny rules and the per-call check
only see the agent's *file tools*. A shell command (``cat > .github/ci.yml``,
``sed -i``, a script, a code generator) writes files the call itself does not
name. Real Claude Code agents do this routinely. Without the worktree check
such a write would only be caught at the merge gate, after the run finished.

A forbidden call that the agent's own permissions refused still shows up in
its stream (the tool_use, then an error result "...has been denied"). That
is a blocked *attempt*, recorded for the reviewer but not a reason to stop the
run. So for agents that enforce deny rules themselves, a call is judged when
its result arrives: refused -> recorded as "flag"; executed -> the policy's
action ("kill" stops the run). Agents without their own enforcement are
judged at the call.
"""

from __future__ import annotations

import json
import posixpath
import time
from dataclasses import dataclass, field

from . import policy as pol
from .sandbox import REFUSED

PATH_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Tools that cannot change files. After any other tool's result, the
# conductor looks at the worktree itself.
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead", "WebFetch", "WebSearch",
                   "TodoWrite"}
# Agents whose CLI applies policy deny rules itself (verified for Claude Code:
# a disallowed Bash command is refused even with permission prompts skipped).
SELF_ENFORCING = {"claude"}


@dataclass
class Finding:
    violation: dict          # {rule, detail, action, at}
    kill: bool               # stop the run now


@dataclass
class LiveGuard:
    """Per-run (per-attempt) state of the live check."""
    # Policy-relevant calls awaiting their result (self-enforcing agents).
    pending: dict[str, dict] = field(default_factory=dict)
    # Every tool call awaiting its result: id -> (tool name, shell command).
    calls: dict[str, tuple[str, str]] = field(default_factory=dict)
    # A tool that can write has returned since the worktree was last checked;
    # scan_after names it, for the finding ("found after `cmd`").
    scan_due: bool = False
    scan_after: str = ""
    # Paths already reported, so a file is reported once, not on every scan.
    reported: set[str] = field(default_factory=set)
    base: str | None = None  # commit the worktree is compared with (cached)

    def inspect(self, obj: dict, policy: pol.Policy, agent: str,
                worktree: str | None, sandboxed: bool = False) -> list[Finding]:
        findings: list[Finding] = []
        typ = obj.get("type")
        content = ((obj.get("message") or {}).get("content")) or []
        if not isinstance(content, list):
            return findings
        if typ == "assistant":
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                name = str(item.get("name") or "")
                cmd = (str((item.get("input") or {}).get("command") or "")
                       if name == "Bash" else "")
                self.calls[item.get("id") or ""] = (name, cmd)
                v = self._check_call(name, item.get("input") or {}, policy, worktree)
                if v is None:
                    continue
                if agent in SELF_ENFORCING:
                    self.pending[item.get("id") or ""] = v
                else:
                    findings.append(self._final(v))
        elif typ == "user":
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                tid = item.get("tool_use_id") or ""
                text = _text(item.get("content")).lower()
                refused = any(m in text for m in REFUSED)
                name, cmd = self.calls.pop(tid, ("", ""))
                if name not in READ_ONLY_TOOLS:
                    self.scan_due = True
                    self.scan_after = f"`{_first_line(cmd, 80)}`" if cmd else name
                v = self.pending.pop(tid, None)
                if v is None:
                    if sandboxed and name == "Bash" and refused:
                        findings.append(Finding({
                            "rule": "write_outside_worktree", "action": "flag", "at": time.time(),
                            "detail": f"the sandbox refused a write outside the worktree: "
                                      f"`{_first_line(cmd)}`"}, kill=False))
                    continue
                if item.get("is_error") and ("denied" in text or (sandboxed and refused)):
                    by = "agent's permissions" if "denied" in text else "sandbox"
                    findings.append(Finding({**v, "action": "flag", "at": time.time(),
                                             "detail": v["detail"] + f" (blocked by the {by})"},
                                            kill=False))
                else:
                    findings.append(self._final(v))
        return findings

    def check_files(self, files: list[str], policy: pol.Policy,
                    reported: set[str] | frozenset = frozenset(),
                    after: str = "") -> list[Finding]:
        """The path rules applied to the files actually changed in the
        worktree (paths relative to it), whatever wrote them. ``reported``
        holds paths already on the run from earlier attempts or before a
        restart; ``after`` names the tool call the scan followed."""
        out: list[Finding] = []
        for path in files:
            if path in self.reported or path in reported:
                continue
            v = pol.check_path(policy, path)
            if v is None:
                continue
            how = f"found in the worktree after {after}" if after else "found in the worktree"
            out.append(self._final({**v, "path": path, "how": how}))
        return out

    def _final(self, v: dict) -> Finding:
        """A violation that happened (not one the agent's permissions refused)."""
        if v.get("path"):
            self.reported.add(v["path"])
        return Finding({**v, "at": time.time()}, kill=v.get("action") == "kill")

    @staticmethod
    def _check_call(name: str | None, inp: dict, policy: pol.Policy,
                    worktree: str | None) -> dict | None:
        if name == "Bash":
            return pol.check_command(policy, str(inp.get("command") or ""))
        if name in PATH_TOOLS:
            path = str(inp.get("file_path") or inp.get("notebook_path") or "")
            if worktree and path.startswith(worktree.rstrip("/") + "/"):
                path = path[len(worktree.rstrip("/")) + 1:]
            v = pol.check_path(policy, path) if path else None
            return {**v, "path": posixpath.normpath(path)} if v else None
        return None


def _first_line(s: str, limit: int = 120) -> str:
    line = (s.strip().splitlines() or [""])[0]
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c)
                        for c in content)
    return json.dumps(content) if content is not None else ""
