"""The learning loop. A flawed run or a human correction proposes a rule; it becomes active only when its
check fails on the flawed run and passes on the corrected one ("eval first"). A check that is a shell command
written by a model runs nowhere, not even in that eval, until a person approves it (`factory approve`)."""

import json
import os

from .core import RULES, RUNS, Run, now, sh
from .trace import is_claude, no_tools_cmd

# What a failed gate step teaches the builder. The check of each lesson is the gate step itself.
LESSONS = {
    "new-tests-fail-on-base": "Write the test first and run it against the base source, before you touch the"
                              " code it tests: every new test must fail on the base, on its assertion, and pass"
                              " after the change. A test that passes before and after proves nothing.",
    "mutants": "For each condition you add or change, make sure some test fails if it is flipped or removed"
               " (== for !=, < for <=, and for or, [0] for [-1]).",
    "base-tests": "The existing tests must keep passing against your source unchanged; don't change behaviour"
                  " they pin down.",
    "clock": "Never depend on today's date or a fixed date in tests or code paths: the suite must pass with the"
             " clock moved 1, 92 and 730 days ahead.",
    "startup": "The CLI must start with no system time zone data (PYTHONTZPATH=\"\") and no network.",
    "message": "The commit message claims only what you ran, with the numbers the run printed; never claim a"
               " check against a live service or in a container.",
    "suite": "Run the full offline suite before you finish; it must have no failures the base doesn't have.",
}


def all_rules() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(RULES.glob("*.json"))]


def active() -> list[dict]:
    return [r for r in all_rules() if r["status"] == "active"]


def save(rule: dict):
    RULES.mkdir(exist_ok=True)
    (RULES / f"{rule['id']}.json").write_text(json.dumps(rule, indent=2, ensure_ascii=False) + "\n")


def propose_from_gate(run, gate: dict) -> list[dict]:
    """One proposed rule per failed gate step that has a lesson, unless a rule with that check exists."""
    existing = {json.dumps(r["check"], sort_keys=True): r for r in all_rules()}
    proposed = []
    for step in gate["steps"]:
        if step["ok"] or step["step"] not in LESSONS:
            continue
        check = {"type": "gate-step", "step": step["step"]}
        key = json.dumps(check, sort_keys=True)
        if key in existing:
            rule = existing[key]
            rule.setdefault("seen_in", []).append(run.id)
            save(rule)
            run.log("learn", "check", "info", f"{rule['id']} ({rule['status']}) seen again: {step['step']}",
                    {"rule": rule["id"], "status": rule["status"], "born_from": rule["born_from"]})
            continue
        rule = {"id": f"rule-{step['step']}", "born_from": run.id, "event_id": run.event["id"],
                "text": LESSONS[step["step"]], "check": check, "status": "proposed", "created_at": now(),
                "eval": {"fails_on": run.id if not step["ok"] else None, "passes_on": None}}
        save(rule)
        proposed.append(rule)
        run.log("learn", "check", "info", f"proposed {rule['id']}: {step['detail'][:120]}",
                {"rule": rule["id"], "status": "proposed", "text": rule["text"], "check": check, "born_from": run.id})
    return proposed


def propose_from_correction(run, flawed_run_id: str, text: str) -> dict:
    """A human correction. Its check is the gate steps the flawed run failed, or else a script written later
    by an agent from the correction and the two diffs."""
    gate = _gate_of(flawed_run_id) or {"steps": []}
    failed = [s["step"] for s in gate["steps"] if not s["ok"]]
    check = {"type": "gate-step", "step": failed[0]} if failed else {"type": "script", "cmd": None}
    rule = {"id": f"rule-human-{flawed_run_id.removeprefix('run-')}", "born_from": flawed_run_id,
            "event_id": run.event["id"], "text": text, "check": check, "status": "proposed", "created_at": now(),
            "eval": {"fails_on": None, "passes_on": None}, "source": "human"}
    save(rule)
    run.log("learn", "human", "info", f"proposed {rule['id']} from a human correction", {"rule": rule["id"],
            "status": "proposed", "text": text, "check": check, "born_from": flawed_run_id})
    return rule


def evaluate_pending(run, corrected_run_id: str, target: dict, write_check=None):
    """After a successful run: try to activate the proposed rules born from runs of the same event."""
    for rule in all_rules():
        if rule["status"] != "proposed" or rule["event_id"] != run.event["id"]:
            continue
        check = rule["check"]
        if check["type"] == "script" and not check.get("cmd") and write_check:
            check["cmd"], check["written_for"] = write_check(rule, corrected_run_id), corrected_run_id
            save(rule)
        if check["type"] == "script" and not check.get("approved_at"):
            if check.get("cmd"):
                run.log("learn", "check", "info", f"{rule['id']}: a model wrote its check; nothing runs it until a"
                        f" person approves it (factory approve {rule['id']}) -> stays proposed",
                        {"rule": rule["id"], "status": "proposed", "text": rule["text"], "check": check,
                         "born_from": rule["born_from"]})
            continue
        _evaluate(rule, corrected_run_id, target, run.log, "check")


