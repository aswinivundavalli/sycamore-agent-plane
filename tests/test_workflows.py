"""Tests for conductor.workflows (definition, validation, loading, rendering)."""

from __future__ import annotations

import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from conductor import workflows
from conductor.workflows import Step, Workflow, WorkflowError, load_all, parse, render

GOOD = textwrap.dedent('''
    name = "feature"
    description = "Implement, test, self-review, document"

    [[steps]]
    id = "implement"
    prompt = "{task}"
    tests = "required"

    [[steps]]
    id = "review"
    prompt = "Review your diff for step {step_number} of {total_steps} ({step}, {workflow}). Before: {previous_summary}"
    gate = "approve"
    on_fail = "continue"
    session = "fresh"
    budget_usd = 1.5
''')


def wf_toml(name="demo", steps=None, extra_top="", description="d"):
    steps = steps if steps is not None else ['id = "one"\nprompt = "{task}"']
    body = f'name = "{name}"\ndescription = "{description}"\n{extra_top}\n'
    for s in steps:
        body += f"\n[[steps]]\n{s}\n"
    return body


class ParseTests(unittest.TestCase):
    def test_happy_path(self):
        wf = parse(GOOD, "feature.toml")
        self.assertEqual(wf.name, "feature")
        self.assertEqual(wf.description, "Implement, test, self-review, document")
        self.assertEqual(wf.source, "feature.toml")
        self.assertEqual(len(wf.steps), 2)
        s0, s1 = wf.steps
        self.assertEqual(s0, Step(id="implement", prompt="{task}"))
        self.assertEqual((s0.tests, s0.gate, s0.on_fail, s0.session, s0.budget_usd),
                         ("required", "none", "stop", "continue", None))
        self.assertEqual((s1.id, s1.tests, s1.gate, s1.on_fail, s1.session),
                         ("review", "required", "approve", "continue", "fresh"))
        self.assertEqual(s1.budget_usd, 1.5)
        self.assertIsInstance(wf.steps, tuple)

    def test_frozen(self):
        wf = parse(GOOD, "x")
        with self.assertRaises(Exception):
            wf.name = "other"  # type: ignore[misc]
        with self.assertRaises(Exception):
            wf.steps[0].prompt = "x"  # type: ignore[misc]

    def test_integer_budget_becomes_float(self):
        wf = parse(wf_toml(steps=['id = "a"\nprompt = "x"\nbudget_usd = 3']), "s")
        self.assertEqual(wf.steps[0].budget_usd, 3.0)
        self.assertIsInstance(wf.steps[0].budget_usd, float)

    def test_description_optional(self):
        wf = parse('name = "x"\n[[steps]]\nid = "a"\nprompt = "p"\n', "s")
        self.assertEqual(wf.description, "")

    def test_all_enum_values_accepted(self):
        for tests in ("required", "optional", "skip"):
            for gate in ("none", "approve"):
                for on_fail in ("stop", "continue"):
                    for session in ("continue", "fresh"):
                        step = (f'id = "a"\nprompt = "p"\ntests = "{tests}"\n'
                                f'gate = "{gate}"\non_fail = "{on_fail}"\n'
                                f'session = "{session}"')
                        s = parse(wf_toml(steps=[step]), "s").steps[0]
                        self.assertEqual((s.tests, s.gate, s.on_fail, s.session),
                                         (tests, gate, on_fail, session))

    def test_twenty_steps_ok(self):
        steps = [f'id = "s{i}"\nprompt = "p"' for i in range(20)]
        self.assertEqual(len(parse(wf_toml(steps=steps), "s").steps), 20)

    def test_slug_forms(self):
        for good in ["a", "0", "dependency-bump", "a_b-c9", "a" * 64]:
            with self.subTest(slug=good):
                parse(wf_toml(name=good, steps=[f'id = "{good}"\nprompt = "p"']), "s")


