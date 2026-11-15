"""A scripted stand-in for Claude Code, for tests and cost-free demos.

It prints the same stream-json shapes as ``claude -p --output-format
stream-json --verbose`` and really edits files in its cwd (the worktree).
Behaviour is steered by directives embedded in the prompt:

    [[fake:write PATH]]content[[/fake]]    write PATH (relative to cwd); repeatable
    [[fake:append PATH]]content[[/fake]]   append to PATH; repeatable
    [[fake:ask]]        first run ends with "QUESTION: ..."; --resume continues
    [[fake:fail]]       a few steps, then an error result and exit 1
    [[fake:sleep N]]    N extra seconds of sleep in the middle
    [[fake:hang]]       after init, sleep forever with no output
    [[fake:cost X]]     report total_cost_usd X

With no file directives it appends ``- <task>`` to CONDUCTOR_NOTES.md.
Env: FAKE_AGENT_STEP_SECS (default 1.5), FAKE_AGENT_STATE_DIR
(default ~/.cache/conductor-fake) where an asking run saves its prompt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

FILE_RE = re.compile(r"\[\[fake:(write|append)\s+([^\]]+?)\]\](.*?)\[\[/fake\]\]", re.S)
FLAG_RE = re.compile(r"\[\[fake:(ask|fail|hang)\]\]")
SLEEP_RE = re.compile(r"\[\[fake:sleep\s+([0-9.]+)\]\]")
COST_RE = re.compile(r"\[\[fake:cost\s+([0-9.]+)\]\]")
ANY_RE = re.compile(r"\[\[fake:(write|append)\s+[^\]]+?\]\].*?\[\[/fake\]\]|"
                    r"\[\[fake:sh\]\].*?\[\[/fake\]\]|\[\[fake:[^\]]*\]\]", re.S)
BASH_RE = re.compile(r"\[\[fake:bash ([^\]]+)\]\]")  # [[fake:bash git push origin main]]
# [[fake:sh]]echo hi > x.txt[[/fake]]: really executed (bash -c, in the worktree),
# one Bash tool call per non-empty line, with the real output and exit status.
SH_RE = re.compile(r"\[\[fake:sh\]\](.*?)\[\[/fake\]\]", re.S)
ASK_RE = re.compile(r"\[\[fake:ask:\s*([^\]]+)\]\]")  # [[fake:ask: custom question?]]
QUESTION = "Which approach should I take, A or B?"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class Agent:
    def __init__(self, session_id: str, prompt: str, resume: bool):
        self.sid = session_id
        self.step_secs = _float_env("FAKE_AGENT_STEP_SECS", 1.5)
        self.state_dir = Path(os.environ.get("FAKE_AGENT_STATE_DIR")
                              or Path.home() / ".cache" / "conductor-fake").expanduser()
        self.turns = 0
        self.started = time.time()
        self.answer: str | None = None
        self.task_prompt = prompt
        self.resumed_with_state = False
        if resume:
            st = self._load_state()
            if st is not None:
                self.task_prompt = st.get("prompt", "")
                self.answer = prompt.strip()
                self.resumed_with_state = True
        self.resume = resume
        # The conductor's "resolve conflicts" prompt: resolve, don't redo the task.
        self.resolve = "conflict markers" in prompt
        p = "" if self.resolve else self.task_prompt
        self.files = [(m.group(1), m.group(2).strip(), m.group(3)) for m in FILE_RE.finditer(p)]
        flags = set(FLAG_RE.findall(p))
        m_ask = ASK_RE.search(p)
        self.question = m_ask.group(1).strip() if m_ask else QUESTION
        self.ask = ("ask" in flags or m_ask is not None) and not resume
        self.fail = "fail" in flags
        self.hang = "hang" in flags
        self.extra_sleep = sum(float(x) for x in SLEEP_RE.findall(p))
        self.bash = [c.strip() for c in BASH_RE.findall(p)]
        self.sh = [line.strip() for block in SH_RE.findall(p)
                   for line in block.splitlines() if line.strip()]
        m = COST_RE.search(p)
        self.cost = float(m.group(1)) if m else 0.0
        self.task = " ".join(ANY_RE.sub(" ", p).split()) or "(no task)"

    # -- state ------------------------------------------------------------
    def _state_path(self) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.sid)
        return self.state_dir / f"{safe}.json"

    def _load_state(self) -> dict | None:
        try:
            return json.loads(self._state_path().read_text())
        except (OSError, ValueError):
            return None

    def _save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(json.dumps({"prompt": self.task_prompt,
                                                  "cwd": os.getcwd()}))

    def _clear_state(self) -> None:
        try:
            self._state_path().unlink()
        except OSError:
            pass

    # -- output -----------------------------------------------------------
    def emit(self, obj: dict) -> None:
        obj.setdefault("session_id", self.sid)
        obj.setdefault("uuid", str(uuid.uuid4()))
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def pause(self, secs: float | None = None) -> None:
        secs = self.step_secs if secs is None else secs
        if secs > 0:
            time.sleep(secs)

    def summary(self, detail: str | None) -> None:
        self.emit({"type": "system", "subtype": "task_summary", "detail": detail})

    def assistant(self, content: list[dict]) -> None:
        self.turns += 1
        self.emit({"type": "assistant", "parent_tool_use_id": None, "message": {
            "model": "fake", "id": "msg_" + uuid.uuid4().hex[:20], "type": "message",
            "role": "assistant", "content": content, "stop_reason": None}})

    def text(self, text: str) -> None:
        self.assistant([{"type": "text", "text": text}])

    def tool(self, name: str, input: dict, result: str, is_error: bool = False) -> None:
        tid = "toolu_" + uuid.uuid4().hex[:20]
        self.assistant([{"type": "tool_use", "id": tid, "name": name, "input": input}])
        self.pause()
        self.emit({"type": "user", "parent_tool_use_id": None, "message": {
            "role": "user", "content": [{"tool_use_id": tid, "type": "tool_result",
                                         "content": result, "is_error": is_error}]}})

    def result(self, text: str, *, subtype: str = "success", is_error: bool = False) -> None:
        self.summary(None)
        self.emit({"type": "result", "subtype": subtype, "is_error": is_error,
                   "duration_ms": int((time.time() - self.started) * 1000),
                   "num_turns": self.turns, "result": text,
                   "stop_reason": "end_turn", "total_cost_usd": self.cost})

    # -- steps ------------------------------------------------------------
    def step_read(self) -> None:
        cwd = Path.cwd()
        target = None
        readmes = sorted(p for p in cwd.glob("README*") if p.is_file())
        if readmes:
            target = readmes[0]
        else:
            for root, dirs, files in os.walk(cwd):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    if not f.startswith("."):
                        target = Path(root) / f
                        break
                if target:
                    break
        if target is None:
            self.summary("Looking around the project")
            self.tool("Glob", {"pattern": "*"}, "No files found")
            return
        rel = target.relative_to(cwd)
        self.summary(f"Reading {rel}")
        try:
            body = target.read_text(errors="replace")[:2000]
        except OSError as e:
            body = str(e)
        lines = "\n".join(f"{i:>6}\t{l}" for i, l in enumerate(body.splitlines()[:40], 1))
        self.tool("Read", {"file_path": str(target)}, lines or "(empty file)")

    def _safe_path(self, rel: str) -> Path | None:
        cwd = Path.cwd().resolve()
        p = (cwd / rel).resolve()
        if p != cwd and cwd not in p.parents:
            return None
        return p

    def step_files(self) -> list[str]:
        """Perform file directives (or the default notes edit). Returns paths touched."""
        touched = []
        actions = self.files or [("append", "CONDUCTOR_NOTES.md", f"- {self.task[:60]}\n")]
        for mode, rel, content in actions:
            p = self._safe_path(rel)
            if p is None:
                self.tool("Write", {"file_path": rel, "content": content[:200]},
                          f"Refusing to write outside the worktree: {rel}", is_error=True)
                continue
            existed = p.exists()
            self.summary(f"{'Writing' if mode == 'write' else 'Editing'} {rel}")
            p.parent.mkdir(parents=True, exist_ok=True)
            if mode == "write":
                p.write_text(content)
                self.tool("Write", {"file_path": str(p), "content": content[:1000]},
                          f"File {'updated' if existed else 'created'} successfully at: {p}")
            else:
                with open(p, "a") as f:
                    f.write(content)
                self.tool("Edit", {"file_path": str(p), "old_string": "",
                                   "new_string": content[:1000]},
                          f"The file {p} has been updated.")
            touched.append(rel)
        return touched

    def run_shell(self, cmd: str) -> None:
        """Really run a shell command, reported exactly like Claude's Bash tool."""
        import subprocess
        self.summary(f"Running {cmd}")
        tid = "toolu_" + uuid.uuid4().hex[:20]
        self.assistant([{"type": "tool_use", "id": tid, "name": "Bash",
                         "input": {"command": cmd}}])
        try:
            p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60)
            out, code = ((p.stdout or "") + (p.stderr or "")).strip(), p.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            out, code = str(e), 1
        body = (f"Exit code {code}\n{out}" if code else out) or "(no output)"
        self.emit({"type": "user", "parent_tool_use_id": None, "message": {
            "role": "user", "content": [{"tool_use_id": tid, "type": "tool_result",
                                         "content": body, "is_error": code != 0}]}})
        self.pause()

    def step_tests(self) -> None:
        self.summary("Running the tests")
        self.tool("Bash", {"command": "echo 'running tests'", "description": "running tests"},
                  "running tests")

    def middle_sleep(self) -> None:
        if self.extra_sleep > 0:
            self.summary(f"Thinking hard ({self.extra_sleep:g}s)")
            end = time.time() + self.extra_sleep
            while (left := end - time.time()) > 0:
                time.sleep(min(left, 0.25))

    def run(self) -> int:
        self.emit({"type": "system", "subtype": "init", "cwd": os.getcwd(),
                   "session_id": self.sid, "model": "fake", "tools": [
                       "Bash", "Edit", "Glob", "Read", "Write"],
                   "permissionMode": "bypassPermissions"})
        if self.hang:
            while True:
                time.sleep(3600)
        self.pause()

        if self.resolve:
            return self.run_resolve()

        if self.resumed_with_state:
            self.text(f"Thanks. Going with: {self.answer[:200]}")
            self.pause()
        else:
            self.text(f"I'll start by looking at the project. Task: {self.task[:100]}")

        self.step_read()
        self.pause()

        if self.ask:
            self._save_state()
            msg = ("I looked at the project and there are two reasonable ways to do this.\n\n"
                   f"QUESTION: {self.question}")
            self.text(msg)
            self.result(msg)
            return 0

        if self.fail:
            self.summary("Running the build")
            self.tool("Bash", {"command": "make build", "description": "Build the project"},
                      "make: *** [build] Error 2\nfake failure", is_error=True)
            self.pause()
            self.result("Fake agent failed as instructed.",
                        subtype="error_during_execution", is_error=True)
            return 1

        for cmd in self.bash:  # emitted as if run; the fake agent enforces no policy
            self.summary(f"Running {cmd}")
            self.tool("Bash", {"command": cmd}, f"(fake) ran: {cmd}")
            self.pause()
        self.middle_sleep()
        touched = self.step_files()
        self.pause()
        for cmd in self.sh:
            self.run_shell(cmd)
        self.step_tests()
        self.pause()

        lines = [f"Done: {self.task[:100]}"]
        if self.answer:
            lines.append(f"Used your answer: {self.answer[:200]}")
        lines.append("Changed: " + (", ".join(touched) if touched else "nothing"))
        lines.append("Why: the fake agent was asked to (scripted demo).")
        lines.append("Tests: ran `echo 'running tests'`, passed.")
        summary = "\n".join(lines)
        self.text(summary)
        self.result(summary)
        self._clear_state()
        return 0


    def run_resolve(self) -> int:
        """Resolve conflict markers by keeping both sides (fine for a demo)."""
        import subprocess
        self.text("Resolving the merge conflicts.")
        found = subprocess.run(["git", "grep", "-l", "-E", "^<<<<<<< "], capture_output=True,
                               text=True).stdout.split()
        for rel in found:
            p = Path(rel)
            self.summary(f"Resolving conflicts in {rel}")
            kept = [l for l in p.read_text().splitlines(keepends=True)
                    if not re.match(r"^(<<<<<<<|=======|>>>>>>>)( |$)", l.rstrip("\n"))]
            p.write_text("".join(kept))
            self.tool("Edit", {"file_path": str(p.resolve()), "old_string": "<<<<<<< …",
                               "new_string": "(both sides kept)"},
                      f"The file {p.resolve()} has been updated.")
            self.pause()
        self.step_tests()
        self.pause()
        summary = ("Resolved conflicts in " + (", ".join(found) or "no files") +
                   " by keeping both sides.\nTests: passed.")
        self.text(summary)
        self.result(summary)
        return 0


def _on_term(signum, frame):
    try:
        sys.stdout.flush()
    finally:
        os._exit(143)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fake_agent")
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--prompt", default="")
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_term)
    try:
        return Agent(a.session_id, a.prompt, a.resume).run()
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