def _evaluate(rule: dict, corrected_run_id: str, target: dict, log, actor: str):
    fails = check_passes(rule, rule["born_from"], target) is False
    passes = check_passes(rule, corrected_run_id, target) is True
    rule["eval"] = {"fails_on": rule["born_from"] if fails else None, "passes_on": corrected_run_id if passes else None}
    if fails and passes:
        rule["status"] = "active"
        rule["activated_at"] = now()
    save(rule)
    log("learn", actor, "ok" if rule["status"] == "active" else "info",
        f"{rule['id']}: check fails on {rule['born_from']}: {fails}; passes on {corrected_run_id}: {passes}"
        + (" -> active" if rule["status"] == "active" else " -> stays proposed"),
        {"rule": rule["id"], "status": rule["status"], "eval": rule["eval"], "text": rule["text"],
         "born_from": rule["born_from"]})


def approve(rule: dict, target: dict):
    """A person has read the command of this rule's check and lets it run: in the gate of every run once the rule
    is active, and first in the eval that decides whether it becomes active."""
    check = rule["check"]
    check["approved_at"] = now()
    save(rule)
    if rule["status"] == "proposed" and check.get("written_for"):
        run = Run({"id": rule["event_id"]}, check["written_for"])  # logged under the run it was written for
        _evaluate(rule, check["written_for"], target, run.log, "human")


def check_passes(rule: dict, run_id: str, target: dict) -> bool | None:
    """Does the rule's check pass on that run? None when it can't be told."""
    check = rule["check"]
    if check["type"] == "gate-step":
        gate = _gate_of(run_id)
        step = next((s for s in (gate or {}).get("steps", []) if s["step"] == check["step"]), None)
        return None if step is None else step["ok"]
    if check["type"] == "script" and check.get("cmd") and check.get("approved_at"):
        return _script_on_run(check["cmd"], run_id, target)
    return None


def _gate_of(run_id: str) -> dict | None:
    p = RUNS / run_id / "gate.json"
    return json.loads(p.read_text()) if p.exists() else None


def _script_on_run(cmd: str, run_id: str, target: dict) -> bool | None:
    """Recreate the run's change on a fresh worktree of the base and run the check there."""
    from .builder import make_worktree, remove_worktree
    patch = RUNS / run_id / "diff.patch"
    if not patch.exists():
        return None
    wt = RUNS / run_id / "wt-check"
    make_worktree(target, wt)
    try:
        if sh(["git", "apply", str(patch)], cwd=wt)[0]:
            return None
        return sh(cmd, cwd=wt, timeout=300)[0] == 0
    finally:
        remove_worktree(target, wt)


def checker_cmd() -> list[str]:
    """FACTORY_CHECKER_CMD (default `claude -p`); for `claude`: no tools, no connectors, skills or settings of the
    user, a small budget, and FACTORY_CHECKER_MODEL (default haiku)."""
    return no_tools_cmd(os.environ.get("FACTORY_CHECKER_CMD") or "claude -p",
                        os.environ.get("FACTORY_CHECKER_MODEL", "haiku"),
                        os.environ.get("FACTORY_CHECKER_BUDGET", "0.5"))


def agent_writes_check(rule: dict, corrected_run_id: str, run_agent) -> str | None:
    """Ask a cheap agent with no tools for a shell check (exit 0 = rule respected) from the correction and both
    diffs. What it answers is a proposal: `factory approve` shows it to a person before anything runs it."""
    flawed = (RUNS / rule["born_from"] / "diff.patch").read_text()[:12000]
    fixed = (RUNS / corrected_run_id / "diff.patch").read_text()[:12000]
    prompt = (
        "A human rejected a code change with this correction:\n" + rule["text"] + "\n\n"
        "Write ONE shell command, run from the repository root with the change applied, that exits non-zero for"
        " the rejected change and 0 for the corrected one, and that enforces the correction in general (not just"
        " these files' exact text). It must also exit 0 on the unchanged base repository: judge only what a change"
        " adds (for example via `git diff main`), never code that was already there. Prefer grep/git/uv run pytest."
        " No network. Reply with the command only, on one"
        " line, no code fences.\n\nREJECTED DIFF:\n" + flawed + "\n\nCORRECTED DIFF:\n" + fixed)
    cwd = RUNS / corrected_run_id / "checker"  # empty: nothing to read even if a tool slipped through
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = checker_cmd()
    r = run_agent(prompt, cwd=cwd, cmd=cmd, tools=[] if is_claude(cmd) else None)
    rule.setdefault("cost_usd", 0)
    rule["cost_usd"] += r["cost_usd"]
    return r["text"].strip().strip("`").splitlines()[-1].strip() if r["text"].strip() else None