class ValidationTests(unittest.TestCase):
    def assertWfError(self, text, *fragments, source="wf.toml"):
        with self.assertRaises(WorkflowError) as cm:
            parse(text, source)
        msg = str(cm.exception)
        self.assertIn(source, msg)
        for frag in fragments:
            self.assertIn(frag, msg)
        return msg

    def test_is_value_error(self):
        self.assertTrue(issubclass(WorkflowError, ValueError))

    def test_invalid_toml(self):
        self.assertWfError("name = ", "invalid TOML")

    def test_unknown_top_key(self):
        self.assertWfError(wf_toml(extra_top='author = "me"'), "unknown key", "'author'")

    def test_unknown_step_key(self):
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "p"\ntest = "skip"']),
                           "step 1", "unknown key", "'test'")

    def test_bad_enums(self):
        for key, value in [("tests", "always"), ("gate", "human"),
                           ("on_fail", "retry"), ("session", "new")]:
            with self.subTest(key=key):
                self.assertWfError(
                    wf_toml(steps=[f'id = "a"\nprompt = "p"\n{key} = "{value}"']),
                    key, repr(value), "'a'")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "p"\ntests = true']),
                           "tests")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "p"\ngate = ["none"]']),
                           "gate")

    def test_bad_budget(self):
        for value in ["0", "-1", "true", '"5"', "nan", "inf"]:
            with self.subTest(value=value):
                self.assertWfError(
                    wf_toml(steps=[f'id = "a"\nprompt = "p"\nbudget_usd = {value}']),
                    "budget_usd")

    def test_duplicate_step_ids(self):
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "p"',
                                          'id = "b"\nprompt = "p"',
                                          'id = "a"\nprompt = "q"']),
                           "duplicate step id", "'a'")

    def test_bad_workflow_name(self):
        for bad in ["Feature", "-x", "_x", "has space", "a" * 65, "é", ""]:
            with self.subTest(name=bad):
                self.assertWfError(wf_toml(name=bad), "name", "slug")
        self.assertWfError('[[steps]]\nid = "a"\nprompt = "p"\n', "name", "slug")
        self.assertWfError('name = 3\n[[steps]]\nid = "a"\nprompt = "p"\n', "name")

    def test_bad_step_id(self):
        for bad in ["Implement", "two words", "x/y", ""]:
            with self.subTest(id=bad):
                self.assertWfError(wf_toml(steps=[f'id = "{bad}"\nprompt = "p"']),
                                   "step 1", "slug")
        self.assertWfError(wf_toml(steps=['prompt = "p"']), "step 1", "slug")

    def test_bad_prompt(self):
        self.assertWfError(wf_toml(steps=['id = "a"']), "'a'", "prompt")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "   "']), "prompt")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = 5']), "prompt")

    def test_unknown_placeholder(self):
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "Do {thing} now"']),
                           "unknown placeholder", "{thing}", "'a'")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "Do {} now"']),
                           "unknown placeholder", "{}")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "{0}"']),
                           "unknown placeholder", "{0}")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "{task.upper}"']),
                           "unknown placeholder", "{task.upper}")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "{task[0]}"']),
                           "unknown placeholder")

    def test_format_spec_and_conversion_rejected(self):
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "{task!r}"']),
                           "{task}", "conversion")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "{step_number:>3}"']),
                           "{step_number}", "format spec")

    def test_bad_format_string(self):
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "unclosed {task"']),
                           "bad prompt template", "{{")
        self.assertWfError(wf_toml(steps=['id = "a"\nprompt = "stray } brace"']),
                           "bad prompt template")

    def test_zero_steps(self):
        self.assertWfError(wf_toml(steps=[]), "at least 1 step")
        self.assertWfError('name = "x"\nsteps = []\n', "at least 1 step")

    def test_steps_wrong_type(self):
        self.assertWfError('name = "x"\nsteps = "a"\n', "steps")
        self.assertWfError('name = "x"\nsteps = [1]\n', "step 1", "table")

    def test_too_many_steps(self):
        steps = [f'id = "s{i}"\nprompt = "p"' for i in range(21)]
        self.assertWfError(wf_toml(steps=steps), "21 steps", "maximum of 20")

    def test_description_must_be_string(self):
        self.assertWfError('name = "x"\ndescription = 1\n[[steps]]\nid = "a"\nprompt = "p"\n',
                           "description")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.wf = parse(GOOD, "feature.toml")

    def test_first_step_no_preamble(self):
        self.assertEqual(render(self.wf, 0, "Add a --verbose flag", None),
                         "Add a --verbose flag")

    def test_later_step_preamble_and_placeholders(self):
        out = render(self.wf, 1, "Add a --verbose flag", "I added the flag.")
        self.assertEqual(
            out,
            'This is step 2 of 2 ("review") of the "feature" workflow.\n\n'
            "Review your diff for step 2 of 2 (review, feature). "
            "Before: I added the flag.")

    def test_preamble_comes_before_prompt(self):
        out = render(self.wf, 1, "t", "s")
        self.assertTrue(out.startswith("This is step 2 of 2"))
        self.assertLess(out.index("This is step"), out.index("Review your diff"))

    def test_previous_summary_none_is_empty(self):
        out = render(self.wf, 1, "t", None)
        self.assertTrue(out.endswith("Before: "))
        self.assertNotIn("None", out)

    def test_task_with_braces_not_reformatted(self):
        out = render(self.wf, 0, "Handle {weird} input {0}", None)
        self.assertEqual(out, "Handle {weird} input {0}")

    def test_escaped_braces(self):
        wf = parse(wf_toml(steps=[
            'id = "a"\nprompt = "Use {{ and }} around {task}: {{task}}"']), "s")
        self.assertEqual(render(wf, 0, "X", None), "Use { and } around X: {task}")

    def test_index_out_of_range(self):
        with self.assertRaises(IndexError):
            render(self.wf, 2, "t", None)
        with self.assertRaises(IndexError):
            render(self.wf, -1, "t", None)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_exact(self):
        wf = parse(GOOD, "/some/where/feature.toml")
        d = wf.to_dict()
        self.assertEqual(json.loads(json.dumps(d)), d)  # JSON-safe
        back = Workflow.from_dict(json.loads(json.dumps(d)))
        self.assertEqual(back, wf)
        self.assertEqual(back.to_dict(), d)

    def test_dict_shape(self):
        d = parse(GOOD, "src").to_dict()
        self.assertEqual(set(d), {"name", "description", "source", "steps"})
        self.assertEqual(d["steps"][0], {
            "id": "implement", "prompt": "{task}", "tests": "required",
            "gate": "none", "on_fail": "stop", "session": "continue",
            "budget_usd": None})

    def test_from_dict_validates(self):
        d = parse(GOOD, "src").to_dict()
        d["steps"][0]["tests"] = "sometimes"
        with self.assertRaises(WorkflowError):
            Workflow.from_dict(d)
        with self.assertRaises(WorkflowError):
            Workflow.from_dict("nope")  # type: ignore[arg-type]

    def test_builtins_round_trip(self):
        for wf in load_all(Path(tempfile.gettempdir()) / "no-such-home", None).values():
            self.assertEqual(Workflow.from_dict(wf.to_dict()), wf)


class LoadAllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wf-test-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        self.home.mkdir()
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, directory: Path, name: str, text: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        p = directory / f"{name}.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_builtins_only(self):
        found = load_all(self.home, None)
        self.assertEqual(list(found), ["bugfix", "dependency-bump", "feature"])
        self.assertTrue(found["feature"].source.endswith("feature.toml"))
        self.assertEqual(Path(found["feature"].source).parent, workflows.BUILTIN_DIR)

    def test_missing_dirs_fine(self):
        found = load_all(self.tmp / "missing-home", self.tmp / "missing-repo")
        self.assertIn("feature", found)

    def test_layering(self):
        self.write(self.home / "workflows", "feature",
                   wf_toml(name="feature", description="home feature"))
        self.write(self.home / "workflows", "release",
                   wf_toml(name="release", description="home release"))
        repo_feature = self.write(self.repo / ".conductor" / "workflows", "feature",
                                  wf_toml(name="feature", description="repo feature"))
        self.write(self.repo / ".conductor" / "workflows", "zz-local",
                   wf_toml(name="zz-local"))
        found = load_all(self.home, self.repo)
        self.assertEqual(list(found), sorted(found))
        self.assertEqual(found["feature"].description, "repo feature")
        self.assertEqual(found["feature"].source, str(repo_feature))
        self.assertEqual(found["release"].description, "home release")  # home adds
        self.assertIn("bugfix", found)                                   # builtin kept
        self.assertIn("zz-local", found)

    def test_home_overrides_builtin_without_repo(self):
        self.write(self.home / "workflows", "bugfix",
                   wf_toml(name="bugfix", description="mine"))
        self.assertEqual(load_all(self.home, None)["bugfix"].description, "mine")

    def test_repo_none_ignores_repo_layer(self):
        self.write(self.repo / ".conductor" / "workflows", "feature",
                   wf_toml(name="feature", description="repo feature"))
        self.assertNotEqual(load_all(self.home, None)["feature"].description,
                            "repo feature")

    def test_name_must_match_filename(self):
        p = self.write(self.home / "workflows", "release", wf_toml(name="releases"))
        with self.assertRaises(WorkflowError) as cm:
            load_all(self.home, None)
        msg = str(cm.exception)
        self.assertIn(str(p), msg)
        self.assertIn("'releases'", msg)
        self.assertIn("'release'", msg)

    def test_invalid_file_names_source(self):
        p = self.write(self.repo / ".conductor" / "workflows", "bad",
                       wf_toml(name="bad", steps=['id = "a"\nprompt = "{nope}"']))
        with self.assertRaises(WorkflowError) as cm:
            load_all(self.home, self.repo)
        self.assertIn(str(p), str(cm.exception))
        self.assertIn("{nope}", str(cm.exception))

    def test_non_toml_files_ignored(self):
        d = self.home / "workflows"
        d.mkdir(parents=True)
        (d / "README.md").write_text("not a workflow")
        (d / "notes.toml.bak").write_text("junk")
        self.assertEqual(list(load_all(self.home, None)),
                         ["bugfix", "dependency-bump", "feature"])


