import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from conductor.agents import stream
from conductor.agents.base import UNATTENDED_PROMPT, extract_question
from conductor.agents.claude import ClaudeAdapter
from conductor.agents.fake import FakeAdapter
from conductor.agents.registry import ADAPTER_NAMES, get_adapter

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream.jsonl"


def asst(*items):
    return json.dumps({"type": "assistant", "message": {"content": list(items)}})


def tool_use(name, inp):
    return {"type": "tool_use", "id": "t1", "name": name, "input": inp}


class TestStreamFixture(unittest.TestCase):
    def test_fixture(self):
        lines = FIXTURE.read_text().splitlines()
        results, sids, tools, acts = [], set(), [], []
        for line in lines:
            p = stream.parse_line(line)
            if p.session_id:
                sids.add(p.session_id)
            if p.result:
                results.append(p.result)
            if p.activity:
                acts.append(p.activity)
            tools += [e for e in p.events if e[0] == "tool"]
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["subtype"], "success")
        self.assertFalse(r["is_error"])
        self.assertGreater(r["cost_usd"], 0)
        self.assertIn("m.py", r["text"])
        self.assertEqual(sids, {"11111111-2222-3333-4444-555555555555"})
        self.assertEqual(tools[0][1], "Running: Show m.py contents")
        self.assertIn("Showing m.py contents", acts)
        self.assertEqual(acts[-1], "Finished")


class TestStreamSynthetic(unittest.TestCase):
    def act(self, name, inp):
        p = stream.parse_line(asst(tool_use(name, inp)))
        self.assertEqual(p.events[0][0], "tool")
        self.assertEqual(p.events[0][2]["name"], name)
        self.assertEqual(p.activity, p.events[0][1])
        return p.activity

    def test_tools(self):
        self.assertEqual(self.act("Read", {"file_path": "/a/b/c/src/app.py"}), "Reading …/c/src/app.py")
        self.assertEqual(self.act("Read", {"file_path": "src/app.py"}), "Reading src/app.py")
        self.assertTrue(self.act("Edit", {"file_path": "x.py"}).startswith("Editing x.py"))
        self.assertTrue(self.act("MultiEdit", {"file_path": "x.py"}).startswith("Editing"))
        self.assertEqual(self.act("Write", {"file_path": "x.py"}), "Writing x.py")
        self.assertEqual(self.act("Bash", {"command": "ls -la\nmore"}), "Looking through files")
        self.assertEqual(self.act("Bash", {"command": "echo hi"}), "Running: echo hi")
        self.assertEqual(self.act("Bash", {"command": "x", "description": "List files"}),
                         "Running: List files")
        self.assertEqual(self.act("Grep", {"pattern": "foo"}), "Searching for foo")
        self.assertEqual(self.act("Glob", {"pattern": "**/*.py"}), "Searching for **/*.py")
        self.assertIn("example.com", self.act("WebFetch", {"url": "https://example.com"}))
        self.assertIn("cats", self.act("WebSearch", {"query": "cats"}))
        self.assertEqual(self.act("Task", {"description": "find bugs"}), "Delegating: find bugs")
        self.assertEqual(self.act("Agent", {"description": "find bugs"}), "Delegating: find bugs")
        self.assertEqual(self.act("TodoWrite", {"todos": [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "in_progress", "activeForm": "Doing b"}]}),
            "Updating plan: Doing b")
        self.assertEqual(self.act("TodoWrite", {"todos": []}), "Updating plan")
        self.assertEqual(self.act("Mystery", {}), "Using Mystery")

    def test_long_input_truncated(self):
        p = stream.parse_line(asst(tool_use("Write", {"file_path": "a", "content": "x" * 100000})))
        self.assertLessEqual(len(json.dumps(p.events[0][2])), 2200)
        a = stream.describe_tool("Bash", {"command": "y" * 500})
        self.assertLessEqual(len(a), 120)

    def test_text_and_thinking(self):
        p = stream.parse_line(asst({"type": "thinking", "thinking": "hmm"},
                                   {"type": "text", "text": "Fixed it.\nMore detail"}))
        self.assertEqual(p.events, [("text", "Fixed it.\nMore detail", None)])
        self.assertEqual(p.activity, "Fixed it.")
        p = stream.parse_line(asst({"type": "text", "text": "z" * 5000}))
        self.assertLessEqual(len(p.events[0][1]), 4001)
        self.assertIsNone(p.activity)  # too long to be an activity
        self.assertEqual(stream.activity_from_text("\n  hello   world \nx"), "hello world")

    def test_tool_error(self):
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "boom\ntrace", "is_error": True},
            {"type": "tool_result", "content": "fine", "is_error": False}]}})
        p = stream.parse_line(line)
        self.assertEqual(p.events, [("error", "boom", None)])
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "bad"}], "is_error": True}]}})
        self.assertEqual(stream.parse_line(line).events[0][1], "bad")

    def test_summaries(self):
        p = stream.parse_line(json.dumps({"type": "system", "subtype": "task_summary", "detail": "Doing X"}))
        self.assertEqual(p.activity, "Doing X")
        p = stream.parse_line(json.dumps({"type": "system", "subtype": "task_summary", "detail": None}))
        self.assertIsNone(p.activity)
        p = stream.parse_line(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}))
        self.assertEqual(p.session_id, "s1")

    def test_results(self):
        def res(**kw):
            base = {"type": "result", "subtype": "success", "is_error": False,
                    "result": "ok", "total_cost_usd": 0.5, "num_turns": 3, "duration_ms": 9}
            base.update(kw)
            return stream.parse_line(json.dumps(base))
        p = res()
        self.assertEqual(p.activity, "Finished")
        self.assertEqual(p.result, {"subtype": "success", "is_error": False, "text": "ok",
                                    "cost_usd": 0.5, "num_turns": 3, "duration_ms": 9})
        self.assertEqual(p.events[0][0], "result")
        p = res(result="hmm\nQUESTION: A or B?")
        self.assertEqual(p.activity, "Asked a question")
        p = res(subtype="error_max_turns", is_error=True)
        self.assertEqual(p.activity, "Failed: error_max_turns")
        self.assertTrue(p.result["is_error"])

    def test_garbage(self):
        for line in ["", "   ", "not json", "{broken", "[1,2]", "null", "42",
                     '{"type":"rate_limit_event"}', '{"type":"whatever","x":1}',
                     '{"type":"assistant","message":null}',
                     '{"type":"assistant","message":{"content":[1,null,"s"]}}',
                     '{"type":"user","message":{"content":"plain"}}',
                     '{"type":"result","total_cost_usd":"abc"}', "\x00\xff"]:
            p = stream.parse_line(line)
            self.assertIsInstance(p, stream.Parsed)


