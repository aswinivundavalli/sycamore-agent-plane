"""SQLite persistence. The database is the source of truth for every run.

Rules that keep restarts safe:
  * Every state change is a compare-and-set (``transition``). Two callers racing
    to start the same queued run cannot both win, so no run starts twice.
  * Agent output is ingested from a file with a stored byte offset, in the same
    transaction as the events it produced, so re-reading after a crash neither
    loses nor duplicates events.
  * Each thread uses its own connection (sqlite3 connections are not shared).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import states

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    task            TEXT NOT NULL,
    repo_path       TEXT NOT NULL,
    base_branch     TEXT NOT NULL,      -- branch the result merges into
    base_commit     TEXT,               -- commit the worktree was cut from
    branch          TEXT,               -- conductor/run-<id>
    worktree_path   TEXT,
    agent           TEXT NOT NULL DEFAULT 'claude',
    model           TEXT,
    test_cmd        TEXT,
    budget_usd      REAL,
    state           TEXT NOT NULL,
    activity        TEXT,               -- short "what is it doing now"
    activity_at     REAL,
    last_output_at  REAL,
    stalled         INTEGER NOT NULL DEFAULT 0,
    attempt         INTEGER NOT NULL DEFAULT 0,
    next_prompt     TEXT,               -- prompt for the next attempt
    resume          INTEGER NOT NULL DEFAULT 0,  -- next attempt resumes session
    pre_attempt     TEXT,               -- e.g. 'merge_target' before next attempt
    session_id      TEXT,
    runner_pid      INTEGER,
    stream_offset   INTEGER NOT NULL DEFAULT 0,
    exit_code       INTEGER,
    summary         TEXT,
    question        TEXT,
    cost_usd        REAL NOT NULL DEFAULT 0,
    attempt_cost_usd REAL NOT NULL DEFAULT 0,
    test_status     TEXT,               -- none | running | passed | failed
    test_output     TEXT,
    changed_files   TEXT,               -- JSON list
    changed_ranges  TEXT,               -- JSON {base, files: {path: [[start, end]]}}
    diff_stat       TEXT,               -- JSON {files, additions, deletions}
    merge_check     TEXT,               -- JSON {clean, conflicts, checked_at, target_commit}
    error           TEXT,
    result_ref      TEXT,               -- merge commit or kept branch
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    started_at      REAL,
    attempt_started_at REAL,
    finished_at     REAL
);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id),
    ts      REAL NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    kind    TEXT NOT NULL,   -- state | activity | tool | text | question | answer | result | test | error | info
    message TEXT NOT NULL,
    data    TEXT
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);

-- Changed line ranges per run and file: overlap detection is an indexed join
-- on (repo, path) instead of comparing every pair of runs.
CREATE TABLE IF NOT EXISTS run_files (
    run_id  INTEGER NOT NULL REFERENCES runs(id),
    repo    TEXT NOT NULL,
    base    TEXT,
    path    TEXT NOT NULL,
    ranges  TEXT NOT NULL,             -- JSON [[start, end], ...]
    PRIMARY KEY (run_id, path)
);
CREATE INDEX IF NOT EXISTS run_files_repo_path ON run_files(repo, path);

-- Who did what that bypassed or enforced a guardrail.
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,             -- 'user' | 'conductor' | 'schedule:<id>' | 'worker:<id>'
    run_id  INTEGER,
    action  TEXT NOT NULL,             -- override | policy_block | policy_kill | approve | ...
    detail  TEXT                       -- JSON
);
CREATE INDEX IF NOT EXISTS audit_run ON audit(run_id, id);

-- Scheduled and triggered runs. A fire is claimed with a compare-and-set on
-- next_fire_at, so a restart (or two coordinators) can never fire it twice.
CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    repo_path     TEXT NOT NULL,
    base_branch   TEXT NOT NULL,
    task          TEXT NOT NULL,
    workflow      TEXT,
    agent         TEXT NOT NULL DEFAULT 'claude',
    budget_usd    REAL,
    kind          TEXT NOT NULL,          -- cron | on_change
    cron          TEXT,                   -- for kind=cron (UTC)
    watch_ref     TEXT,                   -- for kind=on_change, e.g. "main"
    poll_secs     INTEGER NOT NULL DEFAULT 60,
    overlap       TEXT NOT NULL DEFAULT 'skip',   -- skip | queue
    catch_up      TEXT NOT NULL DEFAULT 'once',   -- once | skip
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_fire_at  REAL,
    last_fired_at REAL,
    last_run_id   INTEGER,
    last_seen_sha TEXT,
    last_note     TEXT,                   -- why the last due time did / did not fire
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
"""

