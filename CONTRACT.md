# Contract

The shared interfaces of the factory: what an event (a notice), a task, a line of the run log and an outcome
look like. Every part (engine, intakes, dashboard) builds against this file and nothing else of the other parts.
To run the factory, start with the [README](README.md).

## What this is

A small software factory, shown working on a real open-source project: renfe-cli
(https://github.com/luisgonzaleznf/renfe-cli, a JSON-first Python CLI for Renfe, Spain's railways). The target
project is an adapter (`targets/renfe-cli/`), not code spread through the engine.

The demo forces its events (canaries, chaos in a twin of Renfe, seeded user feedback), and every run leaves a
readable trace: `runs/events.jsonl`, which `dashboard/` follows.

## Rules every part keeps

- **The target repo is read-only.** The factory works in its own throwaway git worktrees of your clone, cut
  from the target's `base`, and deletes them after. What it leaves there are local `factory/<run>` branches.
- **Nothing leaves the machine.** No push, no PR, no issue, no message to anyone.
- **The demo never calls Renfe.** No live tests (`RENFE_LIVE`), no network to Renfe hosts, no checkout or
  payment code executed. Renfe is the twin (`tests/fakes.py` in the target, extended under
  `targets/renfe-cli/twin/`).
- **The agent command is configurable** (`FACTORY_AGENT_CMD`, default `claude -p`).
- **No AI attribution in git:** the engine strips `Co-Authored-By` trailers and "Generated with" lines from
  the builder's commit message.
- Python 3.12, stdlib first, `uv` for anything else. Short, plain code; comments only where the code can't
  say it.

## Layout

| Path | What |
|---|---|
| `factory/` | runs one event through the process, the verifier gate, the learning loop |
| `rules/` | learned rules, each with the run that created it and the check that enforces it (local, git-ignored) |
| `targets/renfe-cli/target.toml` | repo path, base ref, verify commands, protected paths, maintainer-intent markers |
| `targets/renfe-cli/twin/` | chaos modes on top of the target's `tests/fakes.py` |
| `targets/renfe-cli/canaries/` | synthetic monitoring checks run against the twin |
| `targets/renfe-cli/rules-example/` | the rules the demo learned, as examples |
| `intake/monitoring/` | turns a failed canary into an event |
| `intake/feedback/` | turns user feedback into an event, through the quarantine |
| `dashboard/` | reads `runs/events.jsonl`, shows what is happening |
| `runs/` | output; `runs/fixtures/` holds sample logs for the dashboard |

## Event: what starts a run

One JSON file per event, in `runs/inbox/<id>.json`:

```json
{
  "id": "evt-20261121-0001",
  "source": "monitoring | feedback | ci | canary | human",
  "kind": "short-slug, e.g. api-shape-changed, feature-request, bug-report, clock-canary",
  "title": "one line, written by the intake (trusted)",
  "evidence": {"...": "machine facts: failing check, exit code, stderr tail, dates"},
  "untrusted": "raw text from outside (feedback body, issue text), or null",
  "created_at": "ISO 8601"
}
```

`untrusted` is never shown to an agent that has tools. Only the quarantine reader sees it.

## Task: what the builder agent sees

The engine (or the quarantine, for feedback) turns an event into a task. The builder never sees anything else:

```json
{
  "id": "task-…", "event_id": "evt-…",
  "type": "fix | add | investigate",
  "summary": "what to do, in the factory's own words",
  "acceptance": ["observable facts that must hold after the change"],
  "allowed_paths": ["src/renfe/…", "tests/…", "README.md"],
  "forbidden_paths": ["src/renfe/checkout*", ".github/", "tests/fakes.py", "…"]
}
```

## Run log: `runs/events.jsonl`

Append-only, one JSON object per line. This is the only thing the dashboard reads.

```json
{"ts": "ISO 8601", "run_id": "run-…", "event_id": "evt-…", "step": "intake|triage|plan|build|agent|verify|review|learn|outcome",
 "actor": "agent|check|human|intake", "status": "started|ok|failed|info",
 "detail": "one human-readable line", "data": {"optional": "structured extras, e.g. gate step results, diff stat, cost"}}
```

A run's first line carries `data.title` (and `data.source`, `data.kind`) so a viewer needs no other file. A gate
check is a `verify` line whose `data` is `{"step": "<check name>", "ok": true|false, ...}`; the gate's summary is
the last `verify` line of the run. A learned rule is a `learn` line with `data.rule` (its id), `data.text` and
`data.born_from` (the run id).

Every agent (`builder`, `reviewer`, `reader` (the feedback quarantine), `checker` (writes a rule's check)) runs with
claude's `--output-format stream-json --verbose`; its full stream is kept in `runs/<run>/<agent>.trace.jsonl`, which
never leaves the machine. Each run has to prove what it had: the stream's `init` event must list only that agent's
tools (none for the reviewer, the checker and the reader) and no MCP server, and a call to any other tool stops it.
Without that proof nothing the agent said is used, and the run ends `blocked` (`builder-unverified`,
`reviewer-unverified`, `quarantine-unverified`). While it works, each tool call is a `step: "agent"` line, `actor: "agent"`, `status:
"info"`, with a short safe `detail` ("reads src/renfe/search.py", "edits tests/test_cli.py", "runs uv run pytest
-q", at most ~70 characters, no free text the agent wrote) and `data: {"agent": "builder", "tool": "Read"}`. The
reader, whose input is untrusted, logs no tool lines (it has no tools) and no text. When an agent ends, one more
`agent` line carries `data.agent`, `data.usage` and, for the engine's agents, `data.init` (the tools, MCP servers
and model its `init` event listed):

```json
{"agent": "builder|reviewer|reader|checker", "model": "claude-haiku-5-5", "input_tokens": 0, "output_tokens": 0,
 "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost_usd": 0.0, "turns": 0, "duration_ms": 0,
 "permission_denials": 0}
```

Tokens are summed over every model the agent used. The outcome line's `data.usage` is the sum over the run's agents
so far (same keys; `agents` and `models` are lists), and its `data.cost_usd` equals `data.usage.cost_usd`. A
`checker` that runs after the outcome is counted only in its own line. `factory costs` reports from these lines.
The visible builder (`FACTORY_BUILDER_VISIBLE=1`) logs the same lines, read from its Claude Code transcript, and adds
two keys to its usage: `cost_source` (`"claude-code"`: Claude Code's own cost record in the transcript;
`"list-price"`: tokens times list price, when the transcript has none) and `cast` (its recording, in the run's
folder).

A decision a person would make is a `review` line with `actor: "human"`. Until a real person is in the loop it is
played by an agent (its own prompt and process, no tools and nothing of the user's account,
`FACTORY_REVIEWER_CMD` / `FACTORY_REVIEWER_MODEL`), and the line says so: `data.played_by: "claude"`, `data.decision`, `data.reason`. Two moments ask for one:

- After the gate passes, the reviewer sees only the task, the commit (message and diff), the gate's summary and the
  target's maintainer-intent notes, and decides `approve` (-> `success`) or `request-changes` (-> `flawed`, reason
  `human-rejected`, `data.correction`). A rejection goes the way of a human correction: the event runs again with
  it, and it proposes a rule.
- Before a `manual` or `blocked` outcome, the same role answers the factory's question (`data.question`, e.g.
  "this touches checkout: go ahead?", "we need real Renfe data: can you provide it?") with `yes` or `no`. The
  outcome keeps its state and carries `data.decision` and `data.decision_reason`.

A correction carried into a new run (`factory correct`, or a reviewer's rejection) is a `review` line at the start
of that run with `data.run_id` (the rejected run) and `decision: "request-changes"`; the rejected run keeps its
single outcome.

Every run ends with exactly one `step: "outcome"` line (`learn` lines may follow it) whose `data.state` is one of:

- `success`: the verifier gate passed and the reviewer approved; a disposable commit on a local branch is ready
  for a human to merge.
- `flawed`: the change failed the gate, or the reviewer rejected it (`human-rejected`). Feeds the learning loop.
- `blocked`: the factory lacks something it needs (access, real data, a tool). Says exactly what.
- `manual`: the change touches a decision that belongs to the maintainer (a protected path, a rule the
  maintainer wrote on purpose, a policy change). Stops and asks.

An event can also end at intake with `data.state = "rejected"` (for example, a suspected prompt injection).
It never reaches a builder. `data.reason` is a short slug in every outcome.

## The verifier gate

Deterministic, owned by the factory, never editable by the builder (the builder's worktree diff is checked
against `forbidden_paths` before anything runs). In short: clean diff and lockfile; the full suite; the base's
tests against the new source; every new test fails on the base; the suite with the clock shifted (+1/+92/+730
days for renfe-cli); startup without system time zone data; for ranking changes, a before/after sweep over all
station queries; mutants of the change must be killed; the commit message only claims what the gate re-ran. A
target that sets no lock commands, clock shifts or startup commands skips those steps.

## Learning loop

A `flawed` outcome, or a human correction, produces a proposed rule in `rules/`:
`{id, born_from: run_id, text, check, status: proposed|active}`. A rule becomes `active` only when its check
fails on the flawed run and passes on the corrected one ("eval first"). Active rules are added to the gate
or to the builder's instructions.

A check is either a gate step (`{"type": "gate-step", "step": ...}`) or a shell command
(`{"type": "script", "cmd": ...}`). A correction's command is written by the `checker`, a model with no tools,
and is only a proposal: neither the eval nor the gate runs a `script` check without `check.approved_at`, which
`factory approve RULE_ID` sets after showing the command to a person.

## The demo's scenarios

| # | Scenario | Expected end |
|---|---|---|
| 1 | Monitoring: a canary against the twin catches "Renfe changed its API" (shape drift) and the factory adapts the parser | success |
| 2 | Monitoring: Renfe twin down / rate-limited → the CLI's behaviour is right, nothing to fix | (no run, or success with no change) |
| 3 | Canary with a real date: the clock reaches 25 Jan 2027, when the legacy shop is to be removed (issue #1 of the target) | manual |
| 4 | A fix touches the checkout path → stops | manual |
| 5 | User feedback asks for something small → added | success |
| 6 | User feedback reports something small → fixed | success |
| 7 | User feedback carries a prompt injection → never reaches the builder | rejected |
| 8 | A fix that passes the tests but fails the gate (e.g. a test that passes before and after) → a rule is learned | flawed → rule |
| 9 | Needs a container or real data it can't get | blocked |
