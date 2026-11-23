"""Status line of the visible builder: keeps Claude Code's own session data (cost included) and shows one quiet line.

    statusline.py OUT.json LABEL     (Claude Code sends the session's JSON on stdin after each message)
"""
import json
import sys

out, label = sys.argv[1], sys.argv[2]
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except ValueError:
    data = {}
if data:
    with open(out, "w") as f:
        f.write(raw)
model = (data.get("model") or {}).get("display_name") or ""
cost = (data.get("cost") or {}).get("total_cost_usd")
print("  ·  ".join(x for x in (label, model, f"${cost:.3f}" if isinstance(cost, (int, float)) else "") if x))
