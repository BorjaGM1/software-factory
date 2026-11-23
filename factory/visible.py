"""The builder you can watch: an interactive Claude Code in a keepty session named after the run, recorded to
runs/<run>/builder.cast. FACTORY_BUILDER_VISIBLE=1 selects it; headless `claude -p` stays the default.

It gets the same prompt, model, tools, permission rules and budget as the headless builder, and as little of the
user's account: no connectors, skills, settings files or memory. Interactive Claude Code can't take `-p`'s flags,
so: the budget is watched from Claude Code's own cost and the turn is interrupted past it, and the session is kept
on disk because its transcript is where the `agent` lines and the usage come from. It has no init event to prove
its tools with; a call to a tool that isn't the builder's stops it.
"""

import json
import os
import shutil
import time
import uuid
from collections import Counter
from pathlib import Path

from .builder import TOOLS
from .keepty import Session
from .trace import CLOSED, NO_MEMORY, summarize, usage_of

ROOT = Path(__file__).resolve().parent.parent
# Quiet for video, only with documented switches. The logo and the mode line ("don't ask on") still show.
QUIET_ENV = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1", "CLAUDE_CODE_HIDE_CWD": "1",
             "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
# $ per million tokens: input, output, cache read. Cache writes: 1.25x input (5 min), 2x input (1 h).
PRICES = {"claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-sonnet-5-5": (2.0, 10.0, 0.10),
          "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-fable-5-2": (8.0, 40.0, 0.20)}


def enabled() -> bool:
    return os.environ.get("FACTORY_BUILDER_VISIBLE", "") not in ("", "0", "off")


def command(session_id: str, status: Path, label: str, rules: list[str]) -> list[str]:
    settings = {**NO_MEMORY, "spinnerTipsEnabled": False, "promptSuggestionEnabled": False, "companyAnnouncements": [],
                "statusLine": {"type": "command", "padding": 0,
                               "command": f"python3 {ROOT / 'factory' / 'statusline.py'} {status} '{label}'"}}
    cmd = [os.environ.get("FACTORY_BUILDER_VISIBLE_CMD") or "claude", "--session-id", session_id]
    if os.environ.get("FACTORY_AGENT_MODEL"):
        cmd += ["--model", os.environ["FACTORY_AGENT_MODEL"]]
    return cmd + ["--tools", ",".join(TOOLS), *CLOSED, "--permission-mode", "dontAsk", "--allowedTools", *rules,
                  "--settings", json.dumps(settings)]


def run_agent(prompt: str, cwd, run_dir: Path, session: str, rules: list[str], timeout: int = 1500, on_tool=None,
              agent: str = "builder") -> dict:
    """Same shape as trace.run_agent: {ok, exit, text, cost_usd, turns, model, denials, usage}. `rules` are the
    builder's permission rules (builder.allowed_tools)."""
    sid, started = str(uuid.uuid4()), time.monotonic()
    status, cast = run_dir / f"{agent}.status.json", run_dir / f"{agent}.cast"
    budget = float(os.environ.get("FACTORY_AGENT_BUDGET", "2"))
    env = dict(QUIET_ENV, **{k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "PATH", "HOME") if k in os.environ})
    seen, transcript, stopped, rogue = set(), None, None, None
    with Session(session, command(sid, status, f"Claude ({agent})", rules), cwd, cast, env) as s:
        _wait_ready(s)
        s.send(b"\x1b[200~" + prompt.encode() + b"\x1b[201~")  # a paste: the TUI takes it as one message
        time.sleep(1.5)
        s.send(b"\r")
        deadline = time.time() + timeout
        while True:
            time.sleep(1)
            transcript = transcript or _transcript(sid)
            entries = _entries(transcript)
            for block in _tool_uses(entries):
                if block["id"] not in seen:
                    seen.add(block["id"])
                    if block.get("name") not in TOOLS:
                        rogue = stopped = f"it called a tool it may not have: {block.get('name')}"
                    elif on_tool:
                        on_tool(summarize(block.get("name", "?"), block.get("input") or {}, cwd), block.get("name"))
            if rogue:
                s.send(b"\x1b")  # interrupt the turn
                time.sleep(3)
                break
            if any(e.get("type") == "system" and e.get("subtype") == "turn_duration" for e in entries):
                break  # the turn is over
            if not s.alive():
                stopped = "the session ended before the turn did"
                break
            if _status_cost(status) > budget or time.time() > deadline:
                stopped = "over budget" if _status_cost(status) > budget else "timed out"
                s.send(b"\x1b")  # interrupt the turn, as -p would stop
                time.sleep(3)
                break
        time.sleep(3)  # let the last message sit on screen
        if s.alive():
            s.send(b"\x04\x04")  # Ctrl-D twice; `/exit` is a slash command, and those are off
            for _ in range(20):
                if not s.alive():
                    break
                time.sleep(0.5)
    entries = _entries(transcript)
    if transcript:
        shutil.copyfile(transcript, run_dir / f"{agent}.trace.jsonl")
    usage = _usage(entries, agent, int((time.monotonic() - started) * 1000))
    usage["cast"] = cast.name
    texts = [b.get("text", "") for e in entries if e.get("type") == "assistant"
             for b in (e.get("message") or {}).get("content") or [] if b.get("type") == "text"]
    return {"ok": stopped is None and bool(entries), "exit": 0 if stopped is None else 1,
            "text": ((texts[-1] if texts else "") + (f"\n[{stopped}]" if stopped else ""))[-4000:],
            "cost_usd": usage["cost_usd"], "turns": usage["turns"], "model": usage["model"],
            "denials": usage["permission_denials"], "usage": usage,
            **({"unverified": rogue} if rogue else {})}


