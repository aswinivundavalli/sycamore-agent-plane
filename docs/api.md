# HTTP API

JSON in, JSON out. Errors: non-2xx with `{"error": "message"}`.
Served by `conductor/server.py`; the UI in `conductor/static/` is the only client.

## Run object

```jsonc
{
  "id": 7,
  "title": "Add retry to fetch()",
  "task": "full free-text task",
  "repo_path": "/home/me/proj",
  "base_branch": "main",            // target branch the result merges into
  "base_commit": "abc123…",
  "branch": "conductor/run-7",
  "worktree_path": "/home/me/.conductor/worktrees/run-7",
  "agent": "claude",                // "claude" | "fake"
  "model": null,
  "test_cmd": "python3 -m unittest", // null = autodetect, "" = none
  "budget_usd": 5.0,
  "state": "running",               // queued|starting|running|waiting_input|testing|finished|failed|interrupted|accepted|rejected|cancelled
  "activity": "Editing src/fetch.py", // short, current, human-readable
  "activity_at": 1731690000.1,      // unix seconds
  "last_output_at": 1731690000.1,
  "stalled": false,                 // watchdog: no agent output for a while
  "attempt": 1,
  "session_id": "uuid",
  "summary": "agent's final message (markdown-ish text)",
  "question": "text of the question when state=waiting_input",
  "cost_usd": 0.42,                 // total over attempts
  "test_status": "passed",          // null | "none" | "running" | "passed" | "failed"
  "test_output": "last ~20KB of test output",
  "changed_files": ["src/fetch.py"],
  "diff_stat": {"files": 2, "additions": 30, "deletions": 4},
  "merge_check": {"clean": true, "conflicts": [], "target_commit": "…", "behind": 0, "checked_at": 1731690000},
  "error": null,
  "result_ref": "merge commit sha or kept branch name",
  "created_at": 0, "updated_at": 0, "started_at": 0, "finished_at": 0,

  // computed by the server:
  "attention_score": 90,            // higher = look first; 0 = nothing to do
  "attention_reason": "Finished, tests pass: ready to merge",
  "overlaps": [{"run_id": 3, "title": "…", "state": "running", "files": ["src/fetch.py"]}],
  "actions": ["accept", "accept_branch", "reject", "restart", "answer", "cancel", "resolve_conflicts", "retest"]
}
```

`actions` lists the buttons that are legal right now; the UI shows only those.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/runs` | | `{"runs":[Run…], "stats": {...}}` sorted by attention desc, then id desc |
| POST | `/api/runs` | `{repo_path, task, title?, base_branch?, agent?, model?, test_cmd?, budget_usd?}` | `Run` (201) |
| GET | `/api/runs/<id>` | | `Run` plus `"events": [Event…]` (last 300) |
| GET | `/api/runs/<id>/diff` | | `{"diff": "unified diff text", "files": [{"path","status","additions","deletions"}], "merge_check": {...}}` |
| GET | `/api/runs/<id>/log` | `?attempt=N` | `{"lines": ["raw stream-json line", …]}` (last 500) |
| POST | `/api/runs/<id>/answer` | `{text}` | `Run` – only in `waiting_input` |
| POST | `/api/runs/<id>/restart` | `{note?}` | `Run` – from `interrupted` / `failed` / `finished` |
| POST | `/api/runs/<id>/cancel` | | `Run` – kills the agent, cleans up |
| POST | `/api/runs/<id>/accept` | `{mode: "merge" \| "branch"}` | `Run` – merge into base_branch, or keep branch only |
| POST | `/api/runs/<id>/reject` | | `Run` |
| POST | `/api/runs/<id>/resolve_conflicts` | | `Run` – queues an attempt that merges base into the branch and asks the agent to fix conflicts |
| POST | `/api/runs/<id>/retest` | | `Run` |
| GET | `/api/repo?path=…` | | `{"path","root","branches":[…],"current_branch","test_cmd_guess"}` or 400 |
| GET | `/api/stats` | | `{"counts": {state: n}, "worktree_bytes": n, "worktrees": n, "max_parallel": n, "recent_repos": [paths]}` |

`stats` in `/api/runs` is the same object as `/api/stats`.

## Event

```json
{"id": 1, "run_id": 7, "ts": 1731690000.1, "attempt": 1,
 "kind": "state|activity|tool|text|question|answer|result|test|error|info",
 "message": "human readable", "data": {}}
