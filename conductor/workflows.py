"""Repeatable workflows: a named chain of agent steps, defined once in TOML.

A workflow is fired at a task ("fix the flaky upload test") and runs its steps
in order. This module only DEFINES, VALIDATES, LOADS and RENDERS workflows;
execution belongs to the supervisor.

Execution model (implemented by the supervisor, described here for authors)
---------------------------------------------------------------------------
All steps of one workflow run as successive attempts of ONE run, in ONE git
worktree, so every step sees the previous steps' changes. For each step the
supervisor renders the prompt (:func:`render`) and starts the agent:

* ``session = "continue"`` resumes the previous step's agent session (the
  agent remembers what it did); ``"fresh"`` starts a new session, and the
  prompt should carry ``{previous_summary}`` (the previous step's summary) to
  hand over context - useful for a review step that should look with fresh
  eyes.
* ``tests`` decides what happens after the step: ``"required"`` - the tests
  run and must pass for the workflow to continue; ``"optional"`` - they run,
  the result is recorded, the workflow continues regardless; ``"skip"`` - no
  test run (e.g. a step that deliberately adds a failing test).
* ``on_fail``: when a step fails (the agent errors, or required tests fail),
  ``"stop"`` ends the workflow there; ``"continue"`` records the failure and
  moves on to the next step.
* ``gate = "approve"``: once the step finishes, a human must approve before
  the NEXT step starts (on the last step: before the run is finished).
* ``budget_usd`` optionally caps that step's spend (still subject to policy).

A snapshot of the workflow (:meth:`Workflow.to_dict`) is stored on the run, so
editing the TOML later does not change runs in flight.

File format and layering
------------------------
::

    name = "feature"                       # slug; must equal the file stem
    description = "Implement, test, self-review, document"
    [[steps]]
    id = "implement"                       # slug, unique in the workflow
    prompt = "{task}"
    tests = "required"                     # required | optional | skip
    gate = "none"                          # none | approve
    on_fail = "stop"                       # stop | continue
    session = "continue"                   # continue | fresh
    budget_usd = 2.5                       # optional

Prompts are ``str.format`` templates restricted to the placeholders in
:data:`PLACEHOLDERS`; ``{{`` and ``}}`` produce literal braces. Unknown keys,
unknown placeholders and bad enum values are errors, so typos fail loudly at
load time rather than mid-run.

:func:`load_all` reads, in order, the built-ins shipped in
``conductor/builtin_workflows/``, then ``<home>/workflows/``, then
``<repo>/.conductor/workflows/``; a later layer replaces an earlier workflow
of the same name.
"""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "WorkflowError", "Step", "Workflow", "parse", "render", "load_file",
    "load_all", "BUILTIN_DIR", "PLACEHOLDERS", "MAX_STEPS",
]

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin_workflows"
MAX_STEPS = 20
PLACEHOLDERS = frozenset({
    "task", "previous_summary", "step", "step_number", "total_steps",
    "workflow",
})

_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_TOP_KEYS = ("name", "description", "steps")
_STEP_KEYS = ("id", "prompt", "tests", "gate", "on_fail", "session",
              "budget_usd")
_ENUMS = {
    "tests": ("required", "optional", "skip"),
    "gate": ("none", "approve"),
    "on_fail": ("stop", "continue"),
    "session": ("continue", "fresh"),
}


class WorkflowError(ValueError):
    """An invalid workflow definition; the message names the source."""