class TestQuestion(unittest.TestCase):
    def test_extract(self):
        self.assertIsNone(extract_question(""))
        self.assertIsNone(extract_question(None))
        self.assertIsNone(extract_question("All done. No questions."))
        self.assertIsNone(extract_question("the QUESTION: inline is not a line"))
        self.assertEqual(extract_question("Summary\nQUESTION: A or B?"), "A or B?")
        self.assertEqual(extract_question("QUESTION: first\nmore\nQUESTION: second\nline two\n"),
                         "second\nline two")
        self.assertEqual(extract_question("x\n**QUESTION:** bold?"), "bold?")
        self.assertIsNone(extract_question("x\nQUESTION:   \n"))


class TestAdapters(unittest.TestCase):
    def test_registry(self):
        self.assertEqual(set(ADAPTER_NAMES), {"claude", "fake"})
        self.assertIsInstance(get_adapter("fake"), FakeAdapter)
        with self.assertRaises(KeyError) as cm:
            get_adapter("nope")
        self.assertIn("claude", str(cm.exception))

    def test_claude_argv(self):
        os.environ["CONDUCTOR_CLAUDE_BIN"] = "/opt/claude"
        try:
            a = ClaudeAdapter()
            s = a.build(prompt="do it", session_id="sid", resume=False, model="opus",
                        budget_usd=2.5, worktree="/w")
            self.assertEqual(s.argv[:3], ["/opt/claude", "-p", "do it"])
            self.assertIn("--dangerously-skip-permissions", s.argv)
            i = s.argv.index("--append-system-prompt")
            self.assertEqual(s.argv[i + 1], UNATTENDED_PROMPT)
            self.assertEqual(s.argv[s.argv.index("--session-id") + 1], "sid")
            self.assertEqual(s.argv[s.argv.index("--model") + 1], "opus")
            self.assertEqual(s.argv[s.argv.index("--max-budget-usd") + 1], "2.5")
            self.assertEqual(s.env, {"IS_SANDBOX": "1"})
            s = a.build(prompt="p", session_id="sid", resume=True, model=None,
                        budget_usd=None, worktree="/w")
            self.assertEqual(s.argv[s.argv.index("--resume") + 1], "sid")
            self.assertNotIn("--session-id", s.argv)
            self.assertNotIn("--model", s.argv)
            self.assertNotIn("--max-budget-usd", s.argv)
            self.assertFalse(a.available()[0])
        finally:
            del os.environ["CONDUCTOR_CLAUDE_BIN"]


