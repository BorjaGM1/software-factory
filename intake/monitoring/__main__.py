"""Monitoring intake: run the canaries against the twin, turn each failure worth a change into an event.

    python -m intake.monitoring [--mode MODE] [--clock ISO] [--target PATH] [--check]

From the factory's root. Without --target it cuts a throwaway worktree of the target (repo and base from
targets/renfe-cli/target.toml) and deletes it after. With --check it writes nothing and exits 0 only when every
canary passes: the engine can use it to judge a builder's worktree (--target that worktree).

What a failure becomes:
    api-changed         an event, kind api-shape-changed, with acceptance criteria for the builder
    calendar            an event, kind clock-canary (the maintainer's scheduled work; the engine routes it)
    cli-broken          an event, kind bug-report
    renfe-unavailable   no event: the CLI handled the outage as its contract says. One `intake` line and one
                        `outcome` line (success, no-change) in runs/events.jsonl, so it shows on the dashboard.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("FACTORY_HOME", Path(__file__).resolve().parents[2]))
TARGET_DIR = ROOT / "targets" / "renfe-cli"
CANARIES = TARGET_DIR / "canaries" / "run.py"
RUNS = ROOT / "runs"

KINDS = {"api-changed": "api-shape-changed", "calendar": "clock-canary", "cli-broken": "bug-report"}
SOURCES = {"calendar": "canary"}  # the rest come from "monitoring"
# The flow scenario 1 adapts: what it must still print once the parser reads the new shape (from the plain twin).
DRIFT_PROBE = ["search", "Madrid", "Barcelona", "--date", "2027-01-29"]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def target_config() -> dict:
    path = TARGET_DIR / "target.toml"
    doc = tomllib.loads(path.read_text()) if path.exists() else {}
    return {"repo": Path(doc.get("repo", "~/projects/renfe-cli-lab")).expanduser(), "base": doc.get("base", "main")}


class Checkout:
    """The target to test: a given path as it is, or a throwaway worktree of the base, removed on exit."""

    def __init__(self, path: str | None):
        self.given = Path(path).expanduser().resolve() if path else None

    def __enter__(self) -> Path:
        if self.given:
            return self.given
        cfg = target_config()
        self.repo = cfg["repo"]
        if subprocess.run(["git", "-C", str(self.repo), "rev-parse", "-q", "--verify", cfg["base"] + "^{commit}"],
                          capture_output=True).returncode:
            sys.exit(f"monitoring intake: no clone of the target with a `{cfg['base']}` at {self.repo} (see README.md)")
        self.tmp = tempfile.mkdtemp(prefix="canary-target-")
        self.path = Path(self.tmp) / "renfe-cli"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--detach", "-q", str(self.path), cfg["base"]],
                       check=True)
        return self.path

    def __exit__(self, *exc):
        if self.given:
            return
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(self.path)], check=False)
        subprocess.run(["rm", "-rf", self.tmp], check=False)


def run_canaries(target: Path, mode: str, clock: str | None, only: list[str]) -> dict:
    cmd = ["uv", "run", "--project", str(target), "--frozen", "--offline", "--quiet", "python", str(CANARIES), "--json",
           "--mode", mode]
    if clock:
        cmd += ["--clock", clock]
    for name in only:
        cmd += ["--only", name]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=600)
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        sys.exit(f"canaries did not run (exit {proc.returncode}):\n{proc.stderr[-3000:]}")
    commit = subprocess.run(["git", "-C", str(target), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    report["target_commit"] = commit or None
    return report


def _error(result: dict) -> dict | None:
    try:
        return json.loads(result["stderr"]).get("error")
    except (ValueError, AttributeError):
        return None


def _facts(result: dict) -> dict:
    """A failed canary as machine facts. Its stdout stays out: answers carry Renfe's free text."""
    keep = ("canary", "what", "argv", "exit_code", "problems", "seconds", "retry_pauses", "facts")
    facts = {k: result[k] for k in keep if result.get(k) not in (None, [], "")}
    if error := _error(result):
        facts["error"] = error
    if result["requests"]:
        facts["requests"] = [f"{r['method']} {r['host']}{r['path']} -> {r.get('got')}" for r in result["requests"]]
    return facts


def _drift_expectation(target: Path) -> list[str]:
    """What the drifted flow prints against the plain twin: the facts a correct fix must keep."""
    report = run_canaries(target, "plain", None, ["search-new-system"])
    result = report["results"][0]
    if result["status"] != "pass":
        return []
    out = json.loads(result["stdout"])
    return [f"{t['service']} {t['departure']} -> {t['arrival']}, cheapest {t['fares'][0]['price']} EUR"
            for t in out["trains"]]


