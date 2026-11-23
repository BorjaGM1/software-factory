# The engine

Runs one event through the process the contract describes, and logs every step to `runs/events.jsonl`:

```
intake -> triage -> plan -> build (agent) -> scope check -> verify (gate) -> review (human role) -> outcome -> learn
```

Python 3.12, stdlib only. The target is an adapter: everything about renfe-cli is in
`targets/renfe-cli/target.toml` (repo, base, verify commands, protected paths, maintainer-intent markers,
routing by `kind`, capabilities).

## Run it

```sh
bin/factory run factory/examples/evt-s4-checkout.json     # one event file
bin/factory run EVENT.json --retries 1                     # a flawed run is retried once with the gate's findings
bin/factory watch                                          # every new event in runs/inbox/ (skips ids already done)
bin/factory retry RUN_ID                                   # the same event again, told what the gate found
bin/factory correct RUN_ID "what was wrong"                # a human correction: proposes a rule, re-runs the event
bin/factory rules                                          # learned rules, their status and their shell checks
bin/factory approve RULE_ID                                # read the shell check a model wrote for a rule, and let it run
bin/factory gate REF                                       # the gate alone on an existing commit (like prototype/gate.sh)
bin/factory --target targets/NAME/target.toml run EVENT.json   # another target; --target goes before the command
```

The builder is `FACTORY_AGENT_CMD` (default `claude -p`), run headless in the worktree with the prompt on stdin.
For a `claude` command the engine adds what it relies on:

- `--output-format stream-json --verbose` (for the trace, the usage and the proof of its tools), a budget cap
  (`FACTORY_AGENT_BUDGET`, default 2 USD) and `--model $FACTORY_AGENT_MODEL` if set. Without it the builder runs
  on the account's default model.
- `--tools Read,Edit,Write,Glob,Grep,Bash`: the only tools it has.
- `--strict-mcp-config` with an empty `--mcp-config`, `--disable-slash-commands`, `--setting-sources ""` and
  memory off: no connectors (the account's included: they attach in the background, after startup), no skills,
  no user, project or local settings, no memory.
- `--permission-mode dontAsk` with these rules, unless the command chose its own mode: `Read` and `Edit` on
  `<worktree>/**` (they also cover Glob, Grep and Write), and Bash for the target's test command (`verify.test`
  up to its `pytest` word, so `uv run pytest` with any arguments), `git diff`, `git status` and `ls`. Anything
  else is denied; so is a command that names a path outside the worktree.

A command that isn't `claude` is run as given.

```sh
FACTORY_AGENT_MODEL=haiku bin/factory run factory/examples/evt-s8-no-more-trains.json --retries 1
```

## What each part does

- **Target notes**: `[builder] notes` in target.toml go into every builder prompt (for renfe-cli: Renfe's real
  behaviour is only what the repo records; a fix that depends on anything else is `blocked`, not a guess).
- **Triage** (`triage.py`) is deterministic and never reads `untrusted`. In order: `manual_kinds` -> `manual`;
  each `evidence.needs` probed against `[triage.capabilities]` -> `blocked`; `evidence.paths` on a forbidden path
  -> `manual`; a maintainer-intent marker quoted in the evidence -> `manual`; unknown `kind` -> `manual`.
  Otherwise it writes the task. A prebuilt task in `event.task` (from the feedback quarantine) is used as is,
  but the target's forbidden paths are always added and its allowed paths filtered.
- **Builder** (`builder.py`): a detached worktree of the target cut from `base`, under `runs/<run>/wt`. The agent
  sees the task JSON, the active rules and, on a retry, what the gate found. It writes
  `.factory/commit-message.txt`, or `.factory/stop.json` (`blocked` / `manual`, with what it needs) when it can't
  do the task without guessing. Before the gate, the diff is checked: forbidden paths, outside allowed paths,
  removed assertions in existing tests, or lines inside a maintainer-intent region (the `def` holding the marker,
  or its README paragraph) -> `manual`. The worktree is always removed; only `success` keeps a local branch
  `factory/<run>`.
