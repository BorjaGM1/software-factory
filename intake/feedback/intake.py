"""Feedback intake: one feedback file in, either a checked task in runs/inbox/ or a `rejected` outcome.

    python3 intake/feedback/intake.py FILE [--all-layers]
    python3 intake/feedback/intake.py --self-test

--all-layers keeps going after a deterministic check on the raw text fails, so the log shows what the reader and
the task checks would have done on their own. The verdict is the same: anything that fails is rejected.
--self-test proves the reader has no tools, and that the proof catches a reader that does.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import checks
import quarantine
import source

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
TARGET = "renfe-cli"
KINDS = {"fix": "bug-report", "add": "feature-request", "investigate": "bug-report"}


def runs_dir() -> Path:
    return Path(os.environ.get("FACTORY_RUNS_DIR", ROOT / "runs"))


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_policy() -> dict:
    policy = tomllib.loads((HERE / "policy.toml").read_text())
    target = dict(policy["targets"][TARGET])
    adapter = ROOT / "targets" / TARGET / "target.toml"
    conf = tomllib.loads(adapter.read_text()) if adapter.exists() else {}
    target["forbidden"] = sorted(set(target["forbidden"]) | set(conf.get("forbidden_paths", [])))
    target["repo"] = os.path.expanduser(conf.get("repo") or conf.get("repo_path") or "~/projects/renfe-cli-lab")
    target["base"] = conf.get("base_ref") or conf.get("base") or "main"
    return {"limits": policy["limits"], "target": target}


def repo_files(target: dict) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", target["repo"], "ls-tree", "-r", "--name-only", target["base"]],
                             capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        sys.exit(f"feedback intake: no clone of {TARGET} with a `{target['base']}` at {target['repo']} (see README.md)")
    return [f for f in out.splitlines() if f]


class Log:
    def __init__(self, event_id: str):
        self.event_id, self.run_id = event_id, "run-" + event_id.removeprefix("evt-")
        self.path = runs_dir() / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, step: str, actor: str, status: str, detail: str, **data) -> None:
        line = {"ts": now(), "run_id": self.run_id, "event_id": self.event_id, "step": step, "actor": actor,
                "status": status, "detail": detail}
        if data:
            line["data"] = data
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"  {step:8} {actor:7} {status:7} {detail}")


def first_failure(results: list[checks.Check]) -> str | None:
    return next((name for name, ok, _ in results if not ok), None)


def as_data(results: list[checks.Check]) -> list[dict]:
    return [{"check": n, "ok": ok, "detail": d} for n, ok, d in results]


def run(path: Path, all_layers: bool = False) -> dict:
    policy = load_policy()
    files = repo_files(policy["target"])
    fb = source.load(path)
    stamp = datetime.now()
    event_id = f"evt-{stamp:%Y%m%d-%H%M%S}-fb{secrets.token_hex(2)}"
    log = Log(event_id)
    print(f"{path.name} -> {event_id}")
    where = f"{'GitHub issue' if fb['channel'] == 'github-issue' else 'X reply'} {fb['ref']}"
    log("intake", "intake", "started", f"Feedback received: {where} by @{fb['author']}",
        title=f"Feedback: {where}", source="feedback", kind="feedback",
        channel=fb["channel"], ref=fb["ref"], raw_file=str(path), raw_bytes=len(fb["text"].encode()))

    spent: dict = {}  # the reader's usage, once it has run: every outcome below sums it, as the contract says

    def total() -> dict:
        u = spent.get("usage")
        if not u:
            return {}
        rest = {k: v for k, v in u.items() if k not in ("agent", "model")}
        return {"usage": {**rest, "agents": ["reader"], "models": [u["model"]] if u["model"] else []},
                "cost_usd": u["cost_usd"]}

    def reject(reason: str, detail: str, **data) -> dict:
        log("outcome", "intake", "failed", f"Rejected at intake: {detail}", state="rejected", reason=reason,
            **total(), **data)
        return {"event_id": event_id, "state": "rejected", "reason": reason}

    raw_results = checks.raw(fb["text"], policy["limits"])
    raw_fail = first_failure(raw_results)
    log("intake", "check", "failed" if raw_fail else "ok",
        f"Raw text checks: {raw_fail} ({dict((n, d) for n, _, d in raw_results)[raw_fail]})" if raw_fail
        else "Raw text checks passed (size, hidden text, encoded text, injection phrases)",
        checks=as_data(raw_results))
    if raw_fail and not all_layers:
        return reject(raw_fail, f"the raw text failed `{raw_fail}`; no model read it")

    reading = quarantine.read(fb["text"], files)
    run_dir = runs_dir() / log.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "reader.trace.jsonl").write_text("".join(reading.trace))  # stays on this machine, under runs/
    if reading.usage:
        u = spent["usage"] = reading.usage
        log("agent", "agent", "ok" if reading.answer is not None else "failed",
            f"reader done ({u['model']}): {u['turns'] or 0} turns, "
            f"{u['input_tokens'] + u['cache_read_tokens'] + u['cache_creation_tokens']:,} tokens in, "
            f"{u['output_tokens']:,} out, ${u['cost_usd']:.3f}", agent="reader", usage=u)
    if not reading.verified:
        log("intake", "agent", "failed", f"Quarantine reader not used: {reading.problem}", init=reading.init)
        log("outcome", "intake", "failed", "Blocked: the quarantine reader could not be proven tool-free",
            state="blocked", reason="quarantine-unverified", **total())
        return {"event_id": event_id, "state": "blocked", "reason": "quarantine-unverified"}
    if reading.answer is None:
        refused = any(w in reading.problem.lower() for w in ("can't help", "cannot help", "usage policy", "/aup"))
        log("intake", "agent", "failed", f"Quarantine reader (tools: none, verified) gave no answer: {reading.problem}",
            tools=reading.init.get("tools"), model=reading.init.get("model"))
        if refused or raw_fail:
            return reject(raw_fail or "reader-refused",
                          (f"the raw text failed `{raw_fail}`" if raw_fail else "the model refused to read it") +
                          ("; the model refused to read it too" if raw_fail and refused else ""))
        log("outcome", "intake", "failed", "Blocked: the quarantine reader failed", state="blocked",
            reason="quarantine-failed", **total())
        return {"event_id": event_id, "state": "blocked", "reason": "quarantine-failed"}
    doc = checks.parse_answer(reading.answer)
    said = ("refused: " + str(doc["reject"])) if doc and "reject" in doc else \
        "proposed a task" if doc else "answered with something that is not one JSON object"
    log("intake", "agent", "ok" if doc and "reject" not in doc else "failed",
        f"Quarantine reader ({reading.init.get('model')}, tools: none, verified) {said}",
        tools=reading.init.get("tools"), mcp_servers=reading.init.get("mcp_servers"),
        model=reading.init.get("model"), cost_usd=reading.cost_usd)

    if doc is None:
        return reject("reader-output-not-json", "the reader's answer was not one JSON object")
    if "reject" in doc:
        slug = doc["reject"] if set(doc) == {"reject"} and doc["reject"] in quarantine.REJECT_SLUGS else "reader-refused"
        if raw_fail:
            return reject(raw_fail, f"the raw text failed `{raw_fail}`, and the reader refused it too ({slug})",
                          reader=slug)
        return reject(slug, f"the quarantine reader refused it ({slug})")

    task_results = checks.task(doc, fb["text"], policy, set(files))
    task_fail = first_failure(task_results)
    log("intake", "check", "failed" if task_fail else "ok",
        f"Task checks: {task_fail} ({dict((n, d) for n, _, d in task_results)[task_fail]})" if task_fail
        else "Task checks passed (schema, type, size, paths, no URLs, commands, secrets or talk of the factory)",
        checks=as_data(task_results), proposed=doc)
    if raw_fail:
        return reject(raw_fail, f"the raw text failed `{raw_fail}`" +
                      (f", and the task failed `{task_fail}`" if task_fail else
                       "; the reader's task passed its checks but is dropped with the feedback"),
                      task_check=task_fail)
    if task_fail:
        return reject(task_fail, f"the reader's task failed `{task_fail}`")

    task = {
        "id": "task-" + event_id.removeprefix("evt-"),
        "event_id": event_id,
        "type": doc["type"],
        "summary": doc["summary"],
        "acceptance": doc["acceptance"],
        "allowed_paths": doc["allowed_paths"],
        "forbidden_paths": policy["target"]["forbidden"],
    }
    event = {
        "id": event_id,
        "source": "feedback",
        "kind": KINDS[doc["type"]],
        "title": f"{where}: {doc['type']}: {doc['summary']}",
        "evidence": {"channel": fb["channel"], "ref": fb["ref"], "url": fb["url"], "author": fb["author"],
                     "received_at": fb["created_at"], "raw_file": str(path),
                     "quarantine": {"model": reading.init.get("model"), "tools": reading.init.get("tools"),
                                    "mcp_servers": reading.init.get("mcp_servers"), "cost_usd": reading.cost_usd},
                     "checks": as_data(raw_results + task_results)},
        "untrusted": fb["text"],
        "created_at": now(),
        "task": task,
    }
    inbox = runs_dir() / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{event_id}.json").write_text(json.dumps(event, ensure_ascii=False, indent=2) + "\n")
    log("intake", "intake", "ok", f"Checked task {task['id']} queued for the builder: {doc['summary']}",
        task_id=task["id"], inbox=f"runs/inbox/{event_id}.json")
    return {"event_id": event_id, "state": "queued", "task": task}


def self_test() -> int:
    files = repo_files(load_policy()["target"])
    ok = quarantine.read("The search is great, thanks!", files)
    print(f"reader as configured: verified={ok.verified} tools={ok.init.get('tools')} "
          f"mcp_servers={ok.init.get('mcp_servers')} model={ok.init.get('model')} answer={ok.answer!r}")
    leak = quarantine.read("The search is great, thanks!", files, tools="default")
    print(f"control, reader given its default tools: verified={leak.verified} ({leak.problem})")
    return 0 if ok.verified and ok.init.get("tools") == [] and not leak.verified else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--all-layers", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.files:
        ap.error("give a feedback file")
    for f in args.files:
        result = run(f, args.all_layers)
        print(f"=> {result['state']}" + (f" ({result['reason']})" if "reason" in result else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
