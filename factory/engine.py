"""One event through the process: intake -> triage -> plan -> build -> verify -> outcome (-> learn)."""

import json
import re
from pathlib import Path

from . import learn, reviewer, visible
from .builder import TOOLS, agent_cmd, allowed_tools, collect, make_worktree, prompt_for, remove_worktree, scope_check
from .trace import describe, is_claude, run_agent
from .core import RUNS, Run, git, load_target, matches, sh
from .gate import baseline, run_gate
from .triage import triage

TRAILERS = re.compile(r"(?im)^\s*(co-authored-by:.*|.*generated with .*|🤖.*)$\n?")


def process(event: dict, target: dict | None = None, *, retry_of: str | None = None, feedback: str | None = None,
            correction: dict | None = None) -> Run:
    target = target or load_target()
    run = Run(event)
    (run.dir / "event.json").write_text(json.dumps(event, indent=2, ensure_ascii=False))
    if not run.logged("intake"):  # the feedback intake logs its own intake line under the same run_id
        run.log("intake", "intake", "info", event.get("title", event["id"]),
                {"title": event.get("title"), "source": event.get("source"), "kind": event.get("kind"), "retry_of": retry_of,
                 "untrusted": event.get("untrusted") is not None})
    if correction:
        run.log("review", "human", "failed", f"correction of {correction['run_id']}: {correction['text']}",
                {**correction, "decision": "request-changes", "reason": correction["text"]})
        learn.propose_from_correction(run, correction["run_id"], correction["text"])
    try:
        _process(run, event, target, retry_of, feedback, correction)
    except Exception as e:  # the factory itself broke: say so, never leave a run without an outcome
        run.outcome("blocked", "factory-error", f"the factory failed: {type(e).__name__}: {e}")
        raise
    return run


def _process(run, event, target, retry_of, feedback, correction):
    task, early = triage(event, target)
    if early:
        detail = early.pop("detail")
        run.log("triage", "check", "info", detail, early)
        seen = {"event": {k: event.get(k) for k in ("source", "kind", "title", "evidence")}}  # never `untrusted`
        _end(run, target, seen, early.pop("state"), early.pop("reason"), f"stopped at triage: {detail}", **early)
        return
    run.log("triage", "check", "ok", f"route '{event.get('kind')}' -> {task['type']}", {"task_type": task["type"]})
    (run.dir / "task.json").write_text(json.dumps(task, indent=2, ensure_ascii=False))
    rules = learn.active()
    run.log("plan", "check", "ok", f"task {task['id']}: {task['summary'].splitlines()[0][:100]}",
            {"task": task, "active_rules": [r["id"] for r in rules]})

    wt = run.dir / "wt"
    try:
        base_sha = make_worktree(target, wt)
        base = baseline(wt, target)
        run.log("verify", "check", "info", f"baseline of {target['base']} ({base_sha[:7]}): {len(base['failed'])} failing"
                f" test(s), {len(base['ids'])} collected" + (" (cached)" if base.get("cached") else ""),
                {"base_failed": base["failed"], "cached": base.get("cached", False)})
        _build_and_verify(run, task, target, wt, base, rules, feedback, retry_of)
    finally:
        remove_worktree(target, wt)


