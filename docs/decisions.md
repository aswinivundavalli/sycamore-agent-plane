# Decisions

## What I built

All of Part 1: runs in isolated worktrees, a live status board, review (diff, the agent's summary, tests the conductor ran itself) with merge, keep-branch or reject. Agents survive the conductor being killed and are re-attached; dead ones are marked interrupted and resume their session on restart. Worktrees are cleaned up on every exit path, plus a janitor.

Extensions, chosen for an engineer with 4–6 agents who can't tell which ones need them:

| Extension | Why |
|---|---|
| **Attention ranking** | The core complaint. Board order: blocked > ready to merge > conflicts or failing tests > failed or interrupted > wedged > working. |
| **Blocked on a question** | The question goes to the top with an answer box; the answer resumes the same session. |
| **Conflict handling** | Same-file overlap is flagged *while runs are live*. Merge checks follow the moving target. One click hands conflicts back to the agent. Committed `<<<<<<<` markers block the merge. |
| **Reaching me (browser)** | A notification fires only when a run *becomes* needs-you and the tab is unfocused. Nothing else notifies. |
| **Budgets + watchdog** | A per-run cap enforced by `claude --max-budget-usd`. Stall flag and a hard timeout. |

## Left out, and why

- **Auto-merge.** First I'd want tests run on the *merge result*, protected paths and a size limit. "Tests pass on the branch" isn't enough to merge without a human.
- **Phone push.** Needs an external service. It would hook into the same "becomes needs-you" transition.
- **Handoff, multi-user, multiple machines.** These need auth and a shared server: a different architecture, not a feature.
- **Workflows, fan-out, schedules.** Each adds a concept before the basic loop is trusted.
- **Transcript search.** Every transcript is kept in `~/.conductor/runs/`, but there's no index.

## With two more days

1. Tests on the merge result (temporary worktree), then re-test automatically when the target moves. This is the prerequisite for auto-merge.
2. Server-sent events instead of polling.
3. Catch questions asked in prose without the `QUESTION:` marker (a classifier on the final message).
4. Full-text search over transcripts (SQLite FTS).

## At 10× runs or 10 users

- **One machine:** 40 agents need 40 worktrees' worth of CPU, RAM and disk. Move to a worker pool that speaks the same runner/stream-file protocol, with streams shipped to central storage.
- **SQLite and a pid check:** move to Postgres with leases (a heartbeat column) instead of "the pid is alive on this box".
- **Per-run merge buttons:** the target moves constantly and finished runs go stale. Use a merge queue (rebase, test, merge, one at a time).
- **No auth:** it's localhost-only with an Origin check. Multiple users need identity, run ownership and an audit trail of merges.
- **Disk:** use sparse checkouts or a shared object store, plus a quota that refuses new runs before the disk fills.

## Known weak spots

- Agents (permissions skipped) and test commands run unsandboxed: this assumes a trusted repo on your own machine.
- Cancel kills the process group, but a `setsid`-daemonized child would escape.
- Pid verification uses `/proc`. On macOS a reused pid after a reboot could fool it.
