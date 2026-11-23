"""The verifier gate, driven by target.toml.

Runs on a worktree whose HEAD is the builder's commit on top of the base. Every step returns
{"step", "ok", "detail", ...}. Nothing here is visible to or editable by the builder.
"""

import ast
import io
import json
import re
import shlex
import shutil
import sys
import tempfile
import tokenize
from pathlib import Path

from .core import PLUGINS, ROOT, RUNS, git, matches, sh, tail, timed

NO_NET = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
          "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
CLAIMED = ["live", "container", "docker", "ubuntu"]  # plus the target's verify.unverifiable_claims
# The gate rewrites source files and runs the suite again at once (the base's version, each mutant). A .pyc is
# trusted by its source's size and second of modification, so on a fast suite a rewrite of the same size could run
# the previous code. The gate's own runs write no bytecode.
QUIET = {**NO_NET, "PYTHONDONTWRITEBYTECODE": "1"}
WRONG_REASON = re.compile(r"ImportError|ModuleNotFoundError|SyntaxError|NameError|fixture '.*' not found")


# --- pytest plumbing -------------------------------------------------------------------------------

def pytest(wt, target, *args, env=None, timeout=900) -> dict:
    cmd = shlex.split(target["verify"]["test"]) + list(args)
    code, out = sh(cmd, cwd=wt, env={**QUIET, **(env or {})}, timeout=timeout)
    failed = set(re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M))
    passed = set(re.findall(r"^PASSED (\S+)", out, re.M))
    reasons = dict(re.findall(r"^(?:FAILED|ERROR) (\S+) - (.*)$", out, re.M))
    summary = next((l for l in reversed(out.strip().splitlines()) if re.search(r"\d+ (passed|failed|error)", l)), "")
    n = lambda word: int(m[1]) if (m := re.search(rf"(\d+) {word}", summary)) else 0
    return {"exit": code, "failed": failed, "passed_ids": passed, "reasons": reasons, "summary": summary.strip("= "),
            "passed": n("passed"), "n_failed": n("failed") + n("error"), "out": out}


def collect_ids(wt, target) -> set[str]:
    code, out = sh(shlex.split(target["verify"]["test"]) + ["--collect-only"], cwd=wt, env=QUIET)
    return {l.strip() for l in out.splitlines() if "::" in l and not l.startswith(("ERROR", "FAILED"))}


def shift_env(target, days):
    return {"GATE_SHIFT_DAYS": str(days), "GATE_CLOCK_MODULE": target["verify"]["clock_module"],
            "PYTHONPATH": str(PLUGINS)}


def startup(wt, target) -> list[dict]:
    env = {**NO_NET, **target["verify"].get("startup_env", {})}
    return [{"cmd": c, "ok": (r := sh(c, cwd=wt, env=env, timeout=120))[0] == 0, "tail": tail(r[1], 3)}
            for c in target["verify"]["startup"]]


