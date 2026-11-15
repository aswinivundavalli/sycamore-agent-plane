#!/usr/bin/env python3
"""Load test: one coordinator, several workers, a thousand runs.

    python3 scripts/loadtest.py --runs 1000 --workers 4 --capacity 16

Everything is real (processes, HTTP, git, the detached runner) except the
agent, which is the scripted fake agent so the test costs nothing. Midway it
kills one worker outright (its runs must become interrupted, then are
restarted elsewhere) and kills the coordinator with SIGKILL (workers must
ride it out). At the end it checks the invariants that matter at scale:

  * every run reached a terminal-for-review state (finished),
  * no attempt ever ran twice: each (run, attempt) has exactly one runner
    directory across all workers,
  * no agent output was lost or duplicated: the tool calls and results in
    the database equal those in the raw transcripts on the workers,
  * the board stayed fast: p50/p95 latency of the needs-you page while
    under load.

Results are printed and written as JSON (--out) for docs/scale-test.md.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from conductor.agents import stream as streamparse  # noqa: E402


def http(method: str, url: str, body: dict | None = None, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--capacity", type=int, default=16)
    ap.add_argument("--burst", type=int, default=100, help="runs per batch request")
    ap.add_argument("--port", type=int, default=7795)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--no-chaos", action="store_true", help="skip the worker and coordinator kills")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    work = Path(args.workdir or tempfile.mkdtemp(prefix="conductor-load-"))
    work.mkdir(parents=True, exist_ok=True)
    repo, home = work / "repo", work / "coordinator"
    if not repo.exists():
        subprocess.run([str(ROOT / "scripts" / "make-demo-repo.sh"), str(repo)], check=True,
                       capture_output=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "FAKE_AGENT_STEP_SECS": "0.05",
           "FAKE_AGENT_STATE_DIR": str(work / "fake-state"), "CONDUCTOR_LEASE_SECS": "10"}
    base = f"http://127.0.0.1:{args.port}"
    logs = work / "logs"
    logs.mkdir(exist_ok=True)

    def start_coordinator():
        return subprocess.Popen([sys.executable, "-m", "conductor", "--port", str(args.port),
                                 "--home", str(home), "--max-parallel", "0"], env=env,
                                stdout=subprocess.DEVNULL,
                                stderr=open(logs / "coordinator.log", "ab"))

    def wait_up():
        for _ in range(100):
            try:
                return http("GET", base + "/api/stats")
            except OSError:
                time.sleep(0.1)
        raise SystemExit("coordinator did not start")

    coord = start_coordinator()
    wait_up()
    token = (home / "worker_token").read_text().strip()

    def start_worker(i: int):
        return subprocess.Popen(
            [sys.executable, "-m", "conductor.worker", "--coordinator", base, "--token", token,
             "--capacity", str(args.capacity), "--name", f"load-{i}",
             "--workdir", str(work / f"worker-{i}")], env=env,
            stdout=subprocess.DEVNULL, stderr=open(logs / f"worker-{i}.log", "ab"))

    workers = {i: start_worker(i) for i in range(args.workers)}

    # -- board latency sampler ------------------------------------------------
    latencies: list[float] = []
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            t = time.perf_counter()
            try:
                http("GET", base + "/api/runs?view=needs_you&limit=50", timeout=10)
                latencies.append((time.perf_counter() - t) * 1000)
            except OSError:
                pass  # coordinator restarting
            stop.wait(0.25)

    threading.Thread(target=sample, daemon=True).start()

    # -- submit --------------------------------------------------------------
    t0 = time.time()
    groups = []
    for start in range(0, args.runs, args.burst):
        n = min(args.burst, args.runs - start)
        tasks = [f"Load task {start + k} [[fake:write load/f{start + k}.txt]]{start + k}\n"
                 f"[[/fake]]" for k in range(n)]
        out = http("POST", base + "/api/batches",
                   {"repo_path": str(repo), "title": f"Load {start}-{start + n - 1}",
                    "tasks": tasks, "agent": "fake", "test_cmd": ""}, timeout=300)
        groups.append(out["group_key"])
    submitted = time.time() - t0
    print(f"submitted {args.runs} runs in {submitted:.1f}s", flush=True)

    def counts():
        return http("GET", base + "/api/stats", timeout=30)["counts"]

    chaos = {"worker_killed": None, "coordinator_killed": False, "restarted_interrupted": 0}
    done_at = None
    last_print = 0.0
    while True:
        try:
            c = counts()
        except OSError:
            time.sleep(0.5)
            continue
        finished = c.get("finished", 0)
        if time.time() - last_print > 5:
            print(f"t={time.time() - t0:6.1f}s  {json.dumps(c, sort_keys=True)}", flush=True)
            last_print = time.time()
        if not args.no_chaos:
            if chaos["worker_killed"] is None and finished >= args.runs * 0.3:
                victim = 0
                workers[victim].kill()  # the worker vanishes; its agents are orphaned
                chaos["worker_killed"] = {"worker": victim, "at": round(time.time() - t0, 1)}
                print(f"chaos: killed worker {victim}", flush=True)
            if chaos["worker_killed"] and not chaos["coordinator_killed"] and \
                    finished >= args.runs * 0.5:
                coord.send_signal(signal.SIGKILL)
                coord.wait()
                time.sleep(2)
                coord = start_coordinator()
                wait_up()
                chaos["coordinator_killed"] = round(time.time() - t0, 1)
                print("chaos: killed and restarted the coordinator", flush=True)
            if c.get("interrupted"):
                # an engineer clicking "Restart interrupted" on each group
                for g in groups:
                    r = http("POST", base + f"/api/groups/{g}/restart_interrupted", {},
                             timeout=120)
                    chaos["restarted_interrupted"] += len(r["done"])
        active = sum(c.get(s, 0) for s in ("queued", "starting", "running", "testing"))
        if not active and not c.get("interrupted") and finished + c.get("failed", 0) >= args.runs:
            done_at = time.time() - t0
            break
        if time.time() - t0 > 3600:
            print("timeout", flush=True)
            break
        time.sleep(1)
    stop.set()

    # -- invariants ------------------------------------------------------------
    final = counts()
    import sqlite3
    db = sqlite3.connect(home / "conductor.db")
    runs = db.execute("SELECT id, attempt, state FROM runs").fetchall()
    # (run, attempt) -> worker dirs that hold its runner directory
    owners: dict[tuple[int, int], list[str]] = {}
    for wd in work.glob("worker-*"):
        for d in (wd / "runs").glob("run-*/attempt-*"):
            key = (int(d.parent.name[4:]), int(d.name[8:]))
            owners.setdefault(key, []).append(wd.name)
    double = {k: v for k, v in owners.items() if len(v) > 1}
    # transcripts vs database: tool calls and results, for each run's final
    # attempt. (An attempt on a worker that was killed keeps writing after the
    # coordinator has, correctly, marked it interrupted and stopped listening.)
    final_attempt = {rid: att for rid, att, _state in runs}
    mismatched = []
    for (rid, att), wds in owners.items():
        if final_attempt.get(rid) != att:
            continue
        stream = work / wds[0] / "runs" / f"run-{rid}" / f"attempt-{att}" / "stream.jsonl"
        if not stream.exists():
            continue
        want = 0
        for line in stream.read_text(errors="replace").splitlines():
            want += sum(1 for k, _m, _d in streamparse.parse_line(line).events
                        if k in ("tool", "result"))
        got = db.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND attempt=? AND kind IN "
                         "('tool','result')", (rid, att)).fetchone()[0]
        if want != got:
            mismatched.append({"run": rid, "attempt": att, "transcript": want, "db": got})
    lat = sorted(latencies)
    result = {
        "runs": args.runs, "workers": args.workers, "capacity_per_worker": args.capacity,
        "concurrency": args.workers * args.capacity,
        "submit_seconds": round(submitted, 1),
        "drain_seconds": round(done_at, 1) if done_at else None,
        "runs_per_minute": round(args.runs / done_at * 60, 1) if done_at else None,
        "final_counts": final,
        "attempts_checked": len(owners),
        "transcripts_compared": sum(1 for (r, a) in owners if final_attempt.get(r) == a),
        "double_starts": len(double),
        "event_mismatches": len(mismatched),
        "event_mismatch_examples": mismatched[:5],
        "board_latency_ms": {"samples": len(lat),
                             "p50": round(statistics.median(lat), 1) if lat else None,
                             "p95": round(lat[int(len(lat) * 0.95) - 1], 1) if lat else None,
                             "max": round(lat[-1], 1) if lat else None},
        "chaos": chaos,
        "workdir": str(work),
    }
    print(json.dumps(result, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
    for p in [*workers.values(), coord]:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
    ok = done_at and not double and not mismatched and final.get("finished", 0) == args.runs
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
