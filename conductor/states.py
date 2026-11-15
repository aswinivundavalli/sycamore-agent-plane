"""Run lifecycle: the states a run can be in and the legal moves between them.

Every state change in the system goes through ``Store.transition`` which does a
compare-and-set against the *current* state in SQLite. The table below is the
single source of truth for which moves are legal; anything else is a bug.

    queued ──► starting ──► running ──► testing ──► finished ──► accepted
       ▲           │           │  │                    │   │
       │           ▼           │  └──► waiting_input   │   └──► rejected
       │      interrupted ◄────┘          │            │
       │           │           └──► failed             └──► queued (resolve conflicts)
       └───────────┴──────────────────────┘ (restart / answer)
"""

QUEUED = "queued"
STARTING = "starting"
RUNNING = "running"
WAITING_INPUT = "waiting_input"
TESTING = "testing"
FINISHED = "finished"  # agent done, tests run, awaiting human review
FAILED = "failed"
INTERRUPTED = "interrupted"
ACCEPTED = "accepted"
REJECTED = "rejected"
CANCELLED = "cancelled"

ALL = [QUEUED, STARTING, RUNNING, WAITING_INPUT, TESTING, FINISHED, FAILED,
       INTERRUPTED, ACCEPTED, REJECTED, CANCELLED]

# A live agent (or test) process may exist for runs in these states.
ACTIVE = {STARTING, RUNNING, TESTING}
# Agent processes specifically.
AGENT_ACTIVE = {STARTING, RUNNING}
# Nothing more will ever happen to these; their worktree must be gone.
TERMINAL = {ACCEPTED, REJECTED, CANCELLED}
# A worktree should exist for these.
HAS_WORKTREE = set(ALL) - TERMINAL - {QUEUED}

TRANSITIONS = {
    QUEUED: {STARTING, CANCELLED},
    STARTING: {RUNNING, FAILED, INTERRUPTED, CANCELLED},
    RUNNING: {TESTING, WAITING_INPUT, FAILED, INTERRUPTED, CANCELLED},
    WAITING_INPUT: {QUEUED, REJECTED, CANCELLED},
    TESTING: {FINISHED, INTERRUPTED, CANCELLED},
    FINISHED: {ACCEPTED, REJECTED, QUEUED, TESTING},
    FAILED: {QUEUED, REJECTED, ACCEPTED},
    INTERRUPTED: {QUEUED, REJECTED},
    ACCEPTED: set(),
    REJECTED: set(),
    CANCELLED: set(),
}


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def attention(run: dict) -> tuple[int, str]:
    """Rank how urgently a run needs the human. Higher = look at it first.

    Returns (score, reason). Score 0 means "nothing for you to do".
    """
    s = run["state"]
    if s == WAITING_INPUT:
        return 100, "Agent is blocked on your answer"
    if s == FINISHED:
        mc = run.get("merge_check") or {}
        if mc.get("conflicts"):
            return 85, "Finished, but conflicts with " + (run.get("base_branch") or "target")
        if run.get("test_status") == "failed":
            return 80, "Finished, tests failing"
        if run.get("test_status") == "passed":
            return 90, "Finished, tests pass: ready to merge"
        return 75, "Finished: ready for review"
    if s == FAILED:
        return 70, "Failed: " + (run.get("error") or "agent error")[:80]
    if s == INTERRUPTED:
        return 65, "Interrupted: restart or discard"
    if s in (RUNNING, STARTING) and run.get("stalled"):
        return 60, "No output for a while: may be wedged"
    if s in (RUNNING, STARTING, TESTING):
        return 20, ""
    if s == QUEUED:
        return 10, ""
    return 0, ""