def baseline(wt, target) -> dict:
    """What the base itself does under the gate: failing tests (plain and with each clock shift), test ids,
    startup. Cached per base commit, since the base never changes under a run."""
    sha = git(wt, "rev-parse", "HEAD").strip()
    cache = RUNS / "cache" / f"baseline-{sha[:12]}.json"
    if cache.exists():
        return {**json.loads(cache.read_text()), "cached": True}
    base = {"sha": sha, "failed": sorted(pytest(wt, target)["failed"]), "ids": sorted(collect_ids(wt, target)),
            "startup": startup(wt, target), "shifted": {}}
    for d in target["verify"]["clock_shifts"]:
        r = pytest(wt, target, "-p", "shift_plugin", env=shift_env(target, d))
        base["shifted"][str(d)] = sorted(r["failed"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(base, indent=1))
    return {**base, "cached": False}


class swapped:
    """Temporarily replace a directory of the worktree with the base's version of it."""

    def __init__(self, wt, base, path):
        self.wt, self.base, self.path = Path(wt), base, path

    def __enter__(self):
        shutil.rmtree(self.wt / self.path)
        archive = git(self.wt, "rev-parse", self.base).strip()
        sh(f"git archive {archive} {self.path} | tar -x", cwd=self.wt)

    def __exit__(self, *exc):
        shutil.rmtree(self.wt / self.path, ignore_errors=True)
        git(self.wt, "checkout", "HEAD", "--", self.path)


# --- the steps ------------------------------------------------------------------------------------

def step_scope(wt, target, ctx):
    base = target["base"]
    commits = int(git(wt, "rev-list", "--count", f"{base}..HEAD").strip())
    check = sh(["git", "diff", "--check", base, "HEAD"], cwd=wt)
    files = ctx["files"]
    ok = commits == 1 and check[0] == 0
    return {"ok": ok, "detail": f"{commits} commit, {len(files)} files, diff --check "
                                + ("clean" if check[0] == 0 else "has whitespace errors"),
            "files": files, "stat": git(wt, "diff", "--shortstat", base, "HEAD").strip()}


def step_lock(wt, target, ctx):
    if not target["verify"]["lock"]:
        return None
    results = [(c, sh(c, cwd=wt, timeout=300)[0] == 0) for c in target["verify"]["lock"]]
    return {"ok": all(ok for _, ok in results), "detail": "; ".join(f"{c}: {'ok' if ok else 'FAIL'}" for c, ok in results)}


def step_suite(wt, target, ctx):
    r = pytest(wt, target)
    ctx["suite"] = r
    new = sorted(r["failed"] - set(ctx["base"]["failed"]))
    ok = not new and r["exit"] in (0, 1) and r["passed"] > 0
    return {"ok": ok, "detail": r["summary"] + (f"; new failures: {', '.join(new)}" if new else
                                                 f"; same {len(r['failed'])} failure(s) as the base" if r["failed"] else ""),
            "passed": r["passed"], "new_failures": new}


def step_base_tests(wt, target, ctx):
    """The base's own tests (conftest and fakes included) against the new source."""
    with swapped(wt, target["base"], target["verify"]["tests_dir"]):
        r = pytest(wt, target)
    new = sorted(r["failed"] - set(ctx["base"]["failed"]))
    return {"ok": not new and r["passed"] > 0, "detail": r["summary"] + (f"; broken: {', '.join(new)}" if new else ""),
            "broken": new}


def step_new_tests_fail_on_base(wt, target, ctx):
    new = sorted(collect_ids(wt, target) - set(ctx["base"]["ids"]))
    ctx["new_tests"] = new
    if not new:
        needed = ctx["task"]["type"] in ("fix", "add")
        return {"ok": not needed, "detail": "no new test" + (" (a fix needs one that fails on the base)" if needed else ""),
                "new_tests": []}
    with swapped(wt, target["base"], target["verify"]["src_dir"]):
        r = pytest(wt, target, "-rA", *new)
    passing = [t for t in new if t in r["passed_ids"]]
    wrong = [t for t in new if t in r["failed"] and WRONG_REASON.search(r["reasons"].get(t, ""))]
    if r["exit"] in (2, 4) or "error" in r["summary"] and not r["failed"]:
        wrong = new
    # A fix's every new test must fail on the base. An addition may also keep a guard that the old behaviour
    # stays (it passes on the base), as long as one of its tests fails on the base.
    guards = passing if ctx["task"]["type"] == "add" and len(passing) < len(new) else []
    passing = [t for t in passing if t not in guards]
    ok = not passing and not wrong
    detail = f"{len(new)} new test(s); on the base: " + (
        ("all fail on their assertions" if not guards else
         f"the rest fail on their assertions; {len(guards)} guard(s) of unchanged behaviour pass") if ok else
        "; ".join(filter(None, [passing and f"PASS (prove nothing): {', '.join(passing)}",
                                wrong and f"fail for the wrong reason: {', '.join(wrong)}"])))
    return {"ok": ok, "detail": detail, "new_tests": new, "pass_on_base": passing, "guards": guards,
            "wrong_reason": wrong}


def step_mutants(wt, target, ctx):
    mutants, changed = make_mutants(wt, target)
    if not mutants:
        return {"ok": True, "detail": f"{changed} source lines added" + (", none with an operator to mutate" if changed else ""),
                "mutants": []}
    deselect = [a for t in ctx["base"]["failed"] for a in ("--deselect", t)]
    results = []
    for m in mutants:
        path = Path(wt) / m["file"]
        original = path.read_text()
        lines = original.splitlines(keepends=True)
        lines[m["line"] - 1] = m["mutated"] + "\n"
        path.write_text("".join(lines))
        try:
            r = pytest(wt, target, "-x", *deselect, timeout=300)
        finally:
            path.write_text(original)
        results.append({**m, "killed": r["exit"] != 0, "by": sorted(r["failed"])[:1]})
    alive = [m for m in results if not m["killed"]]
    return {"ok": not alive, "detail": f"{len(results) - len(alive)}/{len(results)} mutants killed"
            + ("; alive: " + "; ".join(f"{m['file']}:{m['line']} {m['op']}" for m in alive) if alive else ""),
            "mutants": results}


def step_clock(wt, target, ctx):
    if not target["verify"]["clock_shifts"]:
        return None
    rows = []
    for d in target["verify"]["clock_shifts"]:
        r = pytest(wt, target, "-p", "shift_plugin", env=shift_env(target, d))
        new = sorted(r["failed"] - set(ctx["base"]["shifted"].get(str(d), [])))
        rows.append({"days": d, "summary": r["summary"], "new_failures": new})
    bad = [r for r in rows if r["new_failures"]]
    return {"ok": not bad, "detail": "; ".join(f"+{r['days']}d: {r['summary']}"
                                               + (f" (new: {len(r['new_failures'])})" if r["new_failures"] else "")
                                               for r in rows), "shifts": rows}


def step_startup(wt, target, ctx):
    if not target["verify"]["startup"]:
        return None
    now, before = startup(wt, target), {s["cmd"]: s["ok"] for s in ctx["base"]["startup"]}
    broken = [s["cmd"] for s in now if not s["ok"] and before.get(s["cmd"], True)]
    label = lambda s: s["cmd"].split("renfe ", 1)[-1] + ": " + (
        "ok" if s["ok"] else "FAIL" if before.get(s["cmd"], True) else "fails, as on the base")
    return {"ok": not broken, "detail": "no system tz data, no network: " + "; ".join(map(label, now)), "runs": now}


def step_sweep(wt, target, ctx):
    if not any(matches(f, target["verify"].get("sweep_paths", [])) for f in ctx["files"]):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="sweep-"))
    script, env = str(PLUGINS / "station_sweep.py"), {**NO_NET, "GATE_SWEEP_MODULE": target["verify"]["sweep_module"]}
    with swapped(wt, target["base"], target["verify"]["src_dir"]):
        sh(["uv", "run", "python", script, "corpus", str(tmp / "corpus.json")], cwd=wt, env=env)
        sh(["uv", "run", "python", script, "resolve", str(tmp / "corpus.json"), str(tmp / "before.json")], cwd=wt, env=env)
    sh(["uv", "run", "python", script, "resolve", str(tmp / "corpus.json"), str(tmp / "after.json")], cwd=wt, env=env)
    try:
        before, after = json.loads((tmp / "before.json").read_text()), json.loads((tmp / "after.json").read_text())
    except (OSError, ValueError):
        return {"ok": False, "detail": "the sweep couldn't run"}
    kind = lambda v: v if v.startswith("<") else "resolved"
    classes = {}
    for q, b in before.items():
        a = after.get(q, "<missing>")
        if a != b:
            classes.setdefault(f"{kind(b)} -> {'other station' if kind(a) == kind(b) == 'resolved' else kind(a)}", []).append(
                f"{q}: {b} -> {a}")
    (ctx["run_dir"] / "sweep.json").write_text(json.dumps(classes, indent=1, ensure_ascii=False))
    # Resolving silently to another station is acceptable only for queries the commit message names.
    msg = git(wt, "log", "-1", "--format=%B").lower()
    risky = {k: [t for t in v if t.split(": ")[0].lower() not in msg] for k, v in classes.items()
             if k in ("resolved -> other station", "<ambiguous> -> resolved")}
    risky = {k: v for k, v in risky.items() if v}
    ctx["sweep"] = {"queries": len(before), "changed": sum(map(len, classes.values()))}
    return {"ok": True, "needs_maintainer": bool(risky),
            "detail": f"{len(before)} queries, {ctx['sweep']['changed']} change: "
                      + (", ".join(f"{k} {len(v)}" for k, v in classes.items()) or "none")
                      + (f"; {sum(map(len, risky.values()))} undeclared silent re-resolutions need the maintainer"
                         if risky else ""),
            "transitions": {k: v[:40] for k, v in classes.items()}, "undeclared": risky, "queries": len(before)}


