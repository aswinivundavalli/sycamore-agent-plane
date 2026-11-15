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
"""

JSON_FIELDS = {"changed_files", "diff_stat", "merge_check"}


class InvalidTransition(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)

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
        return True

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