def _build_and_verify(run, task, target, wt, base, rules, feedback, retry_of):
    prompt = prompt_for(task, target, rules, feedback)
    (run.dir / "prompt.txt").write_text(prompt)
    run.log("build", "agent", "started", f"builder on {task['id']}" + (f" (retry of {retry_of})" if retry_of else ""))
    cmd = agent_cmd(target, wt)
    agent = _agent(run, "builder", prompt, wt, cmd, tools=TOOLS if is_claude(cmd) else None,
                   rules=allowed_tools(target, wt))
    (run.dir / "agent.json").write_text(json.dumps(agent, indent=2, ensure_ascii=False))
    if agent.get("unverified"):  # it had, or called, a tool that isn't the builder's: nothing it wrote is kept
        run.outcome("blocked", "builder-unverified", f"the builder was stopped: {agent['unverified']}"[:300])
        return
    notes = collect(wt)
    stat = {k: agent.get(k) for k in ("cost_usd", "turns", "model", "denials", "exit")}
    run.log("build", "agent", "ok" if agent["ok"] else "failed",
            f"builder done: {len(notes['files'])} files changed, ${agent['cost_usd']:.3f}", {**stat, "files": notes["files"]})

    if "stop" in notes:
        stop = notes["stop"]
        (run.dir / "stop.json").write_text(json.dumps(stop, indent=2, ensure_ascii=False))
        state = stop.get("state") if stop.get("state") in ("blocked", "manual") else "blocked"
        need = re.sub(r"[^a-z0-9]+", "-", str(stop.get("need") or "unknown").lower()).strip("-")[:40]
        words = {w.rstrip(".,:;") for w in re.findall(r"[\w./-]+", f"{stop.get('need')} {stop.get('why')}")}
        protected = sorted(w for w in words if matches(w, task["forbidden_paths"]))
        reason = "protected-path" if state == "manual" and protected else need
        _end(run, target, {"task": _shown(task), "builder": {"need": stop.get("need"), "why": stop.get("why")}},
             state, reason, f"the builder stopped ({state}): {stop.get('why') or stop.get('need')}"[:300],
             need=stop.get("need"), why=stop.get("why"), **({"paths": protected} if protected else {}))
        return
    if not agent["ok"] and not notes["files"]:
        said = agent["text"].strip().splitlines()[-1][:200] if agent["text"].strip() else ""
        run.outcome("blocked", "agent-failed", f"the agent command failed (exit {agent['exit']})"
                    + (f": {said}" if said else ""), tail=agent["text"][-500:])
        return
    if not notes["files"]:
        run.outcome("flawed", "no-change", "the builder changed nothing")
        return
    (run.dir / "diff.patch").write_text(git(wt, "diff", "--cached", "--binary"))
    stop = scope_check(wt, notes["files"], task, target)
    if stop:
        run.log("verify", "check", "failed", stop["detail"], {k: v for k, v in stop.items() if k != "detail"})
        seen = {"task": _shown(task), "files": notes["files"], "diff": (run.dir / "diff.patch").read_text()[:15000]}
        _end(run, target, seen, stop.pop("state"), stop.pop("reason"), stop.pop("detail"), **stop)
        return

    message = TRAILERS.sub("", notes.get("message") or f"{task['summary'].splitlines()[0][:72]}").strip()
    env_id = {}
    if target.get("commit_author"):  # else git's own identity: whoever runs the factory
        name, email = re.match(r"(.*) <(.*)>", target["commit_author"]).groups()
        env_id = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
                  "GIT_COMMITTER_EMAIL": email}
    code, out = sh(["git", "commit", "-q", "--no-verify", "-m", message], cwd=wt, env=env_id)
    if code:
        raise RuntimeError(f"git commit: {out.strip()}")
    run.log("build", "check", "ok", f"committed: {message.splitlines()[0][:100]}",
            {"stat": git(wt, "diff", "--shortstat", target["base"], "HEAD").strip()})

    ctx = {"files": notes["files"], "base": base, "task": task, "run_dir": run.dir, "event": run.event}
    gate = run_gate(wt, target, ctx, rules, run.log)
    (run.dir / "gate.json").write_text(json.dumps(gate, indent=2, ensure_ascii=False, default=str))
    failed = [s["step"] for s in gate["steps"] if not s["ok"]]
    run.log("verify", "check", "ok" if gate["ok"] else "failed",
            "gate passed" if gate["ok"] else f"gate failed: {', '.join(failed)}",
            {"steps": [{k: s[k] for k in ("step", "ok", "detail", "secs")} for s in gate["steps"]]})

    if not gate["ok"]:
        run.outcome("flawed", "gate-" + failed[0], f"the gate rejected the change: {', '.join(failed)}", failed=failed)
        learn.propose_from_gate(run, gate)
        return
    change = git(wt, "show", "--format=%B", "HEAD")
    if gate["needs_maintainer"]:
        sweep = next((s for s in gate["steps"] if s["step"] == "sweep"), {})
        _end(run, target, {"task": _shown(task), "sweep": sweep.get("undeclared"), "change": change[:15000]},
             "manual", "ranking-change-needs-maintainer",
             "the gate passed, but the change resolves station queries differently: the maintainer decides")
        return
    if reviewer.enabled():
        r = reviewer.review_change(task, change, gate, target, run.dir, _reviewer(run))
        _log_review(run, r)
        if r["decision"] != "approve":
            if r["decision"] == "request-changes":
                run.outcome("flawed", "human-rejected", f"the reviewer asked for changes: {r['reason']}"[:300],
                            correction=r["reason"], played_by="claude")
            elif r.get("unverified"):
                run.outcome("blocked", "reviewer-unverified", r["reason"][:300])
            else:
                run.outcome("blocked", "reviewer-no-answer", "the reviewer gave no readable decision")
            return
    branch = f"factory/{run.id}"
    git(target["repo"], "branch", "-f", branch, git(wt, "rev-parse", "HEAD").strip())
    run.outcome("success", "gate-passed", f"ready on local branch {branch}", branch=branch,
                **({"decision": "approve"} if reviewer.enabled() else {}))
    learn.evaluate_pending(run, run.id, target,
                           write_check=lambda rule, rid: learn.agent_writes_check(rule, rid, _checker(run)))


