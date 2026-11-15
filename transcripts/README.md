# Agent transcripts

Raw Claude Code transcripts (JSONL, as the tool saves them).

`build-session/` — building the conductor.
- `main-session.jsonl` — the orchestrating session: I wrote the core (state machine, store, supervisor, runner, service, server) and integrated, tested end to end, and fixed bugs.
- `subagents/` — agents I ran **in parallel** against fixed contracts (`docs/api.md`, `conductor/states.py`, `conductor/db.py`):
  - `agent-a7168c10561be378b` — `conductor/gitops.py` + `tests/test_gitops.py`
  - `agent-aab5af61688774032` — `conductor/agents/*` (Claude adapter, fake agent, stream parser) + tests
  - `agent-a4096df79e90cf631` — the web UI (`conductor/static/*`). Cut off by an account usage limit just before its browser check; I verified the UI against the real backend myself.
  - `agent-a57d18fa6cb43c286` — lifecycle integration tests. Cut off by the same limit before writing anything; I wrote `tests/test_lifecycle.py` myself.

`conductor-managed-run/` — the stream of a real Claude Code run started *by the conductor* (fixing `word_count` in the demo repo), as stored in `~/.conductor/runs/run-N/attempt-K/stream.jsonl`.

## What the agents got wrong, and how it was caught

- The conductor committed `__pycache__/*.pyc` into a run's branch (demo repo has no `.gitignore`) — caught reading the first real-Claude diff (6 files for a 2-line fix). Fixed: junk pathspec exclusions in `gitops.commit_all`.
- The fake agent "resolved" a conflict by appending to a file full of `<<<<<<<` markers, and git reported the branch as merging cleanly — caught in the e2e conflict walkthrough. Fixed: `gitops.conflict_markers` blocks the merge and ranks the run as needing attention.
- The UI sent `test_cmd: ""` for "Skip tests" but the backend read it as "auto-detect" — caught reading the UI code against the service.
- `executescript` implicitly commits, which broke my transaction wrapper on first start — caught by the first smoke run.
- Disk usage in the header only refreshed at startup — caught in a screenshot.