def event_for(failure: str, results: list[dict], report: dict, target: Path, seq: int) -> dict:
    stamp = datetime.now()
    first = results[0]
    names = ", ".join(r["canary"] for r in results)
    evidence = {"failure": failure, "twin_mode": report["mode"], "clock": report["clock"],
                "target_commit": report["target_commit"], "failed_canaries": [_facts(r) for r in results],
                "rerun": f"python -m intake.monitoring --mode {report['mode']} --clock {report['clock']} --check "
                         "--target <worktree>"}
    if failure == "api-changed":
        message = (_error(first) or {}).get("message", "")
        title = f"Renfe's API changed shape: {message} ({names})"
        expected = _drift_expectation(target)
        evidence.update(
            paths=["src/renfe/search.py"],
            needs=["offline-tests", "twin"],
            acceptance=[
                f"`renfe {' '.join(DRIFT_PROBE)}` against the twin in mode {report['mode']} exits 0 and lists the "
                "same trains, times and cheapest fares as against the plain twin: " + "; ".join(expected),
                f"`python -m intake.monitoring --mode {report['mode']} --check --target <worktree>` exits 0, and so "
                "does `--mode plain`",
                "a search-travel leg with neither the old nor the new time fields still stops with exit 7 "
                "(api_changed): the parser reads the new shape, it does not silence ApiShapeError",
                "the offline suite passes as it does on the base; tests/fakes.py (the old shape) is not edited",
                "a new test feeds the new shape and fails on the base",
                "the change stays in src/renfe/search.py and tests/; a correct fix exists in about 10 lines of "
                "search.py",
            ],
            new_shape=("search-travel legs carry departure_station.departure_timestamp and "
                       "arrival_station.arrival_timestamp (Madrid local time, ±HHMM, as journey-search writes them) "
                       "instead of departure_datetime, arrival_datetime and their *_timezone fields"),
        )
    elif failure == "calendar":
        facts = first.get("facts", {})
        title = (f"Clock canary: on {report['clock'][:10]} the legacy shop's last day has passed and its code is "
                 f"still in {len(facts.get('files', []))} files (issue #1)")
        evidence.update(paths=facts.get("files", []), issue=facts.get("issue"),
                        why_not_a_bug=("the date and what to delete were set by the maintainer (config "
                                       "NSV_FIRST_SALE_DATE, issue #1); deleting it removes the checkout command's "
                                       "only shop and README policy"))
    else:
        title = f"Canary failed: {names}: {'; '.join(first['problems'])[:160]}"
        evidence.update(needs=["offline-tests", "twin"])
    return {"id": f"evt-{stamp:%Y%m%d}-{stamp:%H%M%S}{secrets.token_hex(2)}", "source": SOURCES.get(failure, "monitoring"),
            "kind": KINDS[failure], "title": title, "evidence": evidence, "untrusted": None, "created_at": now()}


def log_line(path: Path, run_id: str, event_id: str, step: str, status: str, detail: str, data: dict) -> None:
    line = {"ts": now(), "run_id": run_id, "event_id": event_id, "step": step, "actor": "intake" if step == "intake"
            else "check", "status": status, "detail": detail, "data": data}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def no_change(report: dict, unavailable: list[dict], log: Path, seq: int) -> dict:
    """Renfe failed and the CLI said so as it should: a run that ends at intake with nothing to change."""
    stamp = datetime.now()
    event_id = f"evt-{stamp:%Y%m%d}-{stamp:%H%M%S}{secrets.token_hex(2)}"
    run_id = f"run-{stamp:%Y%m%d-%H%M%S}-{event_id.split('-')[-1]}"
    exits = sorted({r["exit_code"] for r in unavailable if r["exit_code"] is not None})
    degraded = [r["canary"] for r in unavailable if r.get("degraded")]
    detail = (f"twin {report['mode']}: {len(unavailable)} canaries saw Renfe fail; the CLI reported it "
              f"{'with exit ' + '/'.join(map(str, exits)) if exits else ''}"
              f"{' and degraded with a notice in ' + ', '.join(degraded) if degraded else ''}, as its contract says")
    data = {"twin_mode": report["mode"], "clock": report["clock"], "target_commit": report["target_commit"],
            "canaries": [_facts(r) for r in unavailable]}
    log_line(log, run_id, event_id, "intake", "info", detail, data)
    log_line(log, run_id, event_id, "outcome", "ok", "nothing to fix: the CLI behaves right when Renfe is unavailable",
             {"state": "success", "reason": "no-change", "changed": False})
    return {"event_id": event_id, "run_id": run_id, "detail": detail, "data": data}


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m intake.monitoring", description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default="plain", help="twin mode (see targets/renfe-cli/twin/chaos.py)")
    parser.add_argument("--clock", help="the CLI's 'now' (default: the canaries' fixed day); 2027-01-25T09:00:00+01:00 "
                                        "is the date canary")
    parser.add_argument("--target", help="test this checkout of the target instead of a fresh worktree of the base")
    parser.add_argument("--only", action="append", default=[], help="run only this canary (repeatable)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 0 only when every canary passes")
    parser.add_argument("--runs", default=str(RUNS), help="where inbox/ and events.jsonl live")
    args = parser.parse_args()
    runs = Path(args.runs)

    with Checkout(args.target) as target:
        report = run_canaries(target, args.mode, args.clock, args.only)
        print(f"twin: {report['mode']}  clock: {report['clock']}  target: {report['target_commit']}")
        for r in report["results"]:
            mark = "PASS" if r["status"] == "pass" else f"FAIL {r['failure']}"
            print(f"  {mark:<24} {r['canary']:<20} {' '.join(r['argv'] or ['(reads the source)'])}")
            for problem in r["problems"]:
                print(f"  {'':<24}   {problem[:150]}")
        failed = [r for r in report["results"] if r["status"] != "pass"]
        if args.check:
            return 1 if failed else 0

        groups: dict[tuple, list[dict]] = {}
        for r in failed:
            if r["failure"] == "renfe-unavailable":
                continue
            message = (_error(r) or {}).get("message") if r["failure"] == "api-changed" else None
            groups.setdefault((r["failure"], message), []).append(r)
        seq = 1
        for (failure, _), results in groups.items():
            event = event_for(failure, results, report, target, seq)
            inbox = runs / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / f"{event['id']}.json").write_text(json.dumps(event, indent=2, ensure_ascii=False) + "\n")
            print(f"-> event {event['id']} ({event['kind']}): {event['title']}")
            seq += 1
        unavailable = [r for r in failed if r["failure"] == "renfe-unavailable"]
        if unavailable:
            done = no_change(report, unavailable, runs / "events.jsonl", seq)
            print(f"-> no event, logged {done['run_id']}: {done['detail']}")
        if not failed:
            print("-> all canaries pass, nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
