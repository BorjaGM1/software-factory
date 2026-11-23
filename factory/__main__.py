"""factory run EVENT.json | factory watch | factory retry RUN_ID | factory correct RUN_ID "text" | factory rules
| factory approve RULE_ID"""

import argparse
import json
import os
import shlex
import shutil
import sys
import time
import tomllib
from pathlib import Path

from . import engine, learn, reviewer
from .core import DEFAULT_AGENT, INBOX, LOG, load_target, sh


def done_events() -> set[str]:
    if not LOG.exists():
        return set()
    lines = (json.loads(l) for l in LOG.read_text().splitlines() if l.strip())
    return {l["event_id"] for l in lines if l["step"] == "outcome"}


def main():
    p = argparse.ArgumentParser(prog="factory")
    p.add_argument("--target", default=None, help="path to a target.toml (default: targets/renfe-cli)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one event file through the process")
    r.add_argument("event")
    r.add_argument("--retries", type=int, default=0, help="re-run a flawed outcome with the gate's findings")
    w = sub.add_parser("watch", help="run every new event that lands in runs/inbox/")
    w.add_argument("--interval", type=float, default=5)
    w.add_argument("--retries", type=int, default=0)
    t = sub.add_parser("retry", help="run a finished run's event again, with what the gate found")
    t.add_argument("run_id")
    c = sub.add_parser("correct", help="a human correction of a run: proposes a rule and re-runs the event")
    c.add_argument("run_id")
    c.add_argument("text")
    sub.add_parser("rules", help="list the learned rules")
    ap = sub.add_parser("approve", help="read the shell check a model wrote for a rule, and let it run")
    ap.add_argument("rule_id")
    ap.add_argument("--yes", action="store_true", help="approve without being asked")
    k = sub.add_parser("costs", help="what the runs cost: totals, by state, agent and event")
    k.add_argument("--log", default=None, help="a log to read instead of runs/events.jsonl (e.g. a fixture)")
    f = sub.add_parser("fixture", help="copy the log lines of some runs to runs/fixtures/NAME.jsonl")
    f.add_argument("name")
    f.add_argument("run_ids", nargs="+")
    g = sub.add_parser("gate", help="run the verifier gate alone on an existing commit of the target (read-only)")
    g.add_argument("ref")
    a = p.parse_args()
    try:
        target = load_target(a.target) if a.target else load_target()
    except (OSError, KeyError, tomllib.TOMLDecodeError) as e:
        why = f"it has no `{e.args[0]}`" if isinstance(e, KeyError) else str(e)
        sys.exit(f"factory: can't read the target {a.target or 'targets/renfe-cli/target.toml'}: {why}")
    if a.cmd in ("run", "watch", "retry", "correct", "gate", "approve"):
        preflight(target, agents=a.cmd not in ("gate", "approve"))

    if a.cmd == "run":
        run_with_retries(engine.load_event(a.event), target, a.retries)
    elif a.cmd == "watch":
        print(f"watching {INBOX}")
        while True:
            seen = done_events()
            for path in sorted(INBOX.glob("*.json")):
                try:
                    event = engine.load_event(path)
                except (ValueError, OSError) as e:
                    print(f"skipping {path.name}: {e}")
                    continue
                if event["id"] not in seen:
                    run_with_retries(event, target, a.retries)
                    seen.add(event["id"])
            time.sleep(a.interval)
    elif a.cmd == "retry":
        engine.retry(a.run_id, target)
    elif a.cmd == "correct":
        engine.correct(a.run_id, a.text, target)
    elif a.cmd == "fixture":
        lines = [l for l in LOG.read_text().splitlines() if l.strip() and json.loads(l)["run_id"] in a.run_ids]
        out = LOG.parent / "fixtures" / f"{a.name}.jsonl"
        out.parent.mkdir(exist_ok=True)
        out.write_text("\n".join(lines) + "\n")
        print(f"{len(lines)} lines -> {out}")
    elif a.cmd == "gate":
        gate_only(a.ref, target)
    elif a.cmd == "costs":
        from .costs import report
        log = Path(a.log) if a.log else LOG
        if not log.exists():
            sys.exit(f"factory: no runs logged yet ({log}); a sample: factory costs --log runs/fixtures/real-events.jsonl")
        print(report(log))
    elif a.cmd == "rules":
        for rule in learn.all_rules():
            print(f"{rule['status']:9} {rule['id']:32} born from {rule['born_from']}\n          {rule['text']}")
            check = rule["check"]
            if check["type"] == "script" and check.get("cmd"):
                print(f"          check: {shown(check['cmd'])}\n          "
                      + ("approved by a person" if check.get("approved_at") else
                         f"NOT approved, so it never runs: factory approve {rule['id']}"))
    elif a.cmd == "approve":
        rule = next((r for r in learn.all_rules() if r["id"] == a.rule_id), None)
        if not rule or rule["check"]["type"] != "script" or not rule["check"].get("cmd"):
            sys.exit(f"factory: no rule '{a.rule_id}' with a shell check to approve (see: factory rules)")
        print(f"{rule['id']} ({rule['status']}): {rule['text']}\n\nIts check is this shell command. Once approved it"
              f" runs in the worktree of the change, in this rule's eval\nand then in the gate of every run:\n\n"
              f"    {shown(rule['check']['cmd'])}\n")
        if not a.yes:
            try:
                answer = input("Let it run? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                sys.exit("not approved")
        learn.approve(rule, target)
        print(f"{rule['id']}: approved, {rule['status']}")


def shown(cmd: str) -> str:
    """A command as a person has to read it before approving it: whatever a terminal would act on or hide
    (escape sequences, carriage returns, direction marks) is written out."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}" for c in cmd)


def preflight(target: dict, agents: bool):
    """Say in one message what this machine lacks for a run, instead of failing halfway through one."""
    lacks = [f"{target['file']} has no `{k}`" for k in ("name", "base", "allowed_paths") if k not in target]
    lacks += [f"{target['file']} has no `verify.{k}`" for k in ("test", "tests_dir", "src_dir")
              if k not in target["verify"]]
    lacks += [f"{target['file']} has no `triage.kinds`"] if "kinds" not in target["triage"] else []
    if not lacks:
        programs = ["git", target["verify"]["test"]]  # run as they are
        if agents:
            programs.append(os.environ.get("FACTORY_AGENT_CMD") or DEFAULT_AGENT)
            programs += [os.environ.get("FACTORY_REVIEWER_CMD") or "claude -p"] if reviewer.enabled() else []
        missing = [w for w in (shlex.split(c)[0] for c in programs) if not shutil.which(w)]
        for command in [*target.get("setup", []), *target["verify"]["lock"]]:  # run by the shell
            first = next((w for w in shlex.split(command) if "=" not in w), "")
            missing += [first] if first and sh(f"command -v {shlex.quote(first)}")[0] else []
        lacks += [f"`{program}` is not on PATH" for program in dict.fromkeys(missing)]
    if not lacks:
        repo, base = target["repo"], target["base"]
        if sh(["git", "-C", repo, "rev-parse", "-q", "--verify", base + "^{commit}"])[0]:
            lacks.append(f"{repo} is not a git repository with a `{base}` (`repo` and `base` of {target['file']})"
                         + (f". Get it with: git clone {target['source']} {repo}" if target.get("source") else ""))
        elif not target.get("commit_author") and sh(["git", "-C", repo, "config", "user.email"])[0]:
            lacks.append("git has no user.name and user.email for the factory's commits (`git config --global`, or"
                         " `commit_author` in target.toml)")
    if lacks:
        sys.exit("factory: can't run yet:\n" + "".join(f"  - {l}\n" for l in lacks) + "See README.md.")


def gate_only(ref, target):
    """The gate on a commit someone else made (like factory/prototype/gate.sh BRANCH). Prints, logs nothing."""
    from .builder import make_worktree, remove_worktree
    from .core import RUNS, git
    from .gate import baseline, run_gate
    run_dir = RUNS / "gate-only" / ref.replace("/", "-")
    run_dir.mkdir(parents=True, exist_ok=True)
    wt = run_dir / "wt"
    try:
        make_worktree(target, wt)
        base = baseline(wt, target)
        git(wt, "checkout", "-q", "--detach", ref)
        files = [f for f in git(wt, "diff", "--name-only", f"{target['base']}...HEAD").split("\n") if f]
        ctx = {"files": files, "base": base, "task": {"type": "fix"}, "run_dir": run_dir}
        gate = run_gate(wt, target, ctx, learn.active(), lambda step, actor, status, detail, data=None:
                        print(f"[{status:6}] {detail}", flush=True))
        (run_dir / "gate.json").write_text(json.dumps(gate, indent=2, ensure_ascii=False, default=str))
        print("PASS" if gate["ok"] else "FAIL", "(needs maintainer)" if gate["needs_maintainer"] else "")
    finally:
        remove_worktree(target, wt)


def run_with_retries(event, target, retries):
    """A flawed run is retried up to `retries` times with the gate's findings; a change the reviewer rejected is
    retried once with its correction, as `factory correct` would."""
    run, human = engine.process(event, target), 1
    while True:
        outcome = json.loads((run.dir / "outcome.json").read_text())
        if outcome["state"] != "flawed":
            break
        if outcome["reason"] == "human-rejected" and human:
            human -= 1
        elif retries:
            retries -= 1
        else:
            break
        run = engine.retry(run.id, target)


if __name__ == "__main__":
    main()