class BuiltinTests(unittest.TestCase):
    def setUp(self):
        self.found = load_all(Path(tempfile.gettempdir()) / "no-such-home", None)

    def test_shapes(self):
        f = self.found["feature"]
        self.assertEqual([s.id for s in f.steps],
                         ["implement", "add-tests", "self-review", "docs"])
        self.assertEqual([s.tests for s in f.steps],
                         ["required", "required", "required", "optional"])
        self.assertEqual(f.steps[2].gate, "approve")
        self.assertEqual([s.gate for i, s in enumerate(f.steps) if i != 2],
                         ["none"] * 3)
        b = self.found["bugfix"]
        self.assertEqual([(s.id, s.tests) for s in b.steps],
                         [("reproduce", "skip"), ("fix", "required"),
                          ("verify", "required")])
        d = self.found["dependency-bump"]
        self.assertEqual([(s.id, s.tests) for s in d.steps],
                         [("bump", "required"), ("changelog", "optional")])

    def test_all_render(self):
        for name, wf in self.found.items():
            self.assertTrue(wf.description, name)
            for i, step in enumerate(wf.steps):
                with self.subTest(workflow=name, step=step.id):
                    out = render(wf, i, "TASK-TEXT-123", "SUMMARY-456")
                    self.assertNotIn("{", out.replace("TASK-TEXT-123", ""))
                    self.assertNotIn("}", out)
                    if i == 0:
                        self.assertIn("TASK-TEXT-123", out)
                    else:
                        self.assertTrue(out.startswith(
                            f'This is step {i + 1} of {len(wf.steps)} '
                            f'("{step.id}") of the "{name}" workflow.'))

    def test_fresh_session_steps_carry_context(self):
        # A fresh session has no memory: its prompt must hand over the task
        # and the previous summary.
        for name, wf in self.found.items():
            for step in wf.steps:
                if step.session == "fresh":
                    with self.subTest(workflow=name, step=step.id):
                        self.assertIn("{task}", step.prompt)
                        self.assertIn("{previous_summary}", step.prompt)


if __name__ == "__main__":
    unittest.main()