@dataclass(frozen=True)
class Step:
    id: str
    prompt: str
    tests: str = "required"
    gate: str = "none"
    on_fail: str = "stop"
    session: str = "continue"
    budget_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in _STEP_KEYS}


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    steps: tuple[Step, ...]
    source: str

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot; :meth:`from_dict` restores it exactly."""
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workflow":
        """Rebuild (and re-validate) a workflow from :meth:`to_dict` output."""
        if not isinstance(data, dict):
            raise WorkflowError("workflow snapshot: expected a dict")
        source = data.get("source", "<snapshot>")
        if not isinstance(source, str):
            raise WorkflowError("workflow snapshot: 'source' must be a string")
        body = {k: v for k, v in data.items() if k != "source"}
        return _build(body, source)


def _fail(source: str, problem: str) -> WorkflowError:
    return WorkflowError(f"{source}: {problem}")


def _check_keys(source: str, where: str, table: dict, allowed) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise _fail(source, f"{where}: unknown key(s) {', '.join(map(repr, unknown))}"
                            f" (allowed: {', '.join(allowed)})")


def _check_prompt(source: str, where: str, prompt: str) -> None:
    try:
        parts = list(string.Formatter().parse(prompt))
    except ValueError as e:
        raise _fail(source, f"{where}: bad prompt template ({e}); "
                            "write {{ and }} for literal braces") from None
    for _literal, field, spec, conversion in parts:
        if field is None:
            continue
        if field not in PLACEHOLDERS:
            shown = "{}" if field == "" else "{" + field + "}"
            raise _fail(source, f"{where}: unknown placeholder {shown} in prompt "
                                f"(allowed: {', '.join('{%s}' % p for p in sorted(PLACEHOLDERS))};"
                                " write {{ and }} for literal braces)")
        if spec or conversion:
            raise _fail(source, f"{where}: placeholder {{{field}}} may not have a "
                                "format spec or conversion")


def _build_step(source: str, i: int, raw: Any) -> Step:
    where = f"step {i + 1}"
    if not isinstance(raw, dict):
        raise _fail(source, f"{where}: expected a table ([[steps]])")
    _check_keys(source, where, raw, _STEP_KEYS)
    sid = raw.get("id")
    if not isinstance(sid, str) or not _SLUG.fullmatch(sid):
        raise _fail(source, f"{where}: id {sid!r} must be a slug matching "
                            f"{_SLUG.pattern}")
    where = f"step {i + 1} ({sid!r})"
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _fail(source, f"{where}: 'prompt' must be a non-empty string")
    _check_prompt(source, where, prompt)
    kwargs: dict[str, Any] = {"id": sid, "prompt": prompt}
    for key, allowed in _ENUMS.items():
        if key in raw:
            value = raw[key]
            if value not in allowed or not isinstance(value, str):
                raise _fail(source, f"{where}: {key} = {value!r} is not one of "
                                    f"{', '.join(allowed)}")
            kwargs[key] = value
    budget = raw.get("budget_usd")
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) \
                or not budget > 0 or budget != budget or budget == float("inf"):
            raise _fail(source, f"{where}: budget_usd = {budget!r} must be a "
                                "positive number")
        kwargs["budget_usd"] = float(budget)
    return Step(**kwargs)


def _build(data: dict[str, Any], source: str) -> Workflow:
    _check_keys(source, "workflow", data, _TOP_KEYS)
    name = data.get("name")
    if not isinstance(name, str) or not _SLUG.fullmatch(name):
        raise _fail(source, f"name {name!r} must be a slug matching "
                            f"{_SLUG.pattern}")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise _fail(source, "'description' must be a string")
    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list):
        raise _fail(source, "'steps' must be an array of tables ([[steps]])")
    if not raw_steps:
        raise _fail(source, "a workflow needs at least 1 step")
    if len(raw_steps) > MAX_STEPS:
        raise _fail(source, f"{len(raw_steps)} steps is more than the maximum "
                            f"of {MAX_STEPS}")
    steps = tuple(_build_step(source, i, s) for i, s in enumerate(raw_steps))
    seen: set[str] = set()
    for s in steps:
        if s.id in seen:
            raise _fail(source, f"duplicate step id {s.id!r}")
        seen.add(s.id)
    return Workflow(name=name, description=description, steps=steps,
                    source=source)


def parse(text: str, source: str) -> Workflow:
    """Parse and validate workflow TOML; ``source`` names it in errors."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise _fail(source, f"invalid TOML: {e}") from None
    return _build(data, source)


def render(workflow: Workflow, index: int, task: str,
           previous_summary: str | None) -> str:
    """The prompt for step ``index`` (0-based) with placeholders filled.

    Steps after the first get a one-line preamble saying where in the
    workflow the agent is.
    """
    if not 0 <= index < len(workflow.steps):
        raise IndexError(f"workflow {workflow.name!r} has no step {index} "
                         f"(it has {len(workflow.steps)})")
    step = workflow.steps[index]
    total = len(workflow.steps)
    body = step.prompt.format(
        task=task,
        previous_summary=previous_summary or "",
        step=step.id,
        step_number=index + 1,
        total_steps=total,
        workflow=workflow.name,
    )
    if index == 0:
        return body
    preamble = (f'This is step {index + 1} of {total} ("{step.id}") of the '
                f'"{workflow.name}" workflow.')
    return f"{preamble}\n\n{body}"


def load_file(path: Path) -> Workflow:
    """Load one workflow file; its ``name`` must equal the file stem."""
    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise _fail(source, f"cannot read workflow file: {e}") from None
    wf = parse(text, source)
    if wf.name != path.stem:
        raise _fail(source, f"name {wf.name!r} does not match the file name "
                            f"(expected name = {path.stem!r})")
    return wf


def load_all(home: Path, repo: Path | None) -> dict[str, Workflow]:
    """All workflows visible for ``repo``, keyed and sorted by name.

    Layers (later wins by name): built-ins, ``<home>/workflows``,
    ``<repo>/.conductor/workflows``. Missing directories are skipped.
    """
    dirs = [BUILTIN_DIR, Path(home) / "workflows"]
    if repo is not None:
        dirs.append(Path(repo) / ".conductor" / "workflows")
    found: dict[str, Workflow] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.toml")):
            if path.is_file():
                wf = load_file(path)
                found[wf.name] = wf
    return dict(sorted(found.items()))