class TestFakeAgentE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt"
        self.wt.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.wt, check=True)
        (self.wt / "README.md").write_text("# demo\n")
        self.state = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def spawn(self, prompt, sid, resume=False, wait=True):
        spec = FakeAdapter().build(prompt=prompt, session_id=sid, resume=resume,
                                   model=None, budget_usd=None, worktree=str(self.wt))
        env = dict(os.environ, **spec.env, FAKE_AGENT_STEP_SECS="0",
                   FAKE_AGENT_STATE_DIR=str(self.state))
        proc = subprocess.Popen(spec.argv, cwd=self.wt, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        if not wait:
            return proc
        out, err = proc.communicate(timeout=30)
        parsed = [stream.parse_line(l) for l in out.splitlines()]
        for l in out.splitlines():
            json.loads(l)  # every line is JSON
        results = [p.result for p in parsed if p.result]
        self.assertEqual(len(results), 1, err)
        self.assertEqual({p.session_id for p in parsed if p.session_id}, {sid})
        return proc.returncode, results[0], parsed

    def test_default(self):
        sid = str(uuid.uuid4())
        code, res, parsed = self.spawn("Add a retry to fetch() please", sid)
        self.assertEqual(code, 0)
        self.assertEqual(res["subtype"], "success")
        self.assertFalse(res["is_error"])
        self.assertEqual(res["cost_usd"], 0.0)
        self.assertEqual((self.wt / "CONDUCTOR_NOTES.md").read_text(),
                         "- Add a retry to fetch() please\n")
        tools = [e[1] for p in parsed for e in p.events if e[0] == "tool"]
        self.assertTrue(any(t.startswith("Reading") and "README.md" in t for t in tools), tools)
        self.assertTrue(any(t.startswith("Editing") for t in tools))
        self.assertTrue(any(t.startswith("Running:") for t in tools))

    def test_write_and_cost(self):
        prompt = ("Make files [[fake:write src/a.txt]]hello\n[[/fake]] and "
                  "[[fake:append README.md]]more\n[[/fake]] [[fake:cost 0.25]]")
        code, res, _ = self.spawn(prompt, str(uuid.uuid4()))
        self.assertEqual(code, 0)
        self.assertEqual((self.wt / "src/a.txt").read_text(), "hello\n")
        self.assertEqual((self.wt / "README.md").read_text(), "# demo\nmore\n")
        self.assertFalse((self.wt / "CONDUCTOR_NOTES.md").exists())
        self.assertEqual(res["cost_usd"], 0.25)

    def test_ask_resume(self):
        sid = str(uuid.uuid4())
        prompt = "Refactor [[fake:ask]] [[fake:write out.txt]]done[[/fake]]"
        code, res, parsed = self.spawn(prompt, sid)
        self.assertEqual(code, 0)
        self.assertEqual(extract_question(res["text"]), "Which approach should I take, A or B?")
        self.assertEqual(parsed[-1].activity, "Asked a question")
        self.assertFalse((self.wt / "out.txt").exists())
        code, res, _ = self.spawn("Use approach B", sid, resume=True)
        self.assertEqual(code, 0)
        self.assertIsNone(extract_question(res["text"]))
        self.assertIn("Use approach B", res["text"])
        self.assertEqual((self.wt / "out.txt").read_text(), "done")

    def test_resume_without_state(self):
        code, res, _ = self.spawn("Just a new task", str(uuid.uuid4()), resume=True)
        self.assertEqual(code, 0)
        self.assertTrue((self.wt / "CONDUCTOR_NOTES.md").exists())

    def test_fail(self):
        code, res, parsed = self.spawn("Break [[fake:fail]]", str(uuid.uuid4()))
        self.assertEqual(code, 1)
        self.assertTrue(res["is_error"])
        self.assertEqual(res["subtype"], "error_during_execution")
        self.assertTrue(any(e[0] == "error" for p in parsed for e in p.events))

    def test_hang_sigterm(self):
        proc = self.spawn("[[fake:hang]]", str(uuid.uuid4()), wait=False)
        first = proc.stdout.readline()
        self.assertEqual(stream.parse_line(first).session_id is not None, True)
        time.sleep(0.2)
        self.assertIsNone(proc.poll())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=5), 143)
        proc.stdout.close()
        proc.stderr.close()


if __name__ == "__main__":
    unittest.main()


class TestCommandSummaries(unittest.TestCase):
    """Real agents send shell calls without a description (44 of 44 in the
    more-itertools field test); the activity line must still say something."""

    def test_common_commands(self):
        from conductor.agents.stream import summarize_command as s
        cases = {
            'grep -n "raise\\|IndexError" more_itertools/recipes.py': "Searching for raise\\|IndexError",
            "cd . && git status --short; head -60 docs/versions.rst": "git status",
            "sed -n 55,80p docs/api.rst; grep x y": "Reading docs/api.rst",
            "sed -i 's/a/b/' pkg/more.py": "Editing pkg/more.py",
            "python3 - <<'EOF'\nprint(1)\nEOF": "Running a Python snippet",
            "python3 -m unittest discover -s tests -t .": "Running the tests",
            "cd web && npm test": "Running the tests",
            "ruff check .": "Running ruff",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(s(cmd), want)
        self.assertIsNone(s("echo hi"))

    def test_task_summary_echoing_a_command_is_rephrased(self):
        line = json.dumps({"type": "system", "subtype": "task_summary",
                           "detail": "Running python3 - <<'EOF' import re p='x'"})
        self.assertEqual(stream.parse_line(line).activity, "Running a Python snippet")
        line = json.dumps({"type": "system", "subtype": "task_summary",
                           "detail": "Running the remaining edits"})
        self.assertEqual(stream.parse_line(line).activity, "Running the remaining edits")