- **Gate** (`gate.py`), the council protocol, each step logged as a `verify` line with its data: scope (one
  commit, `diff --check`); lock; suite; the base's tests against the new source; every new test fails on the base,
  on its assertion; mutants of the added source lines, all killed; the suite with the clock moved +1/+92/+730
  days; startup with no time zone data and no network; for ranking changes, the station sweep before and after
  (silent re-resolutions not named in the message -> `manual`); the message claims only what the gate counted.
  Failures are judged against the base's own (cached per base commit in `runs/cache/`): main has one red test
  today, and the gate doesn't blame the change for it.
- **Review** (`reviewer.py`): every decision a person would make, made by an agent playing the maintainer's
  reviewer and logged as a `review` line (`actor: human`, `data.played_by: claude`). After the gate passes it
  approves (`success`) or requests changes (`flawed`, `human-rejected`; the event runs once more with that
  correction, which proposes a rule, as `factory correct` does). Before a `manual` or `blocked` outcome it answers
  the factory's question with yes or no; the state stays, with `data.decision`. Its own process, no tools
  (`--tools ""`), the same closing flags as the builder, a working directory with nothing but its own prompts,
  and only the task, the commit, the gate's summary and the target's intent notes (or, for a stop, the trusted
  event or the builder's reason). A reviewer that can't prove it had no tools gives no decision
  (`reviewer-unverified`). `FACTORY_REVIEWER_CMD` (default
  `claude -p`), `FACTORY_REVIEWER_MODEL` (default `sonnet`: in replays of one review, haiku twice rejected a sound change on
  invented facts, sonnet approved it twice with the right reasons), `FACTORY_REVIEWER_BUDGET` (0.5 USD);
  `FACTORY_REVIEWER=off` skips it. The factory's own failures (`agent-failed`, `factory-error`) ask nobody.
- **Learning** (`learn.py`): a failed gate step proposes a rule (`rules/rule-<step>.json`) whose check is that step.
  `factory correct` proposes one from the human's text. A proposed rule turns `active` only when its check fails
  on the run it was born from and passes on a later successful run of the same event. Active rules go into
  every builder prompt; rules with an approved `script` check also run as extra gate steps. For a correction
  whose run passed the gate, the checker (a cheap agent with no tools: `FACTORY_CHECKER_CMD`,
  `FACTORY_CHECKER_MODEL`, default `haiku`) writes the `script` check from the correction and the two diffs. That
  command is a proposal: nothing runs it, not even the rule's eval, until `bin/factory approve RULE_ID` has shown
  it to a person. Once approved, if it doesn't fail on the rejected run and pass on the corrected one, the rule
  stays `proposed`. `rm rules/*.json` resets what the factory has learned.

## What the agents do and what they cost

Every agent runs with `--output-format stream-json --verbose`. Its whole stream is kept in
`runs/<run>/<agent>.trace.jsonl` (`builder`, `reviewer`, `checker`; the feedback intake writes `reader`), and
nothing of it leaves the machine. The stream's first event, `init`, lists the agent's tools and MCP servers: the
engine reads it on every call, stops an agent that has anything it shouldn't (or that calls a tool that isn't
its own) and logs what it listed in the agent's last line (`data.init`). While an agent works, each tool call is an `agent` line in `runs/events.jsonl`
with a short summary ("reads src/renfe/search.py", "runs uv run pytest -q"), so the dashboard shows it live; when
it ends, one more line carries its `usage` (tokens in, cache read and write, out, cost, turns, time, permission
denials), and the outcome sums them for the run. `bin/factory costs [--log FILE]` reads those lines: totals, by
state, by agent, by event, cost per success and what the flawed attempts cost. Runs logged before usage lines
count with their outcome's `cost_usd` and no tokens.

