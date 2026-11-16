"""Remote workers end to end: an in-process coordinator (supervisor thread +
real HTTP server) and real `python -m conductor.worker` processes, using the
scripted agent against a real git repository."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path

from conductor import fleet, gitops, states
from conductor.config import Config
from conductor.db import Store
from conductor.server import serve
from conductor.service import Service
from conductor.supervisor import Supervisor

ROOT = Path(__file__).resolve().parent.parent


class WorkersTest(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        subprocess.run([str(ROOT / "scripts" / "make-demo-repo.sh"), str(self.repo)],
                       check=True, capture_output=True)
        self.env = {**os.environ, "FAKE_AGENT_STEP_SECS": "0.05",
                    "FAKE_AGENT_STATE_DIR": str(self.tmp / "fake-state"),
                    "PYTHONPATH": str(ROOT)}
        os.environ["FAKE_AGENT_STATE_DIR"] = self.env["FAKE_AGENT_STATE_DIR"]
        self._lease = fleet.LEASE_SECS
        fleet.LEASE_SECS = 3.0
        self.cfg = Config()
        self.cfg.home = self.tmp / "home"
        self.cfg.tick_secs = 0.1
        self.cfg.max_parallel = 0  # the coordinator runs nothing itself
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        self.sup = Supervisor(self.cfg, self.store)
        self.svc = Service(self.cfg, self.store, self.sup)
        self.httpd = serve(self.svc, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.sup.start()
        self.workers: list[subprocess.Popen] = []

    def tearDown(self):
        for w in self.workers:
            if w.poll() is None:
                w.kill()
                w.wait()
        out = subprocess.run(["pgrep", "-f", f"conductor.runner {self.tmp}"],
                             capture_output=True, text=True).stdout
        for pid in out.split():
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.sup.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        fleet.LEASE_SECS = self._lease
        os.environ.pop("FAKE_AGENT_STATE_DIR", None)
        self._tmp.cleanup()

    def start_worker(self, name="w1", capacity=2):
        p = subprocess.Popen(
            [sys.executable, "-m", "conductor.worker", "--coordinator", self.url,
             "--token", self.svc.worker_token, "--capacity", str(capacity), "--name", name,
             "--workdir", str(self.tmp / f"worker-{name}")],
            env=self.env, stdout=subprocess.DEVNULL, stderr=open(self.tmp / f"{name}.log", "ab"))
        self.workers.append(p)
        return p

    def create(self, task, **kw):
        return self.svc.create_run({"repo_path": str(self.repo), "task": task,
                                    "agent": "fake", **kw})["id"]

    def wait_for(self, rid, wanted, timeout=30):
        wanted = {wanted} if isinstance(wanted, str) else set(wanted)
        end = time.time() + timeout
        while time.time() < end:
            run = self.store.get(rid)
            if run["state"] in wanted:
                return run
            time.sleep(0.1)
        logs = "".join(p.read_text()[-2000:] for p in self.tmp.glob("*.log"))
        self.fail(f"run {rid} is {self.store.get(rid)['state']}, wanted {wanted}\n{logs}")

    # ---------------------------------------------------------------- tests
    def test_remote_run_end_to_end(self):
        self.start_worker()
        rid = self.create("Add [[fake:write remote.txt]]from a worker\n[[/fake]]")
        run = self.wait_for(rid, states.FINISHED)
        self.assertTrue(run["worker_id"].startswith("w-"))
        self.assertEqual(run["test_status"], "passed")
        self.assertEqual(run["diff_stat"]["files"], 1)
        self.assertEqual(run["changed_files"], ["remote.txt"])
        self.assertTrue(any("Agent started on" in e["message"] for e in self.store.events(rid)))
        # the raw transcript is kept on the coordinator too (harvesting)
        log = self.svc.log(rid)
        self.assertTrue(any('"type": "result"' in ln or '"type":"result"' in ln
                            for ln in log["lines"]))
        self.svc.accept(rid, "merge")
        self.assertEqual(gitops.run_git(self.repo, "show", "main:remote.txt"), "from a worker")
        # the worker is told to clean up its worktree
        jobfile = self.tmp / "worker-w1" / "jobs" / f"run-{rid}.json"
        end = time.time() + 15
        while jobfile.exists() and time.time() < end:
            time.sleep(0.2)
        self.assertFalse(jobfile.exists())

    def test_question_and_answer_resume_on_a_worker(self):
        self.start_worker()
        rid = self.create("Choose [[fake:ask]] [[fake:write c.txt]]c\n[[/fake]]")
        run = self.wait_for(rid, states.WAITING_INPUT)
        session = run["session_id"]
        self.svc.answer(rid, "A")
        run = self.wait_for(rid, states.FINISHED)
        self.assertEqual(run["session_id"], session)
        self.assertEqual(run["attempt"], 2)

    def test_policy_kill_reaches_the_remote_agent(self):
        self.start_worker()
        rid = self.create("Ship [[fake:bash git push origin main]] [[fake:sleep 30]]")
        run = self.wait_for(rid, states.FAILED, timeout=40)
        self.assertIn("Stopped by policy", run["error"])

    def test_shell_write_to_a_forbidden_path_is_stopped_on_a_worker(self):
        # The worktree is on the worker; its heartbeat reports the changed
        # files, and the coordinator applies the path rules to them.
        self.start_worker()
        rid = self.create("Loosen [[fake:sh]]\nmkdir -p .conductor && echo x > "
                          ".conductor/policy.toml\n[[/fake]] [[fake:sh]]\nsleep 40\n[[/fake]]")
        run = self.wait_for(rid, states.FAILED, timeout=35)
        self.assertIn("Stopped by policy: edits .conductor/policy.toml", run["error"])

    def test_parallel_runs_spread_across_two_workers(self):
        self.start_worker("a", capacity=1)
        self.start_worker("b", capacity=1)
        ids = [self.create(f"t{i} [[fake:write f{i}.txt]]{i}\n[[/fake]] [[fake:sleep 1]]")
               for i in range(4)]
        for rid in ids:
            self.wait_for(rid, states.FINISHED, timeout=60)
        used = {self.store.get(r)["worker_id"] for r in ids}
        self.assertEqual(len(used), 2)

    def test_lost_worker_interrupts_its_runs(self):
        w = self.start_worker()
        rid = self.create("Long [[fake:sleep 60]]")
        self.wait_for(rid, states.RUNNING)
        w.kill()  # the whole worker vanishes (its agent keeps running, unseen)
        run = self.wait_for(rid, states.INTERRUPTED, timeout=20)
        self.assertIn("stopped responding", [e["message"] for e in self.store.events(rid)
                                             if e["kind"] == "state"][-1])
        lost = [x for x in self.store.workers() if x["state"] == "lost"]
        self.assertEqual(len(lost), 1)

    def test_worker_restart_reattaches_to_its_agent(self):
        w = self.start_worker()
        rid = self.create("Slow [[fake:sleep 3]] [[fake:write s.txt]]s\n[[/fake]]")
        self.wait_for(rid, states.RUNNING)
        w.send_signal(signal.SIGTERM)  # graceful: agents keep running
        w.wait(timeout=10)
        self.start_worker()  # same workdir, same worker id
        run = self.wait_for(rid, states.FINISHED, timeout=40)
        self.assertEqual(run["attempt"], 1)  # re-attached, never relaunched

    def test_write_sandbox_holds_on_a_remote_worker(self):
        from conductor import sandbox
        if not sandbox.available()[0]:
            self.skipTest("no OS sandbox on this machine")
        (self.repo / ".conductor").mkdir()
        (self.repo / ".conductor" / "policy.toml").write_text('[sandbox]\nwrites = "worktree"\n')
        gitops.run_git(self.repo, "add", "-A")
        gitops.run_git(self.repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q",
                       "-m", "policy")
        outside = Path(tempfile.mkdtemp(prefix="conductor-w-sbx-", dir=Path.home()))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, True))
        self.start_worker()
        rid = self.create(f"Escape [[fake:sh]]\necho x >> {outside}/bashrc\n[[/fake]]")
        run = self.wait_for(rid, states.FINISHED)
        self.assertFalse((outside / "bashrc").exists())
        self.assertIn("write_outside_worktree", [v["rule"] for v in run["violations"]])
        self.assertTrue(any("sandboxed on" in e["message"] for e in self.store.events(rid)))

    def test_events_are_idempotent_and_auth_is_required(self):
        reg = self.sup.fleet.register({"name": "manual", "capacity": 1})
        wid = reg["worker_id"]
        rid = self.create("x")
        spec = self.sup.fleet.lease(wid, {"free": 1})["runs"][0]
        self.assertEqual(spec["run_id"], rid)
        self.sup.fleet.started(wid, rid, {"attempt": 1, "session_id": "s"})
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"}]}}) + "\n"
        a = self.sup.fleet.events(wid, rid, {"attempt": 1, "offset": 0, "data": line})
        b = self.sup.fleet.events(wid, rid, {"attempt": 1, "offset": 0, "data": line})
        self.assertEqual(a["offset"], b["offset"])
        texts = [e for e in self.store.events(rid) if e["kind"] == "text"]
        self.assertEqual(len(texts), 1)
        with self.assertRaises(fleet.FleetError):  # a gap is refused
            self.sup.fleet.events(wid, rid, {"attempt": 1, "offset": 10_000, "data": line})
        with self.assertRaises(fleet.FleetError):  # a stale attempt is refused
            self.sup.fleet.events(wid, rid, {"attempt": 7, "offset": 0, "data": line})
        req = urllib.request.Request(self.url + f"/api/workers/{wid}/lease", data=b"{}",
                                     method="POST", headers={"content-type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
