"""Where the conductor keeps things, and its tunables.

Everything the conductor owns lives under one directory (``CONDUCTOR_HOME``,
default ``~/.conductor``) so it is obvious what to delete and easy to measure:

    $CONDUCTOR_HOME/
        conductor.db              SQLite: runs + events (the source of truth)
        worktrees/run-<id>/       one git worktree per live run
        runs/run-<id>/attempt-<n>/
            prompt.txt            what the agent was asked this attempt
            stream.jsonl          the agent's raw stdout (its transcript)
            stderr.log
            runner.json           pid of the detached runner process
            exit.json             written by the runner when the agent exits
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(
        os.environ.get("CONDUCTOR_HOME", Path.home() / ".conductor")).expanduser())
    host: str = os.environ.get("CONDUCTOR_HOST", "127.0.0.1")
    port: int = _env_int("CONDUCTOR_PORT", 7700)
    # How many agents may run at once; the rest wait in "queued".
    max_parallel: int = _env_int("CONDUCTOR_MAX_PARALLEL", 4)
    # Watchdog: flag a run as stalled after this many seconds without output...
    stall_secs: int = _env_int("CONDUCTOR_STALL_SECS", 180)
    # ...and kill it after this many seconds of wall time.
    max_run_secs: int = _env_int("CONDUCTOR_MAX_RUN_SECS", 3600)
    # Tests get this long before being killed.
    test_timeout_secs: int = _env_int("CONDUCTOR_TEST_TIMEOUT_SECS", 600)
    # Default per-attempt spend cap passed to the agent (USD). 0 = no cap.
    default_budget_usd: float = float(os.environ.get("CONDUCTOR_BUDGET_USD", "5"))
    # Supervisor loop period.
    tick_secs: float = 0.5

    @property
    def db_path(self) -> Path:
        return self.home / "conductor.db"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    def worktree_for(self, run_id: int) -> Path:
        return self.worktrees_dir / f"run-{run_id}"

    def attempt_dir(self, run_id: int, attempt: int) -> Path:
        return self.runs_dir / f"run-{run_id}" / f"attempt-{attempt}"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.worktrees_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)