def _shown(task: dict) -> dict:
    return {k: task[k] for k in ("type", "summary", "acceptance") if k in task}


def _agent(run, name: str, prompt: str, cwd, cmd, timeout: int = 1500, tools=None, rules=None) -> dict:
    """An agent of this run: its trace in runs/<run>/<name>.trace.jsonl, an `agent` line per tool call as it
    happens, and one with its usage when it ends. With `tools`, that last line also carries what claude's init
    event listed (`data.init`), or says why the agent was stopped without that proof."""
    on_tool = lambda summary, tool: run.log("agent", "agent", "info", summary, {"agent": name, "tool": tool})
    if name == "builder" and visible.enabled():  # the same builder, in a keepty TUI you can watch
        reply = visible.run_agent(prompt, cwd, run.dir, run.id, rules, timeout=timeout, on_tool=on_tool)
    else:
        reply = run_agent(prompt, cwd=cwd, timeout=timeout, cmd=cmd, agent=name,
                          trace=run.dir / f"{name}.trace.jsonl", on_tool=on_tool, tools=tools)
    run.cost += reply["cost_usd"]
    stopped = reply.get("unverified")
    run.log("agent", "agent", "ok" if reply["ok"] else "failed",
            f"{name} stopped, nothing it said is used: {stopped}"[:300] if stopped else describe(reply["usage"]),
            {"agent": name, "usage": reply["usage"], **({"init": reply["init"]} if reply.get("init") else {})})
    return reply


def _reviewer(run):
    return lambda prompt, cwd, timeout, cmd, tools: _agent(run, "reviewer", prompt, cwd, cmd, timeout, tools)


def _checker(run):
    return lambda prompt, cwd, cmd, tools: _agent(run, "checker", prompt, cwd, cmd, 600, tools)


def _log_review(run, answer: dict, question: str | None = None):
    decision = answer["decision"] or "no-answer"
    run.log("review", "human", "ok" if decision in ("approve", "yes") else "failed",
            f"{decision}: {answer['reason']}"[:300],
            {"played_by": "claude", "decision": decision, "reason": answer["reason"], "cost_usd": answer["cost_usd"],
             "model": answer.get("model"), **({"question": question} if question else {})})


def _end(run, target, seen: dict, state: str, reason: str, detail: str, **data):
    """End a run. A manual or blocked one first puts its question to the human role, and keeps its state."""
    if state in ("manual", "blocked") and reviewer.enabled():
        question = reviewer.question_for(state, reason, detail, data)
        answer = reviewer.answer_stop(state, question, seen, target, run.dir, _reviewer(run))
        _log_review(run, answer, question)
        data = {**data, "decision": answer["decision"] or "no-answer", "decision_reason": answer["reason"]}
    run.outcome(state, reason, detail, **data)


def retry(run_id: str, target: dict | None = None, correction_text: str | None = None) -> Run:
    """Run the same event again, telling the builder what the gate (or a human) found in run_id."""
    old = RUNS / run_id
    event = json.loads((old / "event.json").read_text())
    feedback, correction = None, None
    gate = old / "gate.json"
    if gate.exists():
        steps = json.loads(gate.read_text())["steps"]
        feedback = "\n".join(f"- {s['step']}: {s['detail']}" for s in steps if not s["ok"])
    played_by = None
    outcome = json.loads((old / "outcome.json").read_text()) if (old / "outcome.json").exists() else {}
    if not correction_text and outcome.get("reason") == "human-rejected":
        correction_text, played_by = outcome.get("correction"), outcome.get("played_by")
    if correction_text:
        feedback = (feedback + "\n" if feedback else "") + f"- a human reviewer: {correction_text}"
        correction = {"run_id": run_id, "text": correction_text, **({"played_by": played_by} if played_by else {})}
    return process(event, target, retry_of=run_id, feedback=feedback, correction=correction)


def correct(run_id: str, text: str, target: dict | None = None) -> Run:
    """A human correction of a finished run: propose a rule, then run the event again with the correction."""
    return retry(run_id, target, correction_text=text)


def load_event(path: str | Path) -> dict:
    event = json.loads(Path(path).read_text())
    for key in ("id", "source", "kind", "title"):
        if key not in event:
            raise ValueError(f"{path}: event without '{key}'")
    return event