def _wait_ready(s: Session, limit: float = 45):
    """Until the input box is up and the screen is still; answers the folder-trust question if it comes."""
    end, trusted = time.time() + limit, False
    while time.time() < end:
        time.sleep(0.5)
        tail = s.text[-2500:]
        if trusted:
            tail = tail.rsplit("trust this folder", 1)[-1].split("Esc to cancel", 1)[-1]  # only what came after
        elif "trust this folder" in tail:  # the worktree is the factory's own throwaway copy of the target
            if s.quiet_for() > 1:
                s.send(b"\x1b[B")  # the default is "No, exit"; one down is "Yes, I trust this folder"
                s.send(b"\r")
                trusted = True
            continue
        if "❯" in tail and s.quiet_for() > 1.5:
            return


def _transcript(sid: str) -> Path | None:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    found = list(home.glob(f"projects/*/{sid}.jsonl"))
    return found[0] if found else None


def _entries(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # a line being written
    return out


def _tool_uses(entries):
    for e in entries:
        if e.get("type") == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use" and block.get("id"):
                    yield block


def _status_cost(status: Path) -> float:
    try:
        return float(json.loads(status.read_text())["cost"]["total_cost_usd"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _usage(entries: list[dict], agent: str, elapsed_ms: int) -> dict:
    """From the transcript: Claude Code's own `cost-state` when it wrote one (cost and tokens per model, as in
    -p's result), else the assistant messages' usage priced from PRICES."""
    messages = {}
    for e in entries:
        m = e.get("message") or {}
        if e.get("type") == "assistant" and m.get("id"):
            messages[m["id"]] = m  # one message is written once per content block, with the same usage
    denials = sum(1 for e in entries if e.get("type") == "user" for b in (e.get("message") or {}).get("content") or []
                  if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
                  and "denied" in str(b.get("content")).lower())
    states = [e for e in entries if e.get("type") == "cost-state"]
    if states:
        st = states[-1]
        result = {"modelUsage": st.get("modelUsage") or {}, "total_cost_usd": st.get("totalCostUSD"),
                  "num_turns": len(messages), "duration_ms": st.get("totalDuration") or elapsed_ms,
                  "permission_denials": [None] * denials}
        return {**usage_of(result, agent, elapsed_ms), "cost_source": "claude-code"}
    by_model = {}
    for m in messages.values():
        u, t = m.get("usage") or {}, by_model.setdefault(m.get("model") or "?", Counter())
        t["inputTokens"] += u.get("input_tokens") or 0
        t["outputTokens"] += u.get("output_tokens") or 0
        t["cacheReadInputTokens"] += u.get("cache_read_input_tokens") or 0
        cc = u.get("cache_creation") or {}
        t["cache1h"] += cc.get("ephemeral_1h_input_tokens") or 0
        t["cacheCreationInputTokens"] += u.get("cache_creation_input_tokens") or 0
    unpriced = [m for m in by_model if m not in PRICES]
    for model, t in by_model.items():
        pin, pout, pread = PRICES.get(model, (0, 0, 0))
        five_min = t["cacheCreationInputTokens"] - t["cache1h"]
        t["costUSD"] = (t["inputTokens"] * pin + t["outputTokens"] * pout + t["cacheReadInputTokens"] * pread
                        + five_min * pin * 1.25 + t["cache1h"] * pin * 2) / 1e6
    result = {"modelUsage": by_model, "total_cost_usd": sum(t["costUSD"] for t in by_model.values()),
              "num_turns": len(messages), "duration_ms": elapsed_ms, "permission_denials": [None] * denials}
    return {**usage_of(result, agent, elapsed_ms), "cost_source": "list-price" + (f" (no price for {', '.join(unpriced)})"
                                                                                   if unpriced else "")}
