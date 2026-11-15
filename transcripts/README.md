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

## How I worked with agents

1. **Contracts first, then fan-out.** Before starting any agent I wrote the pieces everything else depends on: the state machine (`states.py`), the store with compare-and-set transitions (`db.py`), config, and the HTTP contract (`docs/api.md`). Then three agents built git ops, agent adapters and the UI in parallel, each confined to its own files. That's why there were no merge fights between them.
2. **Core stays with me.** The process model (detached runner, offset-based ingest, recovery) is where correctness lives, so I wrote the supervisor, runner and service myself while the agents worked.
3. **Probe before specifying.** I captured a real `claude -p --output-format stream-json` run first and gave that file to the parser agent. It parsed real events rather than guessed ones.
4. **Verify against the real thing, not the agent's report.** Every agent's "tests pass" was re-run. Then the whole system was driven end to end: the fake agent, a real Claude run, `kill -9` of the conductor, a headless browser looking at the real UI, and frame-by-frame checks of the recorded video. Most bugs below were found that way, not by unit tests.
5. **When agents stopped.** Two agents (UI and lifecycle tests) were cut off by an account usage limit. The UI agent had already written the UI but not checked it in a browser, so I checked it against the real backend. The test agent had written nothing, so I wrote `tests/test_lifecycle.py` myself.

## What the agents got wrong, and how it was caught

- The conductor committed `__pycache__/*.pyc` into a run's branch (demo repo has no `.gitignore`) — caught reading the first real-Claude diff (6 files for a 2-line fix). Fixed: junk pathspec exclusions in `gitops.commit_all`.
- The fake agent "resolved" a conflict by appending to a file full of `<<<<<<<` markers, and git reported the branch as merging cleanly — caught in the e2e conflict walkthrough. Fixed: `gitops.conflict_markers` blocks the merge and ranks the run as needing attention.
- The UI sent `test_cmd: ""` for "Skip tests" but the backend read it as "auto-detect" — caught reading the UI code against the service.
- `executescript` implicitly commits, which broke my transaction wrapper on first start — caught by the first smoke run.
- Disk usage in the header only refreshed at startup — caught in a screenshot.
- After an agent resolved conflicts, the review diff still used the original base commit, so it also showed everything main had gained since. This was caught by watching frames of the recorded demo (3 files shown for a 1-file change). Fixed: the diff is taken from the merge-base with the target (`gitops.review_base`), and a regression assertion was added to the conflict test.
- Cancelling a run while its tests ran left the test process alive, and the budget was per attempt rather than per run. Both were found reviewing the code against the brief's "budgets" ask. Fixed, with a test for each.