JSON_FIELDS = {"changed_files", "changed_ranges", "diff_stat", "merge_check",
               "policy", "violations", "workflow_def"}

# Columns added after the first release: (name, SQL type). Added on open if missing,
# so a database from any earlier version keeps working.
MIGRATIONS = [
    ("changed_ranges", "TEXT"),
    # attention, stored so the board can be sorted and paged in SQL
    ("attention_score", "INTEGER NOT NULL DEFAULT 0"),
    ("attention_reason", "TEXT"),
    ("attention_since", "REAL"),
    # grouping: runs created together, by a schedule, ...
    ("group_key", "TEXT"),
    ("group_title", "TEXT"),
    # guardrails
    ("policy", "TEXT"),            # JSON: the effective policy for this run
    ("policy_sha", "TEXT"),
    ("violations", "TEXT"),        # JSON list of {rule, detail, action, at}
    # workflows and schedules (phase 2)
    ("workflow", "TEXT"),
    ("workflow_step", "INTEGER"),
    ("workflow_def", "TEXT"),
    ("schedule_id", "INTEGER"),
    # workers (phase 3)
    ("worker_id", "TEXT"),
    ("lease_expires_at", "REAL"),
]
POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS runs_attention ON runs(attention_score DESC, attention_since);
CREATE INDEX IF NOT EXISTS runs_group ON runs(group_key);
"""


class InvalidTransition(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)  # executescript manages its own transaction
        have = {r["name"] for r in self.conn().execute("PRAGMA table_info(runs)")}
        for name, sqltype in MIGRATIONS:
            if name not in have:
                self.conn().execute(f"ALTER TABLE runs ADD COLUMN {name} {sqltype}")
        self.conn().executescript(POST_MIGRATION_SQL)

    # -- connections -------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return _Tx(c)

    # -- rows --------------------------------------------------------------
    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in JSON_FIELDS:
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    d[k] = None
        d["stalled"] = bool(d.get("stalled"))
        d["resume"] = bool(d.get("resume"))
        return d

    @staticmethod
    def _enc(fields: dict) -> dict:
        out = {}
        for k, v in fields.items():
            out[k] = json.dumps(v) if k in JSON_FIELDS and v is not None else v
        return out

    def create_run(self, **fields: Any) -> dict:
        now = time.time()
        fields = {"state": states.QUEUED, "created_at": now, "updated_at": now, **fields}
        fields = self._enc(fields)
        cols = ",".join(fields)
        qs = ",".join("?" for _ in fields)
        with self.conn() as c:
            cur = c.execute(f"INSERT INTO runs ({cols}) VALUES ({qs})", list(fields.values()))
            run_id = cur.lastrowid
            c.execute(
                "UPDATE runs SET branch=? WHERE id=?", (f"conductor/run-{run_id}", run_id))
            self._event(c, run_id, 0, "state", "Run created (queued)")
            self._refresh_attention(c, run_id)
        return self.get(run_id)

    def get(self, run_id: int) -> dict | None:
        r = self.conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row(r)

    def list(self, states_in: Iterable[str] | None = None) -> list[dict]:
        if states_in:
            st = list(states_in)
            q = f"SELECT * FROM runs WHERE state IN ({','.join('?' * len(st))}) ORDER BY id"
            rows = self.conn().execute(q, st).fetchall()
        else:
            rows = self.conn().execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        return [self._row(r) for r in rows]

    def update(self, run_id: int, **fields: Any) -> None:
        """Update non-state fields. Use ``transition`` to change state."""
        assert "state" not in fields, "use transition() to change state"
        if not fields:
            return
        fields = self._enc({**fields, "updated_at": time.time()})
        sets = ",".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE runs SET {sets} WHERE id=?", [*fields.values(), run_id])
            self._refresh_attention(c, run_id)

    def transition(self, run_id: int, from_states: str | Iterable[str], to_state: str,
                   message: str | None = None, **fields: Any) -> bool:
        """Atomically move a run from one of ``from_states`` to ``to_state``.

        Returns False (and changes nothing) if the run is not currently in one
        of ``from_states``. This is what makes "start" and "accept" idempotent.
        """
        if isinstance(from_states, str):
            from_states = [from_states]
        from_states = list(from_states)
        for s in from_states:
            if not states.can_transition(s, to_state):
                raise InvalidTransition(f"{s} -> {to_state}")
        fields = self._enc({**fields, "state": to_state, "updated_at": time.time()})
        sets = ",".join(f"{k}=?" for k in fields)
        qs = ",".join("?" for _ in from_states)
        with self.conn() as c:
            row = c.execute("SELECT state, attempt FROM runs WHERE id=?", (run_id,)).fetchone()
            cur = c.execute(
                f"UPDATE runs SET {sets} WHERE id=? AND state IN ({qs})",
                [*fields.values(), run_id, *from_states])
            if cur.rowcount != 1:
                return False
            self._event(c, run_id, row["attempt"], "state",
                        message or f"{row['state']} → {to_state}",
                        {"from": row["state"], "to": to_state})
            self._refresh_attention(c, run_id)
            if to_state in states.TERMINAL:
                c.execute("DELETE FROM run_files WHERE run_id=?", (run_id,))
        return True

    # -- attention ---------------------------------------------------------
    def _refresh_attention(self, c, run_id: int) -> None:
        """Recompute the stored attention score after any change to a run.

        Stored (and indexed) so the board can be sorted and paged in SQL at
        thousands of runs. ``attention_since`` marks when the run entered its
        current tier: within a tier, whoever has waited longest comes first.
        """
        row = self._row(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        if row is None:
            return
        score, reason = states.attention(row)
        if score != row.get("attention_score") or row.get("attention_since") is None:
            c.execute("UPDATE runs SET attention_score=?, attention_reason=?, attention_since=? "
                      "WHERE id=?", (score, reason, time.time(), run_id))
        elif reason != row.get("attention_reason"):
            c.execute("UPDATE runs SET attention_reason=? WHERE id=?", (reason, run_id))

    VIEWS = {
        "needs_you": f"attention_score >= {states.NEEDS_YOU}",
        "active": "state IN ('queued','starting','running','testing') "
                  f"AND attention_score < {states.NEEDS_YOU}",
        "done": "state IN ('accepted','rejected','cancelled')",
        "all": "1=1",
    }

    def page(self, view: str = "all", limit: int = 50, offset: int = 0,
             repo: str | None = None, group: str | None = None,
             q: str | None = None) -> tuple[list[dict], int]:
        """One page of runs for the board, most urgent first.

        Order: attention tier, then longest waiting, then the biggest change
        (blast radius), then newest. Returns (runs, total matching).
        """
        where, args = [self.VIEWS.get(view, "1=1")], []
        if repo:
            where.append("repo_path = ?")
            args.append(repo)
        if group:
            where.append("group_key = ?")
            args.append(group)
        if q:
            where.append("(title LIKE ? OR task LIKE ? OR CAST(id AS TEXT) = ?)")
            args += [f"%{q}%", f"%{q}%", q.lstrip("#")]
        w = " AND ".join(where)
        order = "attention_score DESC, " + (
            "finished_at DESC" if view == "done" else
            "attention_since ASC, "
            "COALESCE(json_extract(diff_stat, '$.additions'), 0) + "
            "COALESCE(json_extract(diff_stat, '$.deletions'), 0) DESC") + ", id DESC"
        conn = self.conn()
        total = conn.execute(f"SELECT COUNT(*) FROM runs WHERE {w}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM runs WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                            [*args, int(limit), int(offset)]).fetchall()
        return [self._row(r) for r in rows], total

    def counts(self) -> dict:
        """State counts and the needs-you total in one pass."""
        conn = self.conn()
        by_state = {r[0]: r[1] for r in conn.execute(
            "SELECT state, COUNT(*) FROM runs GROUP BY state")}
        needs = conn.execute(f"SELECT COUNT(*) FROM runs WHERE "
                             f"attention_score >= {states.NEEDS_YOU}").fetchone()[0]
        return {"counts": by_state, "needs_you": needs}

    def groups(self, limit: int = 50) -> list[dict]:
        """Groups of runs (a batch, a schedule) with per-group tallies, most
        urgent group first."""
        rows = self.conn().execute(f"""
            SELECT group_key, MAX(group_title) AS title, COUNT(*) AS total,
                   SUM(attention_score >= {states.NEEDS_YOU}) AS needs_you,
                   SUM(state = 'finished' AND test_status = 'passed') AS ready,
                   SUM(state IN ('failed','interrupted')) AS failed,
                   SUM(state IN ('queued','starting','running','testing')) AS active,
                   SUM(state IN ('accepted','rejected','cancelled')) AS done,
                   MAX(attention_score) AS top_score, MAX(created_at) AS latest
            FROM runs WHERE group_key IS NOT NULL
            GROUP BY group_key ORDER BY top_score DESC, latest DESC LIMIT ?""",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- overlap index -----------------------------------------------------
    def set_run_files(self, run_id: int, repo: str, base: str | None,
                      ranges: dict[str, list]) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM run_files WHERE run_id=?", (run_id,))
            c.executemany(
                "INSERT INTO run_files (run_id, repo, base, path, ranges) VALUES (?,?,?,?,?)",
                [(run_id, repo, base, p, json.dumps(r)) for p, r in ranges.items()])

    def overlap_candidates(self, run_ids: list[int]) -> list[dict]:
        """Pairs (mine, other) of live runs that changed the same file in the
        same repo, for the given runs only: an index lookup, not n x n."""
        if not run_ids:
            return []
        qs = ",".join("?" * len(run_ids))
        rows = self.conn().execute(f"""
            SELECT a.run_id AS mine, b.run_id AS other, a.path AS path,
                   a.base AS my_base, b.base AS other_base,
                   a.ranges AS my_ranges, b.ranges AS other_ranges,
                   r.title AS other_title, r.state AS other_state
            FROM run_files a
            JOIN run_files b ON b.repo = a.repo AND b.path = a.path AND b.run_id != a.run_id
            JOIN runs r ON r.id = b.run_id
            WHERE a.run_id IN ({qs})
              AND r.state NOT IN ('accepted','rejected','cancelled')""", run_ids).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["my_ranges"] = json.loads(d["my_ranges"])
            d["other_ranges"] = json.loads(d["other_ranges"])
            out.append(d)
        return out

    # -- schedules ---------------------------------------------------------
    def create_schedule(self, **fields: Any) -> dict:
        now = time.time()
        fields = {"created_at": now, "updated_at": now, **fields}
        cols, qs = ",".join(fields), ",".join("?" for _ in fields)
        with self.conn() as c:
            sid = c.execute(f"INSERT INTO schedules ({cols}) VALUES ({qs})",
                            list(fields.values())).lastrowid
        return self.get_schedule(sid)

    def get_schedule(self, sid: int) -> dict | None:
        r = self.conn().execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def list_schedules(self, due_before: float | None = None) -> list[dict]:
        if due_before is None:
            rows = self.conn().execute("SELECT * FROM schedules ORDER BY id").fetchall()
        else:
            rows = self.conn().execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_fire_at IS NOT NULL "
                "AND next_fire_at <= ? ORDER BY next_fire_at", (due_before,)).fetchall()
        return [dict(r) for r in rows]

    def update_schedule(self, sid: int, **fields: Any) -> None:
        fields = {**fields, "updated_at": time.time()}
        sets = ",".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE schedules SET {sets} WHERE id=?", [*fields.values(), sid])

    def claim_schedule(self, sid: int, expected_next: float, new_next: float | None,
                       **fields: Any) -> bool:
        """Advance a due schedule to its next time, only if nobody else has.

        True means this caller owns this fire. False means it was already
        claimed (or edited) and must not fire.
        """
        fields = {**fields, "next_fire_at": new_next, "updated_at": time.time()}
        sets = ",".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            cur = c.execute(f"UPDATE schedules SET {sets} WHERE id=? AND next_fire_at=?",
                            [*fields.values(), sid, expected_next])
            return cur.rowcount == 1

    def delete_schedule(self, sid: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM schedules WHERE id=?", (sid,))

    # -- audit -------------------------------------------------------------
    def audit(self, actor: str, action: str, run_id: int | None = None,
              detail: Any = None) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO audit (ts, actor, run_id, action, detail) VALUES (?,?,?,?,?)",
                      (time.time(), actor, run_id, action,
                       json.dumps(detail) if detail is not None else None))

    def audit_log(self, run_id: int | None = None, limit: int = 200) -> list[dict]:
        if run_id is None:
            rows = self.conn().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?",
                                       (limit,)).fetchall()
        else:
            rows = self.conn().execute("SELECT * FROM audit WHERE run_id=? ORDER BY id DESC "
                                       "LIMIT ?", (run_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("detail"):
                d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    # -- events ------------------------------------------------------------
    @staticmethod
    def _event(c, run_id, attempt, kind, message, data=None):
        c.execute(
            "INSERT INTO events (run_id, ts, attempt, kind, message, data) VALUES (?,?,?,?,?,?)",
            (run_id, time.time(), attempt, kind, message,
             json.dumps(data) if data is not None else None))

    def add_event(self, run_id: int, kind: str, message: str, data: Any = None,
                  attempt: int | None = None) -> None:
        with self.conn() as c:
            if attempt is None:
                r = c.execute("SELECT attempt FROM runs WHERE id=?", (run_id,)).fetchone()
                attempt = r["attempt"] if r else 0
            self._event(c, run_id, attempt, kind, message, data)

    def ingest(self, run_id: int, attempt: int, new_offset: int,
               events: list[tuple[str, str, Any]], **fields: Any) -> bool:
        """Record parsed stream events and advance the stream offset atomically.

        Only applies if the run is still on ``attempt`` (guards against a stale
        reader after a restart).
        """
        fields = self._enc({**fields, "stream_offset": new_offset, "updated_at": time.time()})
        sets = ",".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            cur = c.execute(f"UPDATE runs SET {sets} WHERE id=? AND attempt=?",
                            [*fields.values(), run_id, attempt])
            if cur.rowcount != 1:
                return False
            for kind, message, data in events:
                self._event(c, run_id, attempt, kind, message, data)
            if "stalled" in fields:
                self._refresh_attention(c, run_id)
        return True

    def events(self, run_id: int, limit: int = 300, kinds: Iterable[str] | None = None) -> list[dict]:
        if kinds:
            k = list(kinds)
            q = (f"SELECT * FROM events WHERE run_id=? AND kind IN ({','.join('?' * len(k))}) "
                 f"ORDER BY id DESC LIMIT ?")
            rows = self.conn().execute(q, [run_id, *k, limit]).fetchall()
        else:
            rows = self.conn().execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, limit)).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            if d.get("data"):
                d["data"] = json.loads(d["data"])
            out.append(d)
        return out


class _Tx:
    """``with store.conn() as c:`` runs the block in one IMMEDIATE transaction.

    Outside a ``with`` it behaves like the bare connection (autocommit reads).
    """

    def __init__(self, c: sqlite3.Connection):
        self._c = c

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __enter__(self):
        self._c.execute("BEGIN IMMEDIATE")
        return self._c

    def __exit__(self, et, ev, tb):
        if et is None:
            self._c.execute("COMMIT")
        else:
            self._c.execute("ROLLBACK")
        return False
