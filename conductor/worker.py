"""A Conductor worker: runs agents for a coordinator, on any machine.

    python3 -m conductor.worker --coordinator http://host:7700 --capacity 16

The worker leases queued runs from the coordinator, prepares a worktree from
its own cache clone of the repository, runs the agent under the same detached
runner as a local run, streams the agent's raw output back, pushes the run's
branch into the repository, and runs the tests when the coordinator asks.
All decisions (state, policy, attention, merges) stay with the coordinator;
see ``conductor/fleet.py`` for the protocol.

Crash safety mirrors the local design:
  * The agent never depends on the worker process: it runs under
    ``conductor.runner`` in its own process group, writing to a file.
  * Each job's progress (phase, bytes of the stream already accepted by the
    coordinator) is saved in ``<workdir>/jobs/run-<id>.json``. A restarted
    worker re-attaches to agents that are still running, finishes those that
    exited, and reports the rest as lost (the coordinator marks them
    interrupted; nothing is relaunched on its own).
  * Every call is idempotent on the coordinator side, so retrying after a
    network error or a coordinator restart is always safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import gitops
from .agents.registry import get_adapter
from .supervisor import _kill_group, _pkg_root, _read_json, _runner_alive, _run_test_cmd, \
    _tail, conflict_prompt

log = logging.getLogger("conductor.worker")

HEARTBEAT_SECS = 5.0
POST_CHUNK = 256 * 1024


class Rejected(Exception):
    """The coordinator refused a call (stale attempt, wrong state): stop this job."""

    def __init__(self, status: int, body: dict):
        super().__init__(f"{status}: {body.get('error')}")
        self.status, self.body = status, body


class Client:
    """JSON over HTTP with a bearer token. Network errors and 5xx are retried
    with backoff (the coordinator may be restarting); 4xx are raised."""

    def __init__(self, base: str, token: str, stop: threading.Event):
        self.base, self.token, self.stop = base.rstrip("/"), token, stop

    def post(self, path: str, body: dict, retry: bool = True) -> dict:
        delay = 0.5
        while True:
            req = urllib.request.Request(
                self.base + path, data=json.dumps(body).encode(), method="POST",
                headers={"content-type": "application/json",
                         "authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    data = json.load(e)
                except ValueError:
                    data = {"error": str(e)}
                if e.code < 500:
                    raise Rejected(e.code, data)
                err = data
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                err = e
            if not retry or self.stop.is_set():
                raise ConnectionError(str(err))
            log.warning("coordinator unreachable (%s); retrying in %.1fs", err, delay)
            self.stop.wait(delay)
            delay = min(delay * 2, 10)


class Worker:
    def __init__(self, coordinator: str, token: str, workdir: Path, capacity: int,
                 name: str, labels: list[str]):
        self.workdir = workdir
        self.capacity = capacity
        self.name = name
        self.labels = labels
        self.stop = threading.Event()
        self.client = Client(coordinator, token, self.stop)
        self.jobs: dict[int, "Job"] = {}
        self.jobs_lock = threading.Lock()
        self.cache_locks: dict[str, threading.Lock] = {}
        for d in ("jobs", "cache", "worktrees", "runs"):
            (workdir / d).mkdir(parents=True, exist_ok=True)
        ident = _read_json(workdir / "worker.json") or {}
        self.worker_id = ident.get("worker_id")

    # ----------------------------------------------------------- lifecycle
    def run(self) -> None:
        reg = self.client.post("/api/workers/register", {
            "worker_id": self.worker_id, "name": self.name, "capacity": self.capacity,
            "labels": self.labels})
        self.worker_id = reg["worker_id"]
        (self.workdir / "worker.json").write_text(json.dumps({"worker_id": self.worker_id}))
        log.info("worker %s registered with capacity %d", self.worker_id, self.capacity)
        self._recover()
        last_beat = 0.0
        while not self.stop.is_set():
            try:
                if time.time() - last_beat >= HEARTBEAT_SECS:
                    self._heartbeat()
                    last_beat = time.time()
                self._lease()
            except Rejected as e:
                if e.status == 404:  # coordinator forgot us (new database): register again
                    self.client.post("/api/workers/register", {
                        "worker_id": self.worker_id, "name": self.name,
                        "capacity": self.capacity, "labels": self.labels})
                else:
                    log.warning("coordinator refused: %s", e)
            except ConnectionError:
                pass
            self.stop.wait(1.0)

    def shutdown(self, *_):
        log.info("worker stopping (agents keep running; they are re-attached on restart)")
        self.stop.set()

    def _active(self) -> list["Job"]:
        with self.jobs_lock:
            return [j for j in self.jobs.values() if not j.idle]

    def _lease(self) -> None:
        free = self.capacity - len(self._active())
        if free <= 0:
            return
        out = self.client.post(f"/api/workers/{self.worker_id}/lease",
                               {"free": free, "running": self.capacity - free}, retry=False)
        for spec in out.get("runs", []):
            job = Job(self, spec)
            job.save()
            with self.jobs_lock:
                self.jobs[spec["run_id"]] = job
            job.start()

    def _heartbeat(self) -> None:
        with self.jobs_lock:
            jobs = list(self.jobs.values())
        ranges = {}
        for j in jobs:
            if j.phase == "running" and j.worktree and Path(j.worktree).exists():
                try:
                    ranges[str(j.run_id)] = {"base": j.spec["base_commit"],
                                             "files": gitops.changed_ranges(
                                                 j.worktree, j.spec["base_commit"])}
                except gitops.GitError:
                    pass
        out = self.client.post(f"/api/workers/{self.worker_id}/heartbeat",
                               {"running": [j.run_id for j in jobs], "ranges": ranges},
                               retry=False)
        for cmd in out.get("commands", []):
            job = self.jobs.get(int(cmd["run_id"]))
            if cmd["op"] == "kill" and job:
                job.kill()
            elif cmd["op"] == "cleanup":
                if job:
                    job.cleanup()
                with self.jobs_lock:
                    self.jobs.pop(int(cmd["run_id"]), None)

    def _recover(self) -> None:
        for f in sorted((self.workdir / "jobs").glob("run-*.json")):
            st = _read_json(f)
            if not st:
                continue
            job = Job(self, st["spec"], st)
            with self.jobs_lock:
                self.jobs[job.run_id] = job
            if job.idle:
                continue
            log.info("recovering run %s (phase %s)", job.run_id, job.phase)
            job.start(recovering=True)

    def cache_for(self, remote: str) -> Path:
        key = hashlib.sha1(remote.encode()).hexdigest()[:16]
        lock = self.cache_locks.setdefault(key, threading.Lock())
        path = self.workdir / "cache" / key
        with lock:
            if not (path / ".git").exists() and not (path / "HEAD").exists():
                subprocess.run(["git", "clone", "--quiet", "--no-checkout", remote, str(path)],
                               check=True, capture_output=True, timeout=600)
            gitops.run_git(path, "fetch", "--quiet", "--prune", "origin",
                           "+refs/heads/*:refs/remotes/origin/*", timeout=600)
        return path


class Job(threading.Thread):
    """One leased attempt of one run, start to finish."""

    def __init__(self, worker: Worker, spec: dict, state: dict | None = None):
        super().__init__(name=f"job-{spec['run_id']}", daemon=True)
        self.w = worker
        self.spec = spec
        self.run_id = spec["run_id"]
        self.attempt = spec["attempt"]
        st = state or {}
        self.phase = st.get("phase", "prepare")
        self.offset = st.get("offset", 0)
        self.worktree = st.get("worktree")
        self.cache = st.get("cache")
        self.attempt_dir = Path(st.get("attempt_dir") or worker.workdir / "runs" /
                                f"run-{self.run_id}" / f"attempt-{self.attempt}")
        self.runner_pid = st.get("runner_pid")
        self.recovering = False

    @property
    def idle(self) -> bool:
        return self.phase in ("done", "stopped")

    def save(self) -> None:
        state = {"spec": self.spec, "phase": self.phase, "offset": self.offset,
                 "worktree": self.worktree, "cache": self.cache,
                 "attempt_dir": str(self.attempt_dir), "runner_pid": self.runner_pid}
        path = self.w.workdir / "jobs" / f"run-{self.run_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)

    def start(self, recovering: bool = False) -> None:
        self.recovering = recovering
        super().start()

    def call(self, op: str, body: dict) -> dict:
        return self.w.client.post(
            f"/api/workers/{self.w.worker_id}/runs/{self.run_id}/{op}",
            {"attempt": self.attempt, **body})

    # ----------------------------------------------------------------- run
    def run(self) -> None:
        try:
            if self.recovering and self.phase == "running" and not self._agent_present():
                self.call("lost", {})
                self._set("stopped")
                return
            if self.phase == "prepare":
                self._prepare_and_launch()
            if self.phase == "running":
                self._follow()
            if self.phase == "exited":
                self._report_exit()
            if self.phase == "testing":
                self._test()
        except Rejected as e:
            log.info("run %s: coordinator says %s; stopping this job", self.run_id, e)
            self._set("stopped")
        except ConnectionError:
            log.warning("run %s: gave up reaching the coordinator", self.run_id)
        except Exception as e:  # git failure, agent missing, ...
            log.exception("run %s failed on this worker", self.run_id)
            try:
                self.call("failed", {"error": str(e)})
            except Exception:  # noqa: BLE001
                pass
            self._set("stopped")

    def _set(self, phase: str) -> None:
        self.phase = phase
        self.save()

    def _agent_present(self) -> bool:
        if (self.attempt_dir / "exit.json").exists():
            return True
        return bool(self.runner_pid) and _runner_alive(self.runner_pid, self.attempt_dir)

    def _prepare_and_launch(self) -> None:
        s = self.spec
        cache = self.w.cache_for(s["repo_remote"])
        self.cache = str(cache)
        wt = self.w.workdir / "worktrees" / f"run-{self.run_id}"
        if not (wt / ".git").exists():
            gitops.run_git(cache, "worktree", "prune")
            has_branch = gitops.run_git(cache, "rev-parse", "--verify", "--quiet",
                                        f"refs/remotes/origin/{s['branch']}", check=False)
            start = f"origin/{s['branch']}" if has_branch.strip() else s["base_commit"]
            gitops.run_git(cache, "worktree", "add", "--quiet", "-B", s["branch"], str(wt), start)
        self.worktree = str(wt)
        prompt = s["prompt"]
        started = {}
        if s.get("pre_attempt") == "merge_target":
            res = gitops.merge_target_into_worktree(wt, f"origin/{s['base_branch']}")
            if res["clean"]:
                self._push()
                out = self.call("started", {"merged_clean": True})
                self._set("testing")
                self.test_cmd = out.get("test_cmd", "")
                return
            prompt = conflict_prompt(s, res["conflicts"])
            started["conflicts"] = res["conflicts"]
        resume = bool(s.get("resume") and s.get("session_id"))
        session_id = s["session_id"] if resume else str(uuid.uuid4())
        adapter = get_adapter(s["agent"])
        ok, why = adapter.available()
        if not ok:
            raise RuntimeError(why)
        spec = adapter.build(prompt=prompt, session_id=session_id, resume=resume,
                             model=s.get("model"), budget_usd=s.get("budget_usd"),
                             worktree=str(wt), disallowed_tools=s.get("disallowed_tools"))
        d = self.attempt_dir
        d.mkdir(parents=True, exist_ok=True)
        for f in ("stream.jsonl", "exit.json", "runner.json"):
            (d / f).unlink(missing_ok=True)
        (d / "prompt.txt").write_text(prompt)
        env = {**os.environ, **spec.env, "CONDUCTOR_RUN_ID": str(self.run_id),
               "PYTHONPATH": _pkg_root()}
        env.pop("CLAUDECODE", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "conductor.runner", str(d), str(wt), "--", *spec.argv],
            cwd=str(wt), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        self.runner_pid = proc.pid
        self._proc = proc  # reaped when it exits
        self._set("running")
        self.call("started", {"session_id": session_id, "resume": resume, "prompt": prompt,
                              "worktree": str(wt), **started})

    def _follow(self) -> None:
        stream = self.attempt_dir / "stream.jsonl"
        deadline = time.time() + float(self.spec.get("max_runtime_secs") or 3600)
        while not self.w.stop.is_set():
            proc = getattr(self, "_proc", None)
            if proc is not None:
                proc.poll()  # reap the runner once it exits (no zombies)
            self._send_new_lines(stream)
            exited = (self.attempt_dir / "exit.json").exists()
            if exited:
                self._send_new_lines(stream)
                self._set("exited")
                return
            if not _runner_alive(self.runner_pid or 0, self.attempt_dir):
                self.call("lost", {})
                self._set("stopped")
                return
            if time.time() > deadline:
                log.warning("run %s exceeded its run time; stopping the agent", self.run_id)
                self.kill()
                deadline = float("inf")
            time.sleep(0.3)

    def _send_new_lines(self, stream: Path) -> None:
        try:
            with open(stream, "rb") as f:
                f.seek(self.offset)
                data = f.read(POST_CHUNK)
        except FileNotFoundError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return
        data = data[:end + 1]
        try:
            out = self.call("events", {"offset": self.offset,
                                       "data": data.decode("utf-8", "replace")})
        except Rejected as e:
            if "offset" in e.body:  # the coordinator has less than we thought: resend
                self.offset = int(e.body["offset"])
                self.save()
                return
            raise
        self.offset = int(out.get("offset", self.offset + len(data)))
        self.save()

    def _push(self) -> None:
        gitops.run_git(self.worktree, "push", "--quiet", "--force", "origin",
                       f"HEAD:refs/heads/{self.spec['branch']}", timeout=600)

    def _report_exit(self) -> None:
        exit_info = _read_json(self.attempt_dir / "exit.json") or {"code": None}
        wt = self.worktree
        if gitops.merge_in_progress(wt):
            msg = (f"conductor: run {self.run_id}: merge {self.spec['base_branch']} "
                   "(conflicts resolved by the agent)")
        else:
            msg = f"conductor: run {self.run_id}: {self.spec.get('title') or 'agent work'}"
        gitops.commit_all(wt, msg)
        self._push()
        out = self.call("finish", {"code": exit_info.get("code"),
                                   "stderr_tail": _tail(self.attempt_dir / "stderr.log", 600)})
        if out.get("state") == "testing":
            self.test_cmd = out.get("test_cmd", "")
            self._set("testing")
        else:
            self._set("done")

    def _test(self) -> None:
        cmd = getattr(self, "test_cmd", None)
        if cmd is None:  # recovering in the testing phase: ask again by re-finishing
            cmd = gitops.detect_test_cmd(self.worktree) or ""
        if cmd.strip():
            status, output = _run_test_cmd(cmd, self.worktree, 600)
            gitops.run_git(self.worktree, "checkout", "--", ".", check=False)
            gitops.run_git(self.worktree, "clean", "-fdq", check=False)
        else:
            status, output = "none", "No test command configured for this repository."
        self.call("tests", {"status": status, "output": output[-20000:], "test_cmd": cmd})
        self._set("done")

    # ------------------------------------------------------------ commands
    def kill(self) -> None:
        if self.runner_pid and _runner_alive(self.runner_pid, self.attempt_dir):
            _kill_group(self.runner_pid)

    def cleanup(self) -> None:
        self.kill()
        if self.worktree and self.cache:
            gitops.run_git(self.cache, "worktree", "remove", "--force", self.worktree,
                           check=False)
            gitops.run_git(self.cache, "worktree", "prune", check=False)
        import shutil
        shutil.rmtree(self.w.workdir / "runs" / f"run-{self.run_id}", ignore_errors=True)
        (self.w.workdir / "jobs" / f"run-{self.run_id}.json").unlink(missing_ok=True)
        self.phase = "done"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="conductor.worker", description=__doc__.splitlines()[0])
    ap.add_argument("--coordinator", default=os.environ.get("CONDUCTOR_URL",
                                                           "http://127.0.0.1:7700"))
    ap.add_argument("--token", default=os.environ.get("CONDUCTOR_WORKER_TOKEN"))
    ap.add_argument("--token-file", help="read the token from this file "
                    "(e.g. the coordinator's CONDUCTOR_HOME/worker_token)")
    ap.add_argument("--capacity", type=int, default=4)
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--labels", default="")
    ap.add_argument("--workdir", default=os.path.expanduser("~/.conductor-worker"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    token = args.token or (Path(args.token_file).read_text().strip() if args.token_file
                           else None)
    if not token:
        print("a worker token is required (--token, --token-file or CONDUCTOR_WORKER_TOKEN)",
              file=sys.stderr)
        return 2
    w = Worker(args.coordinator, token, Path(args.workdir).expanduser(), args.capacity,
               args.name, [x for x in args.labels.split(",") if x])
    signal.signal(signal.SIGTERM, w.shutdown)
    signal.signal(signal.SIGINT, w.shutdown)
    w.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
