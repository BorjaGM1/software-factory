"""The human role, played by an agent: the maintainer's reviewer approves or rejects a change that passed the
gate, and answers the question a manual or blocked run asks. Its own prompt and process, no tools and nothing of
the account that runs it (connectors, skills, settings, memory); it sees only what it is shown here, never the
builder's reasoning or an event's `untrusted` text. Each call has to prove it had no tools, or its decision is not
used."""

import json
import os
import re
from pathlib import Path

from .trace import is_claude, no_tools_cmd, run_agent

ROLE = ("You play the human reviewer for the maintainer of {name}, an open-source project. A software factory"
        " works on it, and some decisions are yours. Decide only from what is shown below; when it doesn't show"
        " enough to say yes, say no. Saying no is often right: a wrong yes ships a mistake under the maintainer's"
        " name.")

ANSWER = ('Reply with one JSON object and nothing else: {{"decision": "{yes}" or "{no}", "reason": "one to three'
          ' sentences, addressed to the factory"}}.')


def reviewer_cmd() -> list[str]:
    """FACTORY_REVIEWER_CMD (default `claude -p`); for `claude`: no tools, no connectors, skills or settings of the
    user, a small budget, and FACTORY_REVIEWER_MODEL (default sonnet; "" for the command's own default)."""
    # haiku invented facts to reject a sound change; one call per decision, so a stronger model costs cents
    return no_tools_cmd(os.environ.get("FACTORY_REVIEWER_CMD") or "claude -p",
                        os.environ.get("FACTORY_REVIEWER_MODEL", "sonnet"),
                        os.environ.get("FACTORY_REVIEWER_BUDGET", "0.5"))


def enabled() -> bool:
    return os.environ.get("FACTORY_REVIEWER", "on") != "off"


def _intent(target: dict) -> str:
    lines = [f"- {i['why']} ({i['path']})" for i in target.get("intent", [])]
    return "What the maintainer decided on purpose:\n" + "\n".join(lines) if lines else ""


def _ask(prompt: str, run_dir: Path, name: str, yes: str, no: str, runner=None) -> dict:
    """{decision, reason, cost_usd, model}; decision None when the reply can't be read, or when the reviewer
    didn't prove it ran with no tools (`unverified`)."""
    cwd = run_dir / "review"  # only its own prompts and replies: nothing to read even if a tool slipped through
    cwd.mkdir(exist_ok=True)
    (cwd / f"{name}-prompt.txt").write_text(prompt)
    cmd = reviewer_cmd()
    reply = (runner or run_agent)(prompt, cwd=cwd, timeout=600, cmd=cmd, tools=[] if is_claude(cmd) else None)
    (cwd / f"{name}-reply.json").write_text(json.dumps(reply, indent=2, ensure_ascii=False))
    if reply.get("unverified"):
        return {"decision": None, "reason": f"the reviewer could not be proven tool-free: {reply['unverified']}",
                "unverified": True, "cost_usd": reply["cost_usd"], "model": None, "usage": reply.get("usage")}
    found = re.search(r"\{.*\}", reply["text"], re.S)
    try:
        answer = json.loads(found[0]) if found else {}
    except ValueError:
        answer = {}
    decision = answer.get("decision") if answer.get("decision") in (yes, no) else None
    return {"decision": decision, "reason": str(answer.get("reason") or reply["text"][-300:]).strip(),
            "cost_usd": reply["cost_usd"], "model": reply.get("model"), "usage": reply.get("usage")}


def review_change(task: dict, diff: str, gate: dict, target: dict, run_dir: Path, runner=None) -> dict:
    """approve | request-changes, for a change that passed the gate."""
    steps = "\n".join(f"- {s['step']}: {'ok' if s['ok'] else 'FAILED'}: {s['detail']}" for s in gate["steps"])
    prompt = "\n\n".join(filter(None, [
        ROLE.format(name=target["name"]),
        "A builder agent made the change below for this task, and the factory's verifier gate passed it. Review it"
        " as the maintainer would before merging: does it do what the task asks, without breaking or quietly"
        " changing anything else, is the test really about the bug, and does the commit message tell the truth?"
        " Approve only if you would merge it as it is. Request changes if something should be different, and say"
        " exactly what, so a builder can act on it. Don't ask for changes of taste. The gate's results are facts:"
        " it ran the whole suite, the new tests against the old code, the clock moved ahead and the mutants. Never"
        " contradict them from reading the code (if the suite passed, every test in it passed, whatever its date"
        " looks like); judge what they can't see: whether the change is the right one.",
        "TASK:\n" + json.dumps({k: task[k] for k in ("type", "summary", "acceptance") if k in task}, indent=2,
                               ensure_ascii=False),
        _intent(target),
        "GATE (deterministic checks, already run):\n" + steps,
        "THE CHANGE (commit message and diff):\n" + diff[:60000],
        ANSWER.format(yes="approve", no="request-changes"),
    ]))
    return _ask(prompt, run_dir, "change", "approve", "request-changes", runner)


def answer_stop(state: str, question: str, context: dict, target: dict, run_dir: Path, runner=None) -> dict:
    """yes | no, to the question a manual or blocked run asks the human."""
    what = {"manual": "The factory stopped because this decision belongs to the maintainer.",
            "blocked": "The factory stopped because it lacks something it needs; only a person can provide it."}[state]
    prompt = "\n\n".join(filter(None, [
        ROLE.format(name=target["name"]),
        what + " Answer its question. Say yes only if you, as the maintainer, would really give that go-ahead or"
        " provide that thing now, from what is shown; otherwise say no and what would change your answer.",
        "WHAT THE FACTORY WAS DOING:\n" + json.dumps(context, indent=2, ensure_ascii=False)[:20000],
        _intent(target),
        "THE FACTORY'S QUESTION:\n" + question,
        ANSWER.format(yes="yes", no="no"),
    ]))
    return _ask(prompt, run_dir, "stop", "yes", "no", runner)


def question_for(state: str, reason: str, detail: str, data: dict) -> str:
    """The question a stopped run asks, in the factory's words."""
    need = data.get("need") or data.get("intent") or reason
    if state == "blocked":
        return f"{detail}\nWe need: {need}. Can you provide it?"
    paths = ", ".join(data.get("paths", []))
    return f"{detail}\n" + (f"This touches {paths}. " if paths else "") + "Go ahead with this change?"
