# Decisions

## What I built

**All of Part 1:**
- runs in isolated worktrees and a live status board;
- review: the diff, the agent's summary, and tests the conductor ran itself; then merge, keep the branch, or reject;
- agents survive the conductor being killed and are re-attached; dead ones are marked interrupted and resume their session on restart;
- worktrees are cleaned up on every exit path, plus a janitor.

**The customer then grew:** a team lead whose engineers start hundreds to thousands of runs. The extensions follow that.

| Extension | Why |
|---|---|
| **Attention ranking** | The core complaint. Tiers run blocked > policy > ready > conflicts or failing > failed > wedged > working. Within a tier, the longest wait comes first. A "look at these next" strip and groups with bulk actions; the score is stored and indexed, so it pages at thousands. |
| **Guardrails: repository policy** | Boundaries the team owns in `.conductor/policy.toml`, tighten-only across layers. Enforced at create, at launch (the agent's own deny rules), live (every tool call) and before merge (the diff). Overrides need a reason and are audited. Agents can't edit the policy itself. |
| **Blocked on a question** | The question goes to the top with an answer box; the answer resumes the same session. |
| **Conflict handling** | Overlap on the *same lines* is flagged while runs are live: in the field test, 1 pair flagged instead of 10. One click hands conflicts back to the agent; committed markers block the merge. |
| **Repeatable workflows** | Define the five-step chain once and fire it at any task. The steps share one branch, with test requirements and approval gates. |
| **Scheduled and triggered runs** | Cron in UTC, and on-change triggers. Each fire is a compare-and-set claim, so it never fires twice, even across restarts. |
| **Placement across machines** | A coordinator plus workers over a lease protocol, with git as the only file transport. Proven at 1,000 runs with chaos: see [scale-test.md](scale-test.md). |
| **Budgets, watchdog, reaching me** | A per-run cap; stall flag and timeout; a browser notification only when a run *becomes* needs-you. |

## Left out, and why

- **Auto-merge.** It needs tests on the *merged result* and a merge queue first. The policy (protected targets, size caps, approval) is the groundwork.
- **Multi-user and handoff.** They need identity and run ownership. The audit log records actors, but there are no logins.
- **Fan-out, phone push, transcript search.** Each is useful, but none is needed for the loop to be trusted.

## With two more days

1. Tests on the merge result, then a merge queue and policy-gated auto-merge.
2. Server-sent events instead of polling.
3. Detecting questions asked in prose, the gap seen in the field test.
4. Full-text search over transcripts.

## At 10× (10 engineers, 10,000 runs a day)

- **The coordinator** is one process on SQLite. It held 64 concurrent runs and 580 runs a minute on 4 cores. Beyond that: Postgres, the same compare-and-set transitions, and several coordinators.
- **Real agents are the bottleneck:** API rate, cost and *review capacity*. That is why attention ranking, groups and bulk actions matter more than raw throughput.
- **Git:** many workers pushing to one repository need a real git server, which is what `CONDUCTOR_REPO_REMOTES` is for. Worktree disk is bounded by the janitor; quotas are not built.

## Known weak spots

- By default, agents and their tests run unsandboxed. A shell redirect outside the worktree passes every policy check; a real agent did it once (an empty `/tmp/x.py`). Kept as the default on purpose. `[sandbox] writes = "worktree"` closes it with an OS sandbox (bubblewrap or sandbox-exec, failing closed); see [the example](../examples/guardrails/write-escape/). Network and reads stay open.
- Workers report what they ran. A compromised worker could lie; the policy's pre-merge diff check is the backstop.
- Questions without the `QUESTION:` marker are shown as "finished".
