# Decisions

## What I built

Everything in Part 1:

- **Runs:** create, start in an isolated worktree, a live status board, review (diff, summary, test results) with merge, keep-branch or reject.
- **Restart survival:** agents keep running while the conductor is down and are re-attached when it comes back. An agent that died while the conductor was down is marked interrupted, and restarting it resumes the agent's session.
- **Cleanup:** worktrees are removed on accept, reject and cancel, and a janitor sweeps up the rest.

Extensions, chosen for the engineer in the brief, who has 4–6 agents and can't tell which ones need them:

| Extension | Why this one |
|---|---|
| **Attention ranking** | This is the core complaint. The board is sorted by "needs you": blocked > ready to merge > conflicts or failing tests > failed or interrupted > wedged > working. The tab title shows the count. |
| **Blocked on a question** | A question has to find the user, and the run should continue where it stopped. Questions go to the top of the board with an inline answer box. The answer resumes the same agent session. |
| **Conflict handling** | "Two agents keep editing the same file and I find out at merge time." Overlapping files are flagged *while runs are live*. Merge checks are recomputed whenever the target moves. A one-click hand-off asks the agent to resolve conflicts, and a guard refuses to merge committed conflict markers. |
| **Budgets + watchdog** | Cheap to do well, because `claude --max-budget-usd` enforces the cap. A stall flag and a hard timeout catch wedged runs without anyone watching. |
| **Review surface (partial)** | Per-file diff with collapsing, a merge-cleanliness line, test results that the *conductor* produced rather than the agent's claim, and "Request changes" to send feedback to the same session. |

## What I left out, and why

- **Auto-merge.** Before letting this merge unattended I'd want tests run on the *merge result* (not just the branch), a protected-path list, and a size limit. Without those, "tests pass" isn't enough.
- **Phone notifications.** The attention ranking and tab title cover the at-the-desk case. Push needs an external service, which I didn't want in a no-dependency tool.
- **Handoff, multi-user, placement across machines.** All three need auth and a shared server, so they're a different architecture (see below), not a feature on top of this one.
- **Workflows, fan-out, scheduled runs.** These are useful, but each adds a concept to the UI before the basic loop is trusted.
- **Harvesting.** It's half there: every transcript stays in `~/.conductor/runs/`, but there's no search.

## What I'd do with two more days

1. Test the merge result: in a temporary worktree, merge the target into the branch, run the tests there, and show "tests pass after merge". This is the prerequisite for auto-merge.
2. Push updates over server-sent events instead of polling, and a desktop notification when a run enters "needs you".
3. Detect questions the agent asked in prose without the `QUESTION:` marker (a cheap classifier on the final message), so they aren't shown as "finished".
4. Re-run tests automatically when the target moves under a finished run.
5. A searchable transcript index (SQLite FTS over `stream.jsonl`).

## What breaks at 10× runs, or with 10 users

- **One machine.** 40 concurrent agents need CPU, RAM and disk for 40 worktrees, and `max_parallel` just queues them. You'd need a worker pool with placement, where each worker runs the same runner/stream-file protocol and the stream goes to central storage.
- **SQLite and a single supervisor thread.** These are fine for hundreds of runs a day on one box. Ten users means a shared Postgres, with leases (a heartbeat column) instead of "the pid is on this machine".
- **Polling.** 10 users × 1.5 s × full board is wasteful. Use SSE plus per-run deltas.
- **No auth.** It binds to localhost and checks Origin. Multi-user needs identity, run ownership, and an audit trail of who merged what.
- **Merging into a shared target.** With many runs merging, the target moves constantly and most finished runs go stale. You'd need a merge queue (rebase, test, merge, one at a time) rather than per-run merge buttons.
- **Disk.** The janitor bounds worktrees to live runs, but 40 live worktrees of a big monorepo is still a lot. You'd need sparse checkouts or a shared object store, plus a hard disk quota that refuses new runs before the disk fills.

## Known weak spots

- The test step runs the repo's test command through a shell with no sandbox, the same way the agent itself runs unsandboxed with permissions skipped. It assumes a trusted repo on your own machine.
- A cancelled agent is sent SIGTERM and then SIGKILL as a process group. Anything it daemonized with `setsid` would escape.
- Re-attach relies on `/proc` to verify pids. On macOS it falls back to "the pid is alive", which pid reuse could fool after a reboot.
