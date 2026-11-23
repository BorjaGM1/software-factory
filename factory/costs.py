"""`factory costs`: what the runs in a log cost, from its `usage` lines (older runs: their outcome's cost_usd)."""

import json
from collections import defaultdict
from pathlib import Path

TOKENS = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")


def report(log: Path) -> str:
    runs = defaultdict(lambda: {"state": None, "event": None, "cost": 0.0, "traced": False, "usage": []})
    titles = {}
    for raw in log.read_text().splitlines():
        if not raw.strip():
            continue
        d = json.loads(raw)
        run, data = runs[d["run_id"]], d.get("data") or {}
        run["event"] = d["event_id"]
        if d["step"] == "intake" and d["event_id"] not in titles:
            titles[d["event_id"]] = data.get("title") or d["detail"]
        if d["step"] == "outcome":
            run["state"], run["outcome_cost"] = data.get("state"), data.get("cost_usd") or 0.0
        elif d["step"] == "intake" and d["actor"] == "agent" and data.get("cost_usd"):
            run["reader_cost"] = run.get("reader_cost", 0.0) + data["cost_usd"]  # a reader before usage lines
        elif isinstance(data.get("usage"), dict):
            run["usage"].append(data["usage"])
            run["cost"] += data["usage"].get("cost_usd") or 0.0
            run["traced"] = True
    runs = {k: v for k, v in runs.items() if v["state"]}
    for run in runs.values():
        if not run["traced"]:
            run["cost"] = (run.get("outcome_cost") or 0.0) + run.get("reader_cost", 0.0)

    total = sum(r["cost"] for r in runs.values())
    tokens = {k: sum(u.get(k) or 0 for r in runs.values() for u in r["usage"]) for k in TOKENS}
    out = [f"{len(runs)} runs, ${total:.2f}"
           + (f"; tokens: {tokens['input_tokens']:,} in, {tokens['cache_read_tokens']:,} cache read, "
              f"{tokens['cache_creation_tokens']:,} cache write, {tokens['output_tokens']:,} out" if any(tokens.values()) else "")]
    untraced = [r for r in runs.values() if not r["traced"] and r["cost"]]
    if untraced:
        out.append(f"  ({len(untraced)} runs from before usage lines: cost from their outcome, no tokens)")

    out += ["", "By state:"]
    by_state = defaultdict(list)
    for r in runs.values():
        by_state[r["state"]].append(r["cost"])
    for state, costs in sorted(by_state.items(), key=lambda kv: -sum(kv[1])):
        out.append(f"  {state:10} {len(costs):3} runs  ${sum(costs):7.3f}")

    successes = by_state.get("success", [])
    flawed = by_state.get("flawed", [])
    out += ["", f"Cost per success: ${total / len(successes):.3f} (everything spent / {len(successes)} successes);"
            f" a successful run alone: ${sum(successes) / len(successes):.3f}" if successes else "",
            f"Flawed attempts: {len(flawed)} runs, ${sum(flawed):.3f} ({100 * sum(flawed) / total:.0f}% of the total)"
            if flawed and total else "Flawed attempts: none"]

    out += ["", "By agent:"]
    agents = defaultdict(lambda: {"calls": 0, "cost": 0.0, "turns": 0, "in": 0, "out": 0})
    for r in runs.values():
        for u in r["usage"]:
            a = agents[u.get("agent", "?")]
            a["calls"] += 1
            a["cost"] += u.get("cost_usd") or 0.0
            a["turns"] += u.get("turns") or 0
            a["in"] += sum(u.get(k) or 0 for k in TOKENS[:3])
            a["out"] += u.get("output_tokens") or 0
    for name, a in sorted(agents.items(), key=lambda kv: -kv[1]["cost"]):
        out.append(f"  {name:9} {a['calls']:3} calls  ${a['cost']:7.3f}  {a['turns']:4} turns  "
                   f"{a['in']:>11,} tokens in  {a['out']:>8,} out")
    if untraced:
        out.append(f"  (untraced  {len(untraced)} runs  ${sum(r['cost'] for r in untraced):.3f})")

    out += ["", "By event:"]
    events = defaultdict(list)
    for r in runs.values():
        events[r["event"]].append(r)
    for event, rs in sorted(events.items(), key=lambda kv: -sum(r["cost"] for r in kv[1])):
        title = (titles.get(event) or event)[:70]
        states = " -> ".join(r["state"] for r in rs)
        out.append(f"  ${sum(r['cost'] for r in rs):6.3f}  {states:44} {title}")
    return "\n".join(line for line in out if line is not None)