def claims(target) -> re.Pattern:
    """A commit message saying it tested something the gate can't re-run."""
    what = "|".join(CLAIMED + target["verify"].get("unverifiable_claims", []))
    return re.compile(r"(?i)\b(tested|checked|verified|observed|confirmed|ran)\b[^.\n]{0,50}\b(" + what + ")")


def step_message(wt, target, ctx):
    msg = git(wt, "log", "-1", "--format=%B").strip()
    negated = lambda m: re.search(r"(?i)\b(not|never|no|without|un\w+)\b[^.\n]{0,15}$", msg[max(0, m.start() - 20):m.start()] + m[1])
    problems = [f"claims a check the gate can't re-run: \"{m.group(0)}\"" for m in claims(target).finditer(msg) if not negated(m)]
    known = {ctx.get("suite", {}).get("passed"), len(ctx.get("new_tests", [])), *ctx.get("sweep", {}).values()}
    for m in re.finditer(r"(\d[\d,]*)\s+(?:[a-z]+\s+)?(tests?|queries)\b", msg):
        n = int(m[1].replace(",", ""))
        if n not in known:
            problems.append(f"says \"{m.group(0)}\"; the gate counted {sorted(k for k in known if k)}")
    return {"ok": not problems, "detail": "; ".join(problems) or "claims only what the gate re-ran", "message": msg}


