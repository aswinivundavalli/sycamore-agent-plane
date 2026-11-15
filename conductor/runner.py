"""Detached runner: the only process that is the agent's parent.

The conductor never holds the agent's stdout pipe. Instead it starts this small
wrapper in a new session (process group); the wrapper starts the agent with
stdout going straight to ``stream.jsonl``, waits for it, and writes the exit
code to ``exit.json`` (atomically, via rename).

Why: if the conductor is killed, nothing reads a pipe that could fill up and
block the agent, and nothing gets SIGPIPE. The agent keeps working; when the
conductor comes back it re-attaches by tailing the file from its stored offset
and watching the runner pid. If the runner itself died (reboot, kill -9 of the
group) there is no exit.json and no live pid, and the run is marked
interrupted — never silently restarted.

Usage (by the supervisor only):
    python -m conductor.runner <attempt_dir> <cwd> -- <argv...>
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    attempt_dir = Path(argv[0])
    cwd = argv[1]
    cmd = argv[3:]

    stream = open(attempt_dir / "stream.jsonl", "ab", buffering=0)
    stderr = open(attempt_dir / "stderr.log", "ab", buffering=0)
    try:
        child = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                                 stdout=stream, stderr=stderr)
    except OSError as e:
        stderr.write(f"runner: failed to start agent: {e}\n".encode())
        _write_json_atomic(attempt_dir / "exit.json",
                           {"code": 127, "error": str(e), "ended_at": time.time()})
        return 127

    _write_json_atomic(attempt_dir / "runner.json",
                       {"runner_pid": os.getpid(), "agent_pid": child.pid,
                        "started_at": time.time()})

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # survive the terminal closing

    code = child.wait()
    _write_json_atomic(attempt_dir / "exit.json", {"code": code, "ended_at": time.time()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
