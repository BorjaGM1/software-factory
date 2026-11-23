"""Running an agent with its stream: the full trace on disk, one callback per tool call, and its usage at the end.

claude's `--output-format stream-json --verbose` prints one JSON object per line: `system/init`, `assistant`
messages (their `tool_use` blocks are the tool calls), `user` messages (tool results) and a final `result` with
cost, turns, duration, permission denials and `modelUsage` (tokens per model, cumulative)."""

import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path


# For a `claude` agent: no MCP servers (the account's connectors included), no skills or slash commands, no user,
# project or local settings, no memory. What is left is what the factory's own command names.
CLOSED = ["--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}', "--disable-slash-commands",
          "--setting-sources", ""]
NO_MEMORY = {"autoMemoryEnabled": False}


def is_claude(cmd: list[str]) -> bool:
    return Path(cmd[0]).name == "claude"


def closed(cmd: list[str], tools: list[str]) -> list[str]:
    """A `claude` command with only these built-in tools (none for []) and nothing of the user's account."""
    return cmd + ["--tools", ",".join(tools), *CLOSED, "--settings", json.dumps(NO_MEMORY)]


def no_tools_cmd(base: str, model: str, budget: str) -> list[str]:
    """The command of an agent that only reads its prompt and answers (the reviewer, the checker). For `claude`:
    the stream, a model, a budget, no tools and nothing of the user's account. Anything else is run as given."""
    cmd = shlex.split(base)
    if not is_claude(cmd):
        return cmd
    if "--model" not in cmd and model:
        cmd += ["--model", model]
    cmd = claude_stream_flags(cmd)
    if "--max-budget-usd" not in cmd:
        cmd += ["--max-budget-usd", budget]
    return closed(cmd, []) + ["--no-session-persistence"]


def run_agent(prompt: str, cwd, timeout: int = 1500, *, cmd: list[str], agent: str = "builder",
              trace: Path | None = None, on_tool=None, tools: list[str] | None = None) -> dict:
    """{ok, exit, text, cost_usd, turns, model, denials, usage, init}. `on_tool(summary, tool)` fires live per tool
    call.

    With `tools` (the only tools this agent may have; [] for none) the run has to prove it, as the feedback reader
    does: claude's own init event must list no other tool and no MCP server, and a call to any other tool kills the
    process. Without that proof nothing the agent said is used: `ok` is False, `text` is empty and `unverified`
    says why."""
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=os.environ.copy())
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    lines, result, init, problem = [], None, None, None
    sink = trace.open("a") if trace else None  # an agent called twice in a run appends
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            lines.append(line)
            if sink:
                sink.write(line)
                sink.flush()
            event = _json(line)
            if not event:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                init = {k: event.get(k) for k in ("tools", "mcp_servers", "model")}
                if tools is not None and (not isinstance(init["tools"], list) or set(init["tools"]) - set(tools)
                                          or init["mcp_servers"] != []):
                    problem = f"it started with tools {init['tools']} and MCP servers {init['mcp_servers']}"
            elif event.get("type") == "assistant":
                calls = [b for b in (event.get("message") or {}).get("content") or []
                         if b.get("type") in ("tool_use", "server_tool_use")]
                if tools is not None and init is None:
                    problem = "it answered before reporting its tools"
                elif tools is not None and {b.get("name") for b in calls} - set(tools):
                    problem = f"it called a tool it may not have: {sorted({str(b.get('name')) for b in calls} - set(tools))}"
                elif on_tool:
                    for block in calls:
                        on_tool(summarize(block.get("name", "?"), block.get("input") or {}, cwd), block.get("name"))
            elif event.get("type") == "result":
                result = event
            if problem:
                proc.kill()
                break
        code = proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
        if sink:
            sink.close()
    out = "".join(lines)
    if result is None:  # a command that printed one JSON document (--output-format json) or plain text
        start = out.find("{")
        result = _json(out[start:]) if start >= 0 else None
    elapsed = int((time.monotonic() - started) * 1000)
    if tools is not None and init is None and not problem:
        said = out.strip().splitlines()[-1][:200] if code and out.strip() else ""  # a failed command's own error
        problem = "it never reported its tools" + (f" ({said})" if said else "")
    if problem:
        return {"ok": False, "exit": code, "text": "", "cost_usd": 0.0, "unverified": problem, "init": init,
                "usage": usage_of({}, agent, elapsed)}
    if not isinstance(result, dict):
        return {"ok": code == 0, "exit": code, "text": out.strip()[-4000:], "cost_usd": 0.0, "init": init,
                "usage": usage_of({}, agent, elapsed)}
    usage = usage_of(result, agent, elapsed)
    return {"ok": code == 0 and not result.get("is_error"), "exit": code, "text": str(result.get("result", ""))[-4000:],
            "cost_usd": usage["cost_usd"], "turns": usage["turns"], "model": usage["model"],
            "denials": usage["permission_denials"], "usage": usage, "init": init}


