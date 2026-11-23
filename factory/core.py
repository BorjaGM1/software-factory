"""Paths, the target adapter, the run log, and small shell helpers shared by the engine."""

import fnmatch
import json
import os
import subprocess
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("FACTORY_HOME", Path(__file__).resolve().parent.parent))
RUNS = ROOT / "runs"
INBOX = RUNS / "inbox"
LOG = RUNS / "events.jsonl"
RULES = ROOT / "rules"
PLUGINS = Path(__file__).resolve().parent / "plugins"
DEFAULT_TARGET = ROOT / "targets" / "renfe-cli" / "target.toml"
DEFAULT_AGENT = "claude -p"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_target(path: Path = DEFAULT_TARGET) -> dict:
    target = tomllib.loads(Path(path).read_text())
    target["repo"] = str(Path(target["repo"]).expanduser())
    target["dir"], target["file"] = str(Path(path).parent), str(path)
    for key in ("lock", "startup", "clock_shifts"):  # gate steps a target can leave out
        target["verify"].setdefault(key, [])
    target.setdefault("forbidden_paths", [])
    target.setdefault("triage", {}).setdefault("manual_kinds", [])
    target["triage"].setdefault("capabilities", {})
    return target


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)  # `*` crosses `/` here, on purpose


def _intake_only(run_id: str) -> bool:
    """Has only an intake logged under this run id so far (no step of the engine, no outcome)?"""
    if not LOG.exists():
        return False
    steps = {json.loads(l).get("step") for l in LOG.read_text().splitlines() if f'"run_id": "{run_id}"' in l}
    return bool(steps) and steps <= {"intake", "agent"}


class Run:
    """One event going through the process. Every step is one line in runs/events.jsonl."""

    def __init__(self, event: dict, run_id: str | None = None):
        self.event = event
        self.id = run_id or self._new_id(event["id"])
        self.dir = RUNS / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cost = 0.0
        self.ended = False

    @staticmethod
    def _new_id(event_id: str) -> str:
        """run-<event id without evt->, as the intakes name it; a later run of the same event gets -2, -3..."""
        base = "run-" + event_id.removeprefix("evt-")
        if (RUNS / base).exists() and not (RUNS / base / "outcome.json").exists() and _intake_only(base):
            return base  # an intake opened this run (and its folder) for us: carry on under the same id
        n = 1
        while (RUNS / (base if n == 1 else f"{base}-{n}")).exists():
            n += 1
        return base if n == 1 else f"{base}-{n}"

    def logged(self, step: str) -> bool:
        """Has someone (an intake) already logged this step for this run?"""
        if not LOG.exists():
            return False
        return any(f'"run_id": "{self.id}"' in l and f'"step": "{step}"' in l for l in LOG.read_text().splitlines())

    def usage(self) -> dict | None:
        """Every agent's usage logged under this run so far (an intake's reader included), summed."""
        from .trace import add
        total = None
        if LOG.exists():
            for line in LOG.read_text().splitlines():
                if f'"run_id": "{self.id}"' in line and '"usage"' in line:
                    d = json.loads(line)
                    if d["step"] != "outcome" and isinstance((d.get("data") or {}).get("usage"), dict):
                        total = add(total, d["data"]["usage"])
        return total

    def log(self, step: str, actor: str, status: str, detail: str, data: dict | None = None):
        line = {"ts": now(), "run_id": self.id, "event_id": self.event["id"], "step": step,
                "actor": actor, "status": status, "detail": detail}
        if data:
            line["data"] = data
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"[{step:7}] {status:7} {detail}", flush=True)

    def outcome(self, state: str, reason: str, detail: str, **data):
        if self.ended:
            return
        self.ended = True
        usage = self.usage()
        data = {"state": state, "reason": reason, "cost_usd": round(usage["cost_usd"] if usage else self.cost, 4),
                **({"usage": usage} if usage else {}), **data}
        self.log("outcome", "check", "ok" if state == "success" else "info", detail, data)
        (self.dir / "outcome.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))


def sh(cmd: str | list, cwd=None, env: dict | None = None, timeout: int = 900, input: str | None = None):
    """Run a command; returns (exit code, combined output). Never raises on a non-zero exit."""
    full_env = {**os.environ, **(env or {})}
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, env=full_env, input=input,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, f"timeout after {timeout}s\n{e.stdout or ''}"


def git(cwd, *args, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def tail(text: str, n: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def timed(fn, *args, **kwargs):
    start = time.monotonic()
    result = fn(*args, **kwargs)
    return result, round(time.monotonic() - start, 1)
