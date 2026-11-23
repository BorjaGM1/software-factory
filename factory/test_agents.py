"""What each agent is started with, what happens when it can't prove it, and who lets a model-written check
run. With a fake `claude` and a scratch git repository, no model:

    python3.12 -m unittest discover -s factory -t .
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import builder, core, gate, learn, reviewer, trace

INIT = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": "fake"}
APPROVE = {"type": "result", "result": '{"decision": "approve", "reason": "fine"}', "total_cost_usd": 0.01}


def fake_claude(events: list[dict]) -> str:
    """An executable named `claude` that reads its prompt and prints these stream events, whatever its flags."""
    path = Path(tempfile.mkdtemp(prefix="fake-claude-")) / "claude"
    path.with_suffix(".stream").write_text("".join(json.dumps(e) + "\n" for e in events))
    path.write_text('#!/bin/sh\ncat > /dev/null\ncat "$0.stream"\n')
    path.chmod(0o755)
    return str(path)


def tool_call(name: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": {}}]}}


class ClosedCommands(unittest.TestCase):
    def test_reviewer_has_no_tools_connectors_skills_or_user_settings(self):
        with mock.patch.dict(os.environ, {"FACTORY_REVIEWER_CMD": "claude -p"}):
            cmd = reviewer.reviewer_cmd()
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], '{"mcpServers": {}}')
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        for flag in ("--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, cmd)

    def test_a_command_that_is_not_claude_is_run_as_given(self):
        self.assertEqual(trace.no_tools_cmd("my-reviewer --ask", "sonnet", "0.5"), ["my-reviewer", "--ask"])

    def test_builder_has_six_tools_rules_inside_its_worktree_and_the_targets_test_command(self):
        target = {"name": "t", "verify": {"test": "python -m pytest -q"}}
        with mock.patch.dict(os.environ, {"FACTORY_AGENT_CMD": "claude"}):
            cmd = builder.agent_cmd(target, "/work/wt")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Edit,Write,Glob,Grep,Bash")
        self.assertEqual((cmd[cmd.index("--mcp-config") + 1], cmd[cmd.index("--setting-sources") + 1]),
                         ('{"mcpServers": {}}', ""))
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        rules = cmd[cmd.index("--allowedTools") + 1:cmd.index("--max-budget-usd")]
        inside = f"/{Path('/work/wt').resolve()}/**"
        self.assertEqual(rules, [f"Read({inside})", f"Edit({inside})", "Bash(python -m pytest:*)", "Bash(git diff:*)",
                                 "Bash(git status:*)", "Bash(ls:*)"])
        prompt = builder.prompt_for({"id": "task-1"}, target, [])
        self.assertIn("`python -m pytest` (with any arguments), `git diff`, `git status` and `ls`", prompt)

    def test_the_test_command_the_builder_may_run_ends_at_pytest(self):
        for test, prefix in (("uv run pytest -q -p no:cacheprovider -rfE", "uv run pytest"), ("pytest", "pytest"),
                             ("uv run --with pytest pytest -q", "uv run --with pytest pytest"),
                             (".venv/bin/pytest -x", ".venv/bin/pytest"), ("make test", "make test")):
            self.assertEqual(builder.test_prefix({"verify": {"test": test}}), prefix)


class BuilderProof(unittest.TestCase):
    def run_builder(self, events: list[dict]) -> dict:
        return trace.run_agent("prompt", cwd=tempfile.mkdtemp(), cmd=[fake_claude(events), "-p"], tools=builder.TOOLS)

    def test_a_builder_with_its_own_tools_is_used(self):
        done = {"type": "result", "result": "done", "total_cost_usd": 0.02}
        reply = self.run_builder([{**INIT, "tools": ["Bash", "Read", "Edit"]}, tool_call("Read"), done])
        self.assertEqual((reply["ok"], reply["text"], reply.get("unverified")), (True, "done", None))

    def test_a_builder_with_a_connector_or_another_tool_is_stopped(self):
        done = {"type": "result", "result": "done", "total_cost_usd": 0.02}
        for events in ([{**INIT, "tools": ["Bash", "WebFetch"]}, done],
                       [{**INIT, "tools": ["Bash"], "mcp_servers": [{"name": "drive"}]}, done],
                       [{**INIT, "tools": ["Bash"]}, tool_call("mcp__drive__search"), done]):
            reply = self.run_builder(events)
            self.assertEqual((reply["ok"], reply["text"]), (False, ""))
            self.assertTrue(reply["unverified"])


class ReviewerProof(unittest.TestCase):
    def ask(self, events: list[dict]) -> dict:
        run_dir = Path(tempfile.mkdtemp(prefix="run-"))
        with mock.patch.dict(os.environ, {"FACTORY_REVIEWER_CMD": fake_claude(events) + " -p"}):
            return reviewer._ask("prompt", run_dir, "change", "approve", "request-changes")

    def test_a_reviewer_that_proves_it_has_no_tools_is_heard(self):
        self.assertEqual(self.ask([INIT, APPROVE])["decision"], "approve")

    def test_no_decision_from_a_reviewer_that(self):
        cases = {"started with a tool": [{**INIT, "tools": ["Bash"]}, APPROVE],
                 "started with a connector": [{**INIT, "mcp_servers": [{"name": "drive", "status": "pending"}]}, APPROVE],
                 "called a tool": [INIT, tool_call("mcp__drive__search"), APPROVE],
                 "answered before saying what it had": [{"type": "assistant", "message": {"content": []}}, INIT, APPROVE],
                 "never said what it had": [APPROVE]}
        for name, events in cases.items():
            with self.subTest(name):
                answer = self.ask(events)
                self.assertIsNone(answer["decision"])
                self.assertTrue(answer["unverified"])
                self.assertIn("could not be proven tool-free", answer["reason"])


class ModelWrittenCheck(unittest.TestCase):
    """A correction's rule: a model with no tools writes its shell check, and nothing runs it before a person
    approves it."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="factory-"))
        repo, runs = self.home / "target", self.home / "runs"
        sh = lambda *cmd: subprocess.run(cmd, cwd=repo, check=True, capture_output=True, text=True).stdout
        repo.mkdir()
        sh("git", "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a\n")
        sh("git", "add", "-A")
        sh("git", "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "base")
        for run_id, added in (("run-flawed", "bad.txt"), ("run-fixed", "good.txt")):  # each run's change, as a patch
            (repo / added).write_text("x\n")
            sh("git", "add", "-A")
            (runs / run_id).mkdir(parents=True)
            (runs / run_id / "diff.patch").write_text(sh("git", "diff", "--cached", "--binary"))
            sh("git", "reset", "-q", "--hard")
        self.target = {"name": "t", "repo": str(repo), "base": "main"}
        self.ran = self.home / "the-check-ran"
        self.cmd = f"touch {self.ran} && test ! -e bad.txt"
        for module, name, value in ((learn, "RULES", self.home / "rules"), (learn, "RUNS", runs), (core, "RUNS", runs),
                                    (core, "LOG", runs / "events.jsonl")):
            patch = mock.patch.object(module, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        learn.save({"id": "rule-human-flawed", "born_from": "run-flawed", "event_id": "evt-1", "text": "no bad.txt",
                    "check": {"type": "script", "cmd": None}, "status": "proposed", "source": "human"})

    def rule(self) -> dict:
        return learn.all_rules()[0]

    def learn_after_fixed_run(self, write_check):
        with contextlib.redirect_stdout(io.StringIO()):
            learn.evaluate_pending(core.Run({"id": "evt-1"}, "run-fixed"), "run-fixed", self.target, write_check)

    def test_the_command_waits_for_a_person_and_runs_nowhere_before(self):
        self.learn_after_fixed_run(lambda rule, run_id: self.cmd)
        rule = self.rule()
        self.assertEqual((rule["status"], rule["check"]["cmd"]), ("proposed", self.cmd))
        self.assertIsNone(learn.check_passes(rule, "run-flawed", self.target))
        with mock.patch.object(gate, "STEPS", []):  # the gate's extra steps: approved script checks only
            self.assertEqual(gate.run_gate(self.home, self.target, {}, [rule], lambda *a: None)["steps"], [])
        self.assertFalse(self.ran.exists())

    def test_once_approved_it_is_evaluated_and_the_rule_turns_active(self):
        self.learn_after_fixed_run(lambda rule, run_id: self.cmd)
        with contextlib.redirect_stdout(io.StringIO()):
            learn.approve(self.rule(), self.target)
        rule = self.rule()
        self.assertTrue(self.ran.exists())
        self.assertEqual(rule["status"], "active")
        self.assertEqual(rule["eval"], {"fails_on": "run-flawed", "passes_on": "run-fixed"})
        with mock.patch.object(gate, "STEPS", []):
            steps = gate.run_gate(self.home, self.target, {}, [rule], lambda *a: None)["steps"]
        self.assertEqual([s["step"] for s in steps], ["rule:rule-human-flawed"])

    def test_the_checker_has_no_tools_and_must_prove_it(self):
        cmd = learn.checker_cmd()
        self.assertEqual((cmd[cmd.index("--tools") + 1], cmd[cmd.index("--setting-sources") + 1]), ("", ""))
        answer = {"type": "result", "result": "test ! -e bad.txt", "total_cost_usd": 0.001}
        for events, expected in (([INIT, answer], "test ! -e bad.txt"), ([{**INIT, "tools": ["Read"]}, answer], None)):
            with mock.patch.dict(os.environ, {"FACTORY_CHECKER_CMD": fake_claude(events) + " -p"}):
                self.assertEqual(learn.agent_writes_check(self.rule(), "run-fixed", trace.run_agent), expected)


if __name__ == "__main__":
    unittest.main()