def step_rule(wt, target, ctx, rule):
    code, out = sh(rule["check"]["cmd"], cwd=wt, env=NO_NET, timeout=300)
    return {"ok": code == 0, "detail": f"{rule['text'][:80]}: {'ok' if code == 0 else 'FAIL'} {tail(out, 2)}"}


def step_recheck(wt, target, ctx):
    """The monitoring check that raised the event, run again on the change: the fix has to clear it."""
    event = ctx.get("event") or {}
    cmd = (event.get("evidence") or {}).get("rerun") if event.get("source") == "monitoring" else None
    if not cmd:
        return None
    argv = shlex.split(cmd.replace("<worktree>", str(wt)))
    if argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    code, out = sh(argv, cwd=ROOT, env=NO_NET, timeout=600)
    failing = "; ".join(line.strip() for line in out.splitlines() if line.lstrip().startswith("FAIL"))
    return {"ok": code == 0, "detail": f"the canary that raised the event {'passes' if code == 0 else 'still fails'}"
                                       + (f": {failing[:160]}" if failing else "")}


STEPS = [("scope", step_scope), ("lock", step_lock), ("suite", step_suite), ("base-tests", step_base_tests),
         ("new-tests-fail-on-base", step_new_tests_fail_on_base), ("mutants", step_mutants), ("clock", step_clock),
         ("startup", step_startup), ("sweep", step_sweep), ("recheck", step_recheck),
         ("message", step_message)]