```

## Fleet scale (phase 1): attention, groups, policy

### Run object: new fields
```jsonc
{
  "attention_score": 95,            // stored; >= 60 means "needs you"
  "attention_reason": "Policy violation: touches .github/workflows/ci.yml",
  "attention_since": 1731690000.1,  // when it entered this tier; older = more urgent
  "group_key": "batch:7f3a…",       // runs created together, or "schedule:3"
  "group_title": "Dependency bumps",
  "policy": {...},                  // effective policy (see GET /api/policy)
  "policy_sha": "a1b2c3d4e5f6",
  "violations": [                   // live + pre-merge findings
    {"rule": "forbidden_path", "detail": "edits .github/workflows/ci.yml", "action": "block", "at": 1731690000}
  ],                                // action: kill (agent was stopped) | block (merge blocked) |
                                    //         approval (needs an override reason) | flag (informational)
  "merge_check": {..., "policy": [/* same shape as violations, from the diff */]},
  "actions": [..., "accept_override"]   // when policy blocks a plain accept
}
```

### Endpoints
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/runs` | `?view=needs_you\|active\|done\|all&limit=50&offset=0&repo=&group=&q=` | `{"runs":[Run…], "total": n, "next_offset": n\|null, "next3":[Run×≤3], "groups":[Group…], "stats":{…}}`. With no `view`, behaves as before (all runs). |
| POST | `/api/batches` | `{repo_path, title, tasks:[str…], base_branch?, agent?, budget_usd?, test_cmd?}` | `{"group_key", "runs":[ids]}` (201). Creates one run per task, grouped. |
| POST | `/api/groups/<group_key>/<op>` | op = `accept_ready` \| `reject_failed` \| `restart_interrupted` | `{"done":[ids], "skipped":[{"id","reason"}]}` |
| POST | `/api/runs/<id>/accept` | `{mode, override_reason?}` | 409 with the violations when policy blocks; with a non-empty `override_reason` it merges and records an audit entry |
| GET | `/api/policy` | `?repo=<path>` | `{"policy":{…}, "rules":[{"rule","value"}], "sources":[paths], "sha"}` |
| GET | `/api/audit` | `?run_id=&limit=` | `{"entries":[{"ts","actor","run_id","action","detail"}]}` |

`Group`: `{"group_key","title","total","needs_you","ready","failed","interrupted","active","done","top_score","latest"}` (`failed` includes interrupted runs).
`next3`: the three highest-attention runs that need you (attention_score ≥ 60), same order as the needs_you view.

## Workflows and schedules (phase 2)

Run object adds `workflow` (name), `workflow_step` (0-based index of the current step), `workflow_def` (the snapshot: `{name, description, source, steps:[{id, prompt, tests, gate, on_fail, session, budget_usd}]}`) and `schedule_id`.
A workflow gate puts the run in `waiting_input` with a `question`; answering "approve" starts the next step, and any other text is passed to that step as a reviewer note.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/workflows` | `?repo=<path>` | `{"workflows":[{name, description, source, steps, step_ids}]}` (built-in, `CONDUCTOR_HOME/workflows`, then the repo's `.conductor/workflows`) |
| POST | `/api/runs` | add `workflow: "<name>"` | the run starts at step 1 of that workflow |
| GET | `/api/schedules` | | `{"schedules":[Schedule…]}` |
| POST | `/api/schedules` | `{name, repo_path, task, kind: "cron"\|"on_change", cron?, watch_ref?, poll_secs?, base_branch?, workflow?, agent?, budget_usd?, overlap?: "skip"\|"queue", catch_up?: "once"\|"skip"}` | `Schedule` (201) |
| POST | `/api/schedules/<id>/<op>` | op = `pause` \| `resume` \| `run_now` \| `delete` | `Schedule` (or `{"deleted": id}`) |

`Schedule`: `{id, name, repo_path, base_branch, task, workflow, agent, budget_usd, kind, cron, watch_ref, poll_secs, overlap, catch_up, enabled, next_fire_at, last_fired_at, last_run_id, last_seen_sha, last_note, created_at, updated_at}`. Cron expressions are UTC. Runs a schedule creates are grouped under `group_key = "schedule:<id>"`.
