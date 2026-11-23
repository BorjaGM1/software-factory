#!/usr/bin/env python3
"""Append a fixture log to an events file at a chosen speed, rewriting each line's ts to now.

    python3 dashboard/replay.py                      # fixture -> runs/replay/events.jsonl, real time
    python3 dashboard/replay.py --speed 3 --fresh    # 3x faster, truncate the target first
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "runs/fixtures/demo-events.jsonl"
REPLAY_LOG = ROOT / "runs/replay/events.jsonl"


def _ts(line):
    try:
        return datetime.fromisoformat(json.loads(line)["ts"])
    except (ValueError, KeyError, TypeError):
        return None


def replay(fixture=FIXTURE, target=REPLAY_LOG, speed=1.0, fresh=False, delay=0.0, loop=False, hold=10.0):
    lines = [l for l in Path(fixture).read_text().splitlines() if l.strip()]
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    while True:
        if fresh:
            target.write_text("")
        time.sleep(delay)
        prev = None
        for raw in lines:
            ts = _ts(raw)
            if prev and ts:
                time.sleep(max(0.0, (ts - prev).total_seconds()) / speed)
            prev = ts or prev
            try:
                obj = json.loads(raw)
                obj["ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
                raw = json.dumps(obj, ensure_ascii=False)
            except (ValueError, TypeError):
                pass  # replay what we can't parse as is: the dashboard must survive it
            with target.open("a") as f:
                f.write(raw + "\n")
        if not loop:
            return
        time.sleep(hold)
        fresh = True


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixture", default=FIXTURE)
    p.add_argument("--to", default=REPLAY_LOG, help="events file to append to (default: runs/replay/events.jsonl)")
    p.add_argument("--speed", type=float, default=1.0, help="2 = twice as fast as the fixture's timestamps")
    p.add_argument("--fresh", action="store_true", help="truncate the target before replaying")
    p.add_argument("--loop", action="store_true", help="start over after the end")
    a = p.parse_args()
    replay(a.fixture, a.to, a.speed, a.fresh, loop=a.loop)


if __name__ == "__main__":
    main()
