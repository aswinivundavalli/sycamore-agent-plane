"""A scripted stand-in agent (``fake_agent.py``) for tests and free demos."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .base import Adapter, LaunchSpec

# Directory that contains the ``conductor`` package.
REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


class FakeAdapter(Adapter):
    name = "fake"

    def build(self, *, prompt: str, session_id: str, resume: bool,
              model: str | None, budget_usd: float | None,
              worktree: str, disallowed_tools: list[str] | None = None) -> LaunchSpec:
        argv = [sys.executable, "-m", "conductor.agents.fake_agent",
                "--session-id", session_id, "--prompt", prompt]
        if resume:
            argv.append("--resume")
        pp = os.environ.get("PYTHONPATH")
        return LaunchSpec(argv=argv, env={
            "PYTHONPATH": REPO_ROOT + (os.pathsep + pp if pp else "")})

    def available(self) -> tuple[bool, str]:
        return True, "built in"
