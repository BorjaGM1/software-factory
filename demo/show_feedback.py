#!/usr/bin/env python3
"""A feedback that never reaches a builder, shown in a keepty TUI: the raw feedback as it arrived, the injected
part highlighted, then the real intake running on it and its verdict. Recorded like the builder, to
runs/<run>/feedback.cast (asciinema v2, 100x40, with start_epoch).

    python3 demo/show_feedback.py FILE [--highlight TEXT]... [--hold SECONDS]

--highlight marks every line containing TEXT (case-insensitive); the intake doesn't log which words it objected
to, so say which part is the injection. Example:
    python3 demo/show_feedback.py intake/feedback/samples/07b-fake-maintainer-note.json --highlight "maintainer note"
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DIM, BOLD, RESET = "\x1b[38;5;245m", "\x1b[1m", "\x1b[0m"
MARK = "\x1b[48;5;52m\x1b[38;5;217m"  # dark red band, light red text
VERDICT = {"rejected": "\x1b[1;38;5;177m", "blocked": "\x1b[1;38;5;221m", "accepted": "\x1b[1;38;5;114m"}
WIDTH = 96


def show(text: str, marks: list[str], indent: str = "  "):
    for para in text.splitlines() or [""]:
        hit = any(m.lower() in para.lower() for m in marks)
        for line in textwrap.wrap(para, WIDTH - len(indent)) or [""]:
            if hit:
                print(f"{indent}{MARK} {line.ljust(WIDTH - len(indent) - 2)}{RESET}", flush=True)
            else:
                print(f"{indent}{line}", flush=True)
            time.sleep(0.04)


def inner(path: Path, marks: list[str], hold: float, evt_file: Path):
    doc = json.loads(path.read_text())
    print(f"\x1b[?25l{DIM}feedback received{RESET}\n", flush=True)  # no cursor
    time.sleep(0.8)
    if "number" in doc:  # a GitHub issue, as `gh issue view --json` prints it
        who = doc.get("author") or {}
        print(f"{BOLD}#{doc['number']} {doc.get('title', '')}{RESET}")
        print(f"{DIM}@{who.get('login')} · {doc.get('authorAssociation', '').lower()}{RESET}", flush=True)
        show(doc.get("body") or "", marks)
        for c in doc.get("comments") or []:
            time.sleep(0.6)
            print(f"\n{DIM}@{(c.get('author') or {}).get('login')} · {c.get('authorAssociation', '').lower()}"
                  f" · comment{RESET}", flush=True)
            show(c.get("body") or "", marks)
    else:  # an X post (API v2)
        users = {u["id"]: u for u in (doc.get("includes") or {}).get("users", [])}
        print(f"{DIM}@{users.get(doc['data'].get('author_id'), {}).get('username')} on X{RESET}", flush=True)
        show(doc["data"]["text"], marks)
    time.sleep(2.5)
    print(f"\n{DIM}{'─' * 30} intake {'─' * 58}{RESET}\n", flush=True)
    time.sleep(0.8)
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "intake/feedback/intake.py"), str(path)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        line = line.rstrip("\n")
        if m := re.search(r"-> (evt-\S+)", line):
            evt_file.write_text(m[1])
        if m := re.match(r"=> (\w+)(.*)", line):
            print(f"\n  {VERDICT.get(m[1], BOLD)}{m[1].upper()}{m[2]}{RESET}", flush=True)
        else:
            for part in textwrap.wrap(line, WIDTH + 2, subsequent_indent=" " * 26) or [""]:
                print(f"{DIM}{part}{RESET}", flush=True)
    proc.wait()
    time.sleep(hold)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--highlight", action="append", default=[])
    ap.add_argument("--hold", type=float, default=6)
    ap.add_argument("--inner", type=Path, help=argparse.SUPPRESS)
    a = ap.parse_args()
    path = a.file.resolve()
    if a.inner:
        return inner(path, a.highlight, a.hold, a.inner)

    from factory.keepty import Session
    runs = Path(os.environ.get("FACTORY_RUNS_DIR") or ROOT / "runs")
    tmp = runs / "demo" / f"feedback-{path.stem}-{int(time.time())}.cast"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    evt_file = tmp.with_suffix(".evt")
    cmd = [sys.executable, str(Path(__file__).resolve()), str(path), "--inner", str(evt_file), "--hold", str(a.hold)]
    for h in a.highlight:
        cmd += ["--highlight", h]
    env = {k: os.environ[k] for k in ("PATH", "HOME", "CLAUDE_CONFIG_DIR", "FACTORY_AGENT_CMD", "FACTORY_RUNS_DIR",
                                      "FACTORY_QUARANTINE_MODEL") if k in os.environ}
    with Session(f"feedback-{path.stem}", cmd, ROOT, tmp, env) as s:
        time.sleep(2)
        while s.alive():
            time.sleep(0.5)
    if evt_file.exists():
        out = runs / ("run-" + evt_file.read_text().strip().removeprefix("evt-")) / "feedback.cast"
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp, out)
        evt_file.unlink()
        print(out)
    else:
        print(f"{tmp} (the intake printed no event id)")


if __name__ == "__main__":
    main()