To send the same numbers to an observability backend instead, Claude Code has OpenTelemetry built in; nothing in
the factory needs to change. Set, for the agents' environment, `CLAUDE_CODE_ENABLE_TELEMETRY=1`,
`OTEL_METRICS_EXPORTER=otlp`, `OTEL_LOGS_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` (or `http/protobuf`)
and `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317`, and tag runs with
`OTEL_RESOURCE_ATTRIBUTES=factory.run_id=<run>,factory.agent=builder`. It exports the
`claude_code.token.usage` (attribute `type`: `input`, `output`, `cacheRead`, `cacheCreation`) and
`claude_code.cost.usage` metrics, and a `claude_code.api_request` event per request with tokens and cost. Prompts,
tool inputs and tool output are left out unless `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` or
`OTEL_LOG_TOOL_CONTENT` is set to `1`; keep them off for the reader, whose input is untrusted. Names from
https://code.claude.com/docs/en/monitoring-usage (checked 22 Nov 2026).

## Watching the builder

`FACTORY_BUILDER_VISIBLE=1 bin/factory run EVENT` runs the same builder as an interactive Claude Code in a new
[keepty](https://crates.io/crates/keepty) session named after the run (`cargo install keepty`), in the run's
worktree. The factory pastes the same prompt, waits for the turn to end (the transcript's `turn_duration` line),
leaves the last message on screen for a few seconds and leaves with Ctrl-D (`/exit` is a slash command, and
those are off). The whole session is recorded from its first
byte to `runs/<run>/builder.cast` (asciinema v2, 100x40, `start_epoch` in the header, the same clock as the log's
`ts`); `agg runs/<run>/builder.cast out.gif` renders it. `keepty attach --watch <run>` shows it live.

It gets the same tools, closing flags and permission rules as the headless builder. What changes, because
interactive Claude Code can't take `-p`'s flags: there is no `init` event to prove its tools with, so only a
call to a tool that isn't the builder's stops it; `FACTORY_AGENT_BUDGET` is checked against
Claude Code's own running cost and the turn is interrupted past it; the session is kept on disk, because its
transcript (`~/.claude/projects/...`, copied to `builder.trace.jsonl`) is where the `agent` lines and the usage
come from. Tool lines appear as each call lands in the transcript. The cost is Claude Code's own (`cost_source:
"claude-code"`); if a transcript has none, tokens are priced from the list prices in `factory/visible.py`
(`"list-price"`). A new folder's trust question is answered "yes": the worktree is the factory's own throwaway copy.

Quiet for video, with documented switches only: `spinnerTipsEnabled: false`, `promptSuggestionEnabled: false`,
`companyAnnouncements: []`, a status line of its own (`factory/statusline.py`: "Claude (builder) · model · $cost",
which also replaces the shortcuts hint), and `DISABLE_AUTOUPDATER`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY`,
`CLAUDE_CODE_HIDE_CWD`, `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (with it
the startup notices didn't show in our runs). The small logo and the mode line ("don't ask on") stay: no setting hides them.

A feedback rejected at intake has no builder. `python3 demo/show_feedback.py FILE --highlight "TEXT"` shows the raw
feedback in a keepty TUI with the lines containing TEXT marked, then runs the real intake on it and shows the
verdict, recorded to `runs/<run>/feedback.cast` the same way.

## Files a run leaves

A run is `run-<event id without evt->`, the id the intakes already log under, so an event stays one run on the
dashboard; a retry or a re-run of the same event gets `-2`, `-3`... The engine skips its own intake line when an
intake already wrote one for that run.

`runs/<run>/`: `event.json`, `task.json`, `prompt.txt`, `agent.json` (the builder's reply and usage),
`<agent>.trace.jsonl`, `builder.cast` and `builder.status.json` (visible builder), `review/` (the reviewer's prompts and replies), `diff.patch`,
`gate.json`, `sweep.json`, `stop.json`, `outcome.json`.

`runs/fixtures/` has the log lines of the demo scenarios that really ran on 21 Nov 2026 (haiku):
`scenario-4-manual`, `scenario-8-flawed-then-rule`, `scenario-9-blocked` (the builder asks for real Renfe data,
then a `needs: container` event stopped at triage), `human-correction-rule`. `factory/examples/` has their events.
