"""Live policy enforcement: inspect every tool call an agent makes as it streams.

This is the third of the policy's enforcement points (see ``policy``):

  1. at create:  budget and limits validated (``policy.check_create``)
  2. at launch:  forbidden commands and paths become the agent's own deny
                 rules (``policy.claude_disallowed_tools``), so it cannot run them
  3. live:       this module - every streamed tool call is checked
  4. pre-merge:  the diff is checked (``policy.check_diff``)

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
import time
from dataclasses import dataclass, field

from . import policy as pol

PATH_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Agents whose CLI applies policy deny rules itself (verified for Claude Code:
# a disallowed Bash command is refused even with permission prompts skipped).
SELF_ENFORCING = {"claude"}


@dataclass
class Finding:
    violation: dict          # {rule, detail, action, at}
    kill: bool               # stop the run now


@dataclass
class LiveGuard:
    """Per-run state: tool calls awaiting their result."""
    pending: dict[str, dict] = field(default_factory=dict)

    def inspect(self, obj: dict, policy: pol.Policy, agent: str,
                worktree: str | None) -> list[Finding]:
        findings: list[Finding] = []
        typ = obj.get("type")
        content = ((obj.get("message") or {}).get("content")) or []
        if not isinstance(content, list):
            return findings
        if typ == "assistant":
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                v = self._check_call(item.get("name"), item.get("input") or {}, policy, worktree)
                if v is None:
                    continue
                if agent in SELF_ENFORCING:
                    self.pending[item.get("id") or ""] = v
                else:
                    findings.append(self._finding(v))
        elif typ == "user":
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                v = self.pending.pop(item.get("tool_use_id") or "", None)
                if v is None:
                    continue
                if item.get("is_error") and "denied" in _text(item.get("content")).lower():
                    findings.append(Finding({**v, "action": "flag", "at": time.time(),
                                             "detail": v["detail"] + " (blocked by the "
                                             "agent's permissions)"}, kill=False))
                else:
                    findings.append(self._finding(v))
        return findings

    @staticmethod
    def _finding(v: dict) -> Finding:
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
            return pol.check_path(policy, path) if path else None
        return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c)
                        for c in content)
    return json.dumps(content) if content is not None else ""
