"""The builder: a headless agent in a throwaway worktree of the target, seeing only the task."""

import ast
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from .core import DEFAULT_AGENT, git, matches, sh
from .trace import claude_stream_flags, closed, is_claude

TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]  # every tool the builder has; no connector, no skill


def test_prefix(target: dict) -> str:
    """The start of the target's test command that the builder may run with arguments of its own: up to its last
    `pytest` word ("uv run pytest -q -rfE" -> "uv run pytest"), or all of it when it has none."""
    words = shlex.split(target["verify"]["test"])
    at = max((i for i, w in enumerate(words) if Path(w).name in ("pytest", "py.test")), default=len(words) - 1)
    return shlex.join(words[:at + 1])


def allowed_tools(target: dict, wt) -> list[str]:
    """The builder's permission rules: files inside its worktree only (a Read rule also covers Glob and Grep, an
    Edit rule also covers Write), and four shell commands. With `dontAsk`, anything else is denied, except the
    few commands Claude Code itself counts as read-only; a command that names a path outside is denied too."""
    inside = f"/{Path(wt).resolve()}/**"  # in a rule, `//` starts an absolute path
    return [f"Read({inside})", f"Edit({inside})", f"Bash({test_prefix(target)}:*)", "Bash(git diff:*)",
            "Bash(git status:*)", "Bash(ls:*)"]


def agent_cmd(target: dict, wt) -> list[str]:
    """FACTORY_AGENT_CMD as given; for `claude`, what the engine depends on is added: the stream, a budget, only
    the builder's tools, nothing of the user's account (connectors, skills, settings, memory) and, unless the
    command chose a permission mode, rules that keep it inside its worktree."""
    cmd = shlex.split(os.environ.get("FACTORY_AGENT_CMD") or DEFAULT_AGENT)
    if not is_claude(cmd):
        return cmd
    if "-p" not in cmd and "--print" not in cmd:
        cmd.append("-p")
    cmd = claude_stream_flags(cmd)
    if "--model" not in cmd and os.environ.get("FACTORY_AGENT_MODEL"):
        cmd += ["--model", os.environ["FACTORY_AGENT_MODEL"]]
    if "--permission-mode" not in cmd:
        cmd += ["--permission-mode", "dontAsk", "--allowedTools", *allowed_tools(target, wt)]
    if "--max-budget-usd" not in cmd:
        cmd += ["--max-budget-usd", os.environ.get("FACTORY_AGENT_BUDGET", "2")]
    return closed(cmd, TOOLS) + ["--no-session-persistence"]


def make_worktree(target: dict, path: Path) -> str:
    repo = target["repo"]
    if path.exists():
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    git(repo, "worktree", "add", "-q", "--detach", str(path), target["base"])
    for step in target.get("setup", []):
        sh(step, cwd=path)
    return git(path, "rev-parse", "HEAD").strip()