def usage_of(result: dict, agent: str, elapsed_ms: int) -> dict:
    """The contract's usage shape, from claude's final `result` (tokens summed over every model it used)."""
    models = result.get("modelUsage") or {}
    total = lambda key: sum(int(m.get(key) or 0) for m in models.values())
    main = max(models, key=lambda m: models[m].get("costUSD") or 0) if models else None
    u = result.get("usage") or {}
    return {"agent": agent, "model": main,
            "input_tokens": total("inputTokens") if models else int(u.get("input_tokens") or 0),
            "output_tokens": total("outputTokens") if models else int(u.get("output_tokens") or 0),
            "cache_read_tokens": total("cacheReadInputTokens") if models else int(u.get("cache_read_input_tokens") or 0),
            "cache_creation_tokens": total("cacheCreationInputTokens") if models
            else int(u.get("cache_creation_input_tokens") or 0),
            "cost_usd": round(float(result.get("total_cost_usd") or 0), 6), "turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms") or elapsed_ms,
            "permission_denials": len(result.get("permission_denials") or [])}


def add(total: dict | None, usage: dict) -> dict:
    """Sum of usages, for a run's outcome; `agent` and `model` become lists."""
    total = dict(total or {"agents": [], "models": []})
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "duration_ms",
                "permission_denials", "turns"):
        total[key] = (total.get(key) or 0) + (usage.get(key) or 0)
    total["cost_usd"] = round((total.get("cost_usd") or 0) + (usage.get("cost_usd") or 0), 6)
    total["agents"] = sorted(set(total["agents"]) | {usage["agent"]})
    total["models"] = sorted(set(total["models"]) | ({usage["model"]} if usage.get("model") else set()))
    return total


def describe(usage: dict) -> str:
    tokens = usage["input_tokens"] + usage["cache_read_tokens"] + usage["cache_creation_tokens"]
    return (f"{usage['agent']} done ({usage.get('model') or '?'}): {usage.get('turns') or 0} turns, "
            f"{tokens:,} tokens in, {usage['output_tokens']:,} out, ${usage['cost_usd']:.3f}, "
            f"{(usage.get('duration_ms') or 0) / 1000:.0f}s")


def summarize(tool: str, args: dict, cwd) -> str:
    """One short, safe line: the action and its target, never free text the agent wrote."""
    def rel(p):
        p = str(p or "")
        try:
            return str(Path(p).resolve().relative_to(Path(cwd).resolve()))
        except (ValueError, OSError):
            return Path(p).name or p
    def short(s, n=70):
        s = " ".join(str(s or "").split())
        s = re.sub(r"(?i)(token|key|secret|password)=\S+", r"\1=…", s)
        return s if len(s) <= n else s[: n - 1] + "…"
    match tool:
        case "Read":
            return f"reads {rel(args.get('file_path'))}"
        case "Edit" | "MultiEdit":
            return f"edits {rel(args.get('file_path'))}"
        case "Write":
            return f"writes {rel(args.get('file_path'))}"
        case "Glob":
            return f"lists files matching {short(args.get('pattern'), 40)}"
        case "Grep":
            return f"searches for {short(args.get('pattern'), 40)}" + (f" in {rel(args['path'])}" if args.get("path") else "")
        case "Bash":
            return f"runs {short(str(args.get('command', '')).splitlines()[0] if args.get('command') else '')}"
        case _:
            return f"uses {tool}"


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def claude_stream_flags(cmd: list[str]) -> list[str]:
    """For a `claude` command: stream-json with --verbose, unless the caller chose an output format."""
    if not is_claude(cmd) or "--output-format" in cmd:
        return cmd
    return cmd + ["--output-format", "stream-json", "--verbose"]

