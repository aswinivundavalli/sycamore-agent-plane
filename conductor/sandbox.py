"""OS sandbox for agents: everything outside the worktree is read-only.

Enabled per repository by the policy:

    [sandbox]
    writes = "worktree"

Why an OS sandbox and not more policy checks: the live policy guard sees tool
calls (file edits, shell commands matched against a forbidden list) and the
pre-merge check sees the diff. A shell command that writes somewhere else
(``echo x > /tmp/x.py``, ``>> ~/.bashrc``, ``cp`` into the main checkout, a
Python one-liner, a test that does it when the suite runs) is visible to
neither: it never touches the diff, and recognising every way a program can
write a file from its command line is impossible. A real agent did exactly
this in the fleet test. So the guarantee comes from the kernel instead:

  * Linux:  bubblewrap. The whole filesystem is bind-mounted read-only, then
            the worktree (and the agent's own state directory) are bound
            writable on top, and /tmp is replaced by a private directory.
  * macOS:  sandbox-exec with a profile that denies file writes except under
            the worktree, the private temp directory and the agent's state.

The agent keeps the network, its processes stay in the conductor's process
group (so Stop and the watchdog still work), and the conductor itself (which
commits, pushes and merges) runs outside the sandbox. The repository's test
command runs inside the same sandbox when the policy asks for it, otherwise
an agent could write a test that escapes when the conductor runs the suite.

If the policy asks for the sandbox and this machine cannot provide one, the
run does not start (fail closed).

Self-check:  python3 -m conductor.sandbox --check
"""

from __future__ import annotations

import functools
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Markers that a write was refused by the sandbox (Linux, macOS).
REFUSED = ("read-only file system", "operation not permitted")


@functools.lru_cache(maxsize=1)
def available() -> tuple[bool, str, str]:
    """(ok, kind, detail). ``detail`` is the reason when not ok."""
    system = platform.system()
    if system == "Linux":
        bwrap = shutil.which("bwrap")
        if not bwrap:
            return False, "bubblewrap", ("bubblewrap is not installed "
                                         "(apt-get install bubblewrap / dnf install bubblewrap)")
        try:
            p = subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                                "true"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, "bubblewrap", f"bubblewrap failed to run: {e}"
        if p.returncode != 0:
            return False, "bubblewrap", ("bubblewrap cannot create a sandbox here "
                                         f"(user namespaces disabled?): {p.stderr.strip()[:200]}")
        return True, "bubblewrap", bwrap
    if system == "Darwin":
        exe = "/usr/bin/sandbox-exec"
        if not os.path.exists(exe):
            return False, "sandbox-exec", "sandbox-exec is not available"
        try:
            p = subprocess.run([exe, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, "sandbox-exec", f"sandbox-exec failed to run: {e}"
        if p.returncode != 0:
            return False, "sandbox-exec", f"sandbox-exec refused: {p.stderr.strip()[:200]}"
        return True, "sandbox-exec", exe
    return False, "", f"no supported sandbox on {system}"


def agent_state_paths() -> list[Path]:
    """Where the agent CLI keeps its own state (sessions, settings, logs).
    Writable inside the sandbox; nothing else in the home directory is."""
    home = Path.home()
    return [p for p in (home / ".claude", home / ".claude.json") if p.exists()]


def wrap(argv: list[str], worktree: str | Path, private_tmp: str | Path,
         extra_writable: list[str | Path] = ()) -> tuple[list[str], dict]:
    """The command line that runs ``argv`` sandboxed, plus env to overlay.

    Raises RuntimeError if no sandbox is available.
    """
    ok, kind, detail = available()
    if not ok:
        raise RuntimeError(detail)
    wt = os.path.realpath(worktree)
    tmp = Path(private_tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    tmp = os.path.realpath(tmp)
    writable = [os.path.realpath(p) for p in [*extra_writable, *agent_state_paths()]
                if os.path.exists(p)]
    if kind == "bubblewrap":
        cmd = [detail, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
               # private /tmp first, so paths under /tmp bound below stay visible
               "--bind", tmp, "/tmp",
               "--bind", wt, wt]
        for p in writable:
            cmd += ["--bind", p, p]
        cmd += ["--chdir", wt, "--", *argv]
        return cmd, {"TMPDIR": "/tmp"}
    return [detail, "-p", macos_profile(wt, tmp, writable), *argv], {"TMPDIR": tmp}


def macos_profile(worktree: str, tmp: str, writable: list[str]) -> str:
    """sandbox-exec profile: deny every file write except the listed places.
    (Later rules win in SBPL, so the allow list overrides the blanket deny.)"""
    home = os.path.realpath(Path.home())

    def lit(path: str) -> str:
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'

    allow = [f"(subpath {lit(worktree)})", f"(subpath {lit(tmp)})", '(subpath "/dev")',
             # the agent CLI's own state (~/.claude/, ~/.claude.json and its backups)
             f'(regex #"^{re.escape(home)}/\\.claude")',
             # per-user caches and temp area macOS frameworks and node write to
             f"(subpath {lit(home + '/Library/Caches')})",
             '(subpath "/private/var/folders")']
    allow += [f"(subpath {lit(p)})" for p in writable]
    return ("(version 1)\n(allow default)\n(deny file-write*)\n"
            "(allow file-write*\n    " + "\n    ".join(allow) + ")\n")


# ---------------------------------------------------------------- self-check
def self_check() -> int:
    """Prove the sandbox on this machine: a write inside the worktree works,
    a write next to it and into the home directory are refused, and /tmp is
    private. Prints one line per probe; exit status 0 only if all hold."""
    ok, kind, detail = available()
    if not ok:
        print(f"sandbox: NOT AVAILABLE ({kind or 'none'}): {detail}")
        return 1
    print(f"sandbox: {kind} ({detail})")
    base = Path(tempfile.mkdtemp(prefix="conductor-sandbox-check-", dir=Path.home()))
    try:
        wt, outside, tmp = base / "worktree", base / "outside", base / "private-tmp"
        for d in (wt, outside, tmp):
            d.mkdir()
        marker = f"conductor-sandbox-check-{os.getpid()}"
        script = (f"echo in > inside.txt; "
                  f"echo out > {outside}/escaped.txt 2>/dev/null; "
                  f"echo tmp > /tmp/{marker} 2>/dev/null; true")
        argv, env = wrap(["/bin/sh", "-c", script], wt, tmp)
        subprocess.run(argv, cwd=wt, env={**os.environ, **env}, timeout=30)
        checks = [
            ("write inside the worktree works", (wt / "inside.txt").exists()),
            ("write outside the worktree is refused", not (outside / "escaped.txt").exists()),
            ("/tmp is private (nothing reached the host /tmp)",
             not Path(f"/tmp/{marker}").exists()),
        ]
        for name, passed in checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        Path(f"/tmp/{marker}").unlink(missing_ok=True)
        return 0 if all(p for _, p in checks) else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(self_check())
    print(__doc__)