def remove_worktree(target: dict, path: Path):
    git(target["repo"], "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)


def prompt_for(task: dict, target: dict, rules: list[dict], feedback: str | None = None) -> str:
    lines = [
        f"You are the builder in a software factory, working in a git worktree of {target['name']}.",
        "Do exactly the task below and nothing else.", "", "TASK:", json.dumps(task, indent=2, ensure_ascii=False), "",
        "How to work:",
        "- Change only files matching allowed_paths. Never touch forbidden_paths: the diff is checked, and touching"
        " one stops the run.",
        "- No network: the tests run offline.",
        f"- Run the tests with: {target['verify']['test']}",
        f"- The shell commands you can run are `{test_prefix(target)}` (with any arguments), `git diff`,"
        " `git status` and `ls`. Files and paths: only inside this worktree.",
        "- One command per Bash call. Don't chain with `&&`, `;` or `|`, don't `cd`, don't redirect: one part that"
        " isn't on that list gets the whole call denied.",
        "- A denied call doesn't turn Bash off: carry on with the commands above.",
        "- You may add tests. Don't edit or delete existing assertions.",
        "- Don't commit; the factory commits for you.",
        "- When done, write .factory/commit-message.txt: a short subject line, a blank line, and a body that"
        " claims only what you actually ran.",
        "- If the task can't be done without something you don't have (network, real data from an outside service,"
        " a container, credentials, or a forbidden path), don't guess or work around it: write .factory/stop.json as"
        ' {"state": "blocked" or "manual", "need": "a short slug", "why": "one or two sentences"} and stop.'
        ' "blocked": the factory lacks something (need: real-data, container, credentials, ...).'
        ' "manual": the change belongs to the maintainer (need: forbidden-path, product-decision, ...).',
    ]
    lines += [f"- {note}" for note in target.get("builder", {}).get("notes", [])]
    if rules:
        lines += ["", "Rules this factory learned from earlier runs (each one is enforced):"]
        lines += [f"- {r['text']}" for r in rules]
    if feedback:
        lines += ["", "A previous attempt at this task was rejected. What the verifier found:", feedback]
    return "\n".join(lines) + "\n"


def collect(wt: Path) -> dict:
    """Read the builder's notes out of .factory/, then stage everything else."""
    notes = {}
    d = wt / ".factory"
    if (d / "stop.json").exists():
        try:
            notes["stop"] = json.loads((d / "stop.json").read_text())
        except ValueError:
            notes["stop"] = {"state": "blocked", "need": "unreadable stop.json", "why": (d / "stop.json").read_text()}
    if (d / "commit-message.txt").exists():
        notes["message"] = (d / "commit-message.txt").read_text().strip()
    shutil.rmtree(d, ignore_errors=True)
    git(wt, "add", "-A")
    notes["files"] = [f for f in git(wt, "diff", "--cached", "--name-only").split("\n") if f]
    return notes


def scope_check(wt: Path, files: list[str], task: dict, target: dict) -> dict | None:
    """The diff against forbidden paths, allowed paths, existing assertions and maintainer intent."""
    hit = [f for f in files if matches(f, task["forbidden_paths"])]
    if hit:
        return {"state": "manual", "reason": "protected-path", "paths": hit,
                "detail": f"the change touches protected paths: {', '.join(hit)}"}
    outside = [f for f in files if not matches(f, task["allowed_paths"])]
    if outside:
        return {"state": "manual", "reason": "outside-allowed-paths", "paths": outside,
                "detail": f"the change writes outside its allowed paths: {', '.join(outside)}"}
    tests_dir = target["verify"]["tests_dir"] + "/"
    for f in files:
        if f.startswith(tests_dir) and _base_has(wt, target["base"], f):
            removed = [l for l in git(wt, "diff", "--cached", "-U0", target["base"], "--", f).splitlines()
                       if l.startswith("-") and not l.startswith("---") and "assert" in l]
            if removed:
                return {"state": "manual", "reason": "edits-existing-assertion", "paths": [f],
                        "detail": f"the change edits existing assertions in {f}"}
    for intent in target.get("intent", []):
        if intent["path"] in files and _touches_marker(wt, target["base"], intent):
            return {"state": "manual", "reason": "maintainer-intent", "intent": intent["id"],
                    "detail": f"the change rewrites a maintainer decision: {intent['why']}"}
    return None


def _base_has(wt, base, path) -> bool:
    return bool(git(wt, "ls-tree", base, "--", path).strip())


def changed_base_lines(wt, base, path) -> set[int]:
    """Lines of the base version of `path` that the staged change removes or edits (an insertion counts the
    lines around it)."""
    out = git(wt, "diff", "--cached", "-U0", base, "--", path)
    lines = set()
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? ", out, re.M):
        start, count = int(m[1]), int(m[2] if m[2] is not None else 1)
        lines |= set(range(start, start + count)) if count else {start, start + 1}
    return lines


def _touches_marker(wt, base, intent) -> bool:
    source = git(wt, "show", f"{base}:{intent['path']}", check=False)
    at = next((i for i, l in enumerate(source.splitlines(), 1) if intent["marker"] in l), None)
    if at is None:
        return False
    region = _region(source, at, intent["path"])
    return bool(region & changed_base_lines(wt, base, intent["path"]))


def _region(source: str, at: int, path: str) -> set[int]:
    """The def holding the marker (Python), or its paragraph (anything else)."""
    if path.endswith(".py"):
        best = None
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.lineno <= at <= node.end_lineno:
                if best is None or node.lineno > best.lineno:
                    best = node
        if best:
            return set(range(best.lineno, best.end_lineno + 1))
        return {at}
    lines = source.splitlines()
    lo = hi = at
    while lo > 1 and lines[lo - 2].strip():
        lo -= 1
    while hi < len(lines) and lines[hi].strip():
        hi += 1
    return set(range(lo, hi + 1))
