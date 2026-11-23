#!/usr/bin/env python3
"""Play the demo's cases into the Claude Code TUI running in a keepty session, one at a time.

    uv run --with pyte demo/rehearse.py SESSION [--from N] [--only N]

Each case types one prompt and waits until Claude Code is idle again (no "esc to interrupt" on screen for a
while), with a time limit. The TUI and the dashboard are recorded separately (kt.py record, rec_dash.js).
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import kt  # noqa: E402

CASES = [
    ("api", "Renfe acaba de cambiar su API. Lanza los canaries contra el gemelo en modo shape-drift y, si sale "
            "un evento, pásaselo a la fábrica con un reintento."),
    ("feature", "Ha llegado feedback de un usuario: intake/feedback/samples/05-direct-only.json. Pásalo por la "
                "intake y, si queda una tarea, a la fábrica."),
    ("injection", "Otro feedback: intake/feedback/samples/07b-fake-maintainer-note.json. Lo mismo."),
    ("flawed", "Corre factory/examples/evt-s8-from-now.json con un reintento."),
    ("review", "Corre factory/examples/evt-timetable-stale.json con un reintento."),
    ("checkout", "Corre factory/examples/evt-s4-checkout.json."),
    ("blocked", "Corre factory/examples/evt-s9-porto-zone.json."),
    ("numbers", "Enséñame las reglas que ha aprendido (bin/factory rules) y lo que ha costado todo (bin/factory costs)."),
]
# Claude Code's spinner line while it works: a glyph, a gerund and an ellipsis ("· Thundering…").
SPINNER = re.compile(r"^\s*[·✢✳✶✻✽*●◐◓◑◒] ?[A-Z][a-z]+(?:ing)?…", re.M)


def busy(name):
    try:
        screen = kt.screen(name)
    except Exception:
        return True  # can't tell: assume it is still working
    return bool(SPINNER.search(screen)) or "esc to interrupt" in screen or "Do you want to" in screen


def wait_idle(name, limit, quiet=3, must_see_busy=True):
    """Wait until the TUI has been idle for `quiet` checks in a row (after it was seen busy, unless told not to)."""
    end, seen, calm, t0 = time.time() + limit, not must_see_busy, 0, time.time()
    while time.time() < end:
        if busy(name):
            seen, calm = True, 0
        elif seen or time.time() - t0 > 60:  # it may finish before we ever see it busy
            calm += 1
            if calm >= quiet:
                return True
        time.sleep(4)
    return False


def main():
    name = sys.argv[1]
    start = int(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else 1
    only = int(sys.argv[sys.argv.index("--only") + 1]) if "--only" in sys.argv else None
    for i, (tag, prompt) in enumerate(CASES, 1):
        if i < start or (only and i != only):
            continue
        wait_idle(name, limit=30 * 60, must_see_busy=False)  # whatever came before has finished
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] case {i} {tag}: sending", flush=True)
        kt.send(name, prompt.encode())
        time.sleep(0.8)
        kt.send(name, b"\r")
        ok = wait_idle(name, limit=20 * 60)
        print(f"[{time.strftime('%H:%M:%S')}] case {i} {tag}: {'idle' if ok else 'TIMEOUT'} after "
              f"{time.time() - t0:.0f}s", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
