# Scale test: 1,000 runs across 4 workers, with a worker and the coordinator killed mid-run

**Result:** all 1,000 runs finished, at 580 runs a minute with 64 running at once. No attempt started twice, and no agent output was lost or duplicated, even though one worker was killed outright and the coordinator was `kill -9`'d while the test ran. The board's p95 response time stayed under 80 ms.

Reproduce it with:

```bash
python3 scripts/loadtest.py --runs 1000 --workers 4 --capacity 16
```

## Setup

- **Machine:** one 4-core, 15 GB container. The coordinator and all four workers ran on it as separate processes talking over HTTP. They used the same protocol a worker on another machine would use.
- **Processes:**
  - one coordinator started with `--max-parallel 0`, so it executes nothing itself;
  - 4 workers with capacity 16 each, for 64 runs in flight.
- **Agent:** the scripted fake agent, so the test costs nothing. Everything around it is real:
  - its own process tree under the detached runner;
  - a worktree per run in the worker's cache clone;
  - a commit, pushed into the repository;
  - streamed events, the live policy check, and diff and policy checks on the coordinator.
- **Submission:** 10 batches of 100 runs (`POST /api/batches`), each run writing its own file.
- **Chaos:**
  - at 30% finished, worker 0 was killed with SIGKILL, orphaning its agents;
  - at 50% finished, the coordinator was killed with SIGKILL and restarted;
  - interrupted runs were restarted per group, as an engineer would with "Restart interrupted".

## Results

| | Run 1 | Run 2 (after the batch fix below) |
|---|---|---|
| Runs finished | 1,000 / 1,000 | 1,000 / 1,000 |
| Submitting 1,000 runs | 42.6 s | **2.1 s** |
| Submit to last run finished | 102 s | 103 s |
| Throughput | 587 runs/min | 580 runs/min |
| Attempts executed | 1,010 | 1,007 |
| Attempts that ran twice | **0** | **0** |
| Transcripts compared with the database | 1,000 | 1,000 |
| Lost or duplicated events | **0** | **0** |
| Board (`needs_you`, 50 runs), p50 / p95 / max | 19 / 54 / 127 ms | 19 / 75 / 1,053 ms |
| Runs interrupted by the chaos and restarted | 13 | 16 |

How each check was made:
- **Double starts:** every (run, attempt) pair must own exactly one runner directory across all workers' disks.
- **Event integrity:** for each run's final attempt, the tool calls and results parsed from the raw transcript on the worker must equal the events stored in the coordinator's database.
- **Board latency:** sampled four times a second throughout the test. The 1,053 ms maximum in run 2 was a request that arrived as the restarted coordinator came back up.

## What the test found, and what changed

1. **Worker git race.** In a 100-run trial, one run failed: `git fetch` in a worker's cache clone ran while another job on that worker was updating a worktree of the same clone, and git read a half-written worktree HEAD. Every operation on a cache (fetch, worktree add and remove, commit, push, merge) now holds a per-cache lock, and a failed fetch is retried once. No failures since.
2. **Batch creation was slow.** It took 42 ms a run (42.6 s for 1,000), because each run in a batch separately validated the repository with several git calls and read the policy files. A batch now resolves the repository, policy and workflows once: **0.3 ms a run** in isolation, and 2.1 s for 1,000 under load.
3. **Merge checks didn't scale with finished runs.** The periodic merge-check refresh ran `git rev-parse` once per finished run, every 5 s, which is 1,000 subprocesses per pass at this size. It now resolves each repository's target branch once per pass. This was found by reading the code before the test.
4. **Coordinator downtime counted against workers.** After a coordinator restart, every worker's "last seen" time was stale by the length of the outage, so healthy workers could be declared lost. Leases now count from whichever is later: the worker's last heartbeat or the coordinator's start.
5. **A checker mistake, not a product bug.** The first integrity check also compared attempts on the killed worker, whose orphaned agents kept writing after the coordinator had, correctly, marked them interrupted. The check now compares each run's final attempt.

## What this does not show

- **Real agents are much slower than the fake one.** A Claude Code run takes 1–2 minutes and costs real money, so at 1,000 runs the limits are API rate, budget and review capacity, not the conductor.
- **This was one 4-core box.** Spreading workers across real machines adds git transfer time over the network, through `CONDUCTOR_REPO_REMOTES`, which wasn't measured here.
- **There is one coordinator with SQLite.** WAL handled about 10 event batches a second plus the board polling without trouble. Several coordinators behind a load balancer would need Postgres with leases; the `Store` interface is kept narrow so that swap stays contained.
- **No sandboxing.** Agents and their test commands run unsandboxed on the workers, with the same trust model as the single-machine conductor.