def run_gate(wt, target, ctx, rules: list[dict], log) -> dict:
    """All steps, each logged as it ends. Returns {ok, needs_maintainer, steps}."""
    steps = []
    extra = [(f"rule:{r['id']}", lambda w, t, c, r=r: step_rule(w, t, c, r)) for r in rules
             if r["check"]["type"] == "script" and r["check"].get("approved_at")]  # a person let this command run
    for name, fn in STEPS + extra:
        try:
            result, secs = timed(fn, wt, target, ctx)
        except Exception as e:  # a broken step is a failed step, never a pass
            result, secs = {"ok": False, "detail": f"step crashed: {type(e).__name__}: {e}"}, 0
        if result is None:
            continue
        result = {"step": name, "secs": secs, **result}
        steps.append(result)
        log("verify", "check", "ok" if result["ok"] else "failed", f"{name}: {result['detail']}",
            {k: v for k, v in result.items() if k not in ("detail",)})
    return {"ok": all(s["ok"] for s in steps), "needs_maintainer": any(s.get("needs_maintainer") for s in steps),
            "steps": steps}


# --- mutants --------------------------------------------------------------------------------------

OPS = [("==", "!="), ("!=", "=="), ("<=", "<"), (">=", ">"), (" < ", " <= "), (" > ", " >= "), (" and ", " or "),
       (" or ", " and "), ("not ", ""), ("[0]", "[-1]"), ("[-1]", "[0]"), ("True", "False"), ("False", "True"),
       (" + 1", " - 1"), (" - 1", " + 1"), ("min(", "max("), ("max(", "min("), (" not in ", " in "), (" in ", " not in "),
       (" if ", " if not "), (" is None", " is not None"), (" is not None", " is None")]


def make_mutants(wt, target) -> list[dict]:
    """Plausible wrong versions of the lines the change added to the source, one operator each."""
    src = target["verify"]["src_dir"] + "/"
    diff = git(wt, "diff", "-U0", target["base"], "HEAD", "--", src)
    added, file = [], None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.endswith(".py") else None
        elif line.startswith("@@") and file:
            lineno = int(re.search(r"\+(\d+)", line)[1])
        elif line.startswith("+") and file:
            added.append((file, lineno, line[1:]))
            lineno += 1
    candidates, in_strings = [], {}
    for file, lineno, text in added:
        if file not in in_strings:
            in_strings[file] = _string_lines(Path(wt) / file)
        if lineno in in_strings[file]:  # a line of a docstring or other multi-line string
            continue
        code = text.split("#")[0]
        stripped = code.strip()
        if not stripped or stripped.startswith(("def ", "class ", "import ", "from ", "@")) or (
                stripped[0] in "\"'" and stripped[-1] in "\"'"):  # a docstring or a plain string line
            continue
        for old, new in OPS:
            if old in code and not _in_string(code, code.index(old)):
                mutated = text.replace(old, new, 1)
                if _compiles(Path(wt) / file, lineno, mutated):
                    candidates.append({"file": file, "line": lineno, "op": f"{old.strip()} -> {new.strip() or '(removed)'}",
                                       "mutated": mutated})
    # Spread over lines first: the first operator of every line, then the second, ...
    by_line = {}
    for c in candidates:
        by_line.setdefault((c["file"], c["line"]), []).append(c)
    spread = [c for i in range(len(OPS)) for group in by_line.values() if i < len(group) for c in [group[i]]]
    return spread[: target["verify"].get("mutants_max", 6)], len(added)


def _string_lines(path: Path) -> set[int]:
    """Lines that lie inside a multi-line string (a docstring's continuation lines): mutating them proves nothing."""
    lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                lines.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, SyntaxError):
        pass
    return lines


def _in_string(code: str, at: int) -> bool:
    return code[:at].count('"') % 2 == 1 or code[:at].count("'") % 2 == 1


def _compiles(path: Path, lineno: int, mutated: str) -> bool:
    lines = path.read_text().splitlines()
    lines[lineno - 1] = mutated
    try:
        ast.parse("\n".join(lines))
        return True
    except SyntaxError:
        return False
