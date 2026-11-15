"""Agent adapters: how to launch a headless coding agent for one attempt.

An adapter only *builds* the command line; the supervisor/runner owns the
process. Every adapter's agent must write Claude-style stream-json to stdout
(see ``stream.py``), so the rest of the conductor is agent-agnostic.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

UNATTENDED_PROMPT = """\
You are running unattended, inside an isolated git worktree managed by a conductor. \
No human is watching this session in real time.

Rules:
- Work only inside the current directory (the worktree). Do not read or modify files outside it.
- Do not push, do not switch or create branches, and do not rewrite git history. \
You do not need to commit: the conductor commits your changes when you finish.
- If you truly cannot proceed without a human decision, end your final message with a single line
  QUESTION: <the question>
  and stop. That is the only way to ask; no other way of asking will reach a human.
- Otherwise, finish the task fully. If the project has tests, run them.
- End with a brief summary (at most about 8 lines): what changed, why, and the test result."""

# A line starting with "QUESTION:" (optionally after markdown emphasis/bullets).
QUESTION_RE = re.compile(r"^[ \t>*_\-]*QUESTION:[*_]*[ \t]*", re.MULTILINE)


def extract_question(text: str | None) -> str | None:
    """Return the text after the LAST ``QUESTION:`` line (to end of text), or None."""
    if not text:
        return None
    last = None
    for m in QUESTION_RE.finditer(text):
        last = m
    if last is None:
        return None
    q = text[last.end():].strip()
    q = q.rstrip("*_").strip()
    return q or None


@dataclass
class LaunchSpec:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)  # overlay on os.environ


class Adapter(ABC):
    name: str = ""

    @abstractmethod
    def build(self, *, prompt: str, session_id: str, resume: bool,
              model: str | None, budget_usd: float | None,
              worktree: str) -> LaunchSpec:
        """Command line + env for one attempt, run with cwd=worktree."""

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(ok, reason). reason explains what is missing when not ok."""
