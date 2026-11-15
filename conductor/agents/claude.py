"""Claude Code in headless mode (``claude -p --output-format stream-json``)."""

from __future__ import annotations

import os
import shutil

from .base import UNATTENDED_PROMPT, Adapter, LaunchSpec


class ClaudeAdapter(Adapter):
    name = "claude"

    def binary(self) -> str | None:
        env_bin = os.environ.get("CONDUCTOR_CLAUDE_BIN")
        if env_bin:
            return env_bin
        return shutil.which("claude")

    def build(self, *, prompt: str, session_id: str, resume: bool,
              model: str | None, budget_usd: float | None,
              worktree: str) -> LaunchSpec:
        argv = [self.binary() or "claude", "-p", prompt,
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions",
                "--append-system-prompt", UNATTENDED_PROMPT]
        if resume:
            argv += ["--resume", session_id]
        else:
            argv += ["--session-id", session_id]
        if model:
            argv += ["--model", model]
        if budget_usd:
            argv += ["--max-budget-usd", f"{float(budget_usd):g}"]
        # Lets --dangerously-skip-permissions work when running as root.
        return LaunchSpec(argv=argv, env={"IS_SANDBOX": "1"})

    def available(self) -> tuple[bool, str]:
        b = self.binary()
        if not b:
            return False, "claude CLI not found on PATH (set CONDUCTOR_CLAUDE_BIN)"
        if os.path.sep in b and not (os.path.isfile(b) and os.access(b, os.X_OK)):
            return False, f"claude CLI not executable: {b}"
        if os.path.sep not in b and not shutil.which(b):
            return False, f"claude CLI not found: {b}"
        return True, b
