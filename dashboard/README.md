# dashboard

A local page that follows `runs/events.jsonl` live and shows what the factory is doing: the run in
focus moving through its steps (and who acts at each), what its agent is doing right now, its final
state, the gate's checks for it, the rules the factory has learned and from which run, counters by
state, and what it all cost.

Stdlib Python, no build step, no external calls. It listens on 127.0.0.1 only.

## Run it

```bash
python3 dashboard/serve.py                       # follow runs/events.jsonl
python3 dashboard/serve.py --replay              # replay the synthetic fixture, real time
python3 dashboard/serve.py --replay --speed 3 --loop
python3 dashboard/serve.py --replay --fixture runs/fixtures/real-events.jsonl --loop   # the runs that really happened
```

Then open http://127.0.0.1:8765. Made for half of a 1920x1080 screen (960x1080), dark; a shorter window
is scaled down to fit, and in a narrow browser pane (480-720 px) the page stacks and scrolls.
For recording, open http://127.0.0.1:8765/?present=1 (presentation mode, below) and see
[`demo/rec_dash.js`](../demo/rec_dash.js).

## How it behaves

- **Focus is stable.** The page stays on a run until its outcome, holds the finished run 4.5 s (2.5 s
  when other runs are waiting, 1.3 s when three or more are), then moves to the oldest run it hasn't
  shown yet. Another run logging a line never takes the focus mid-run. Click a run to pin it; Esc unpins.
  A page opened mid-log starts on the oldest run still going, else the latest.
- **Nothing changes size.** Every block has a fixed, reserved size at 960x1080: the counters, the focus
  (two lines kept for the title whether it uses them or not), the track, the "now" box, a two-line feed,
  the gate (up to 14 checks in two columns, three columns past that), runs and rules. Text truncates or
  scrolls inside; boxes never grow, shrink or jump. The money line keeps its place before anything costs.
- **One "now" box.** Under the track, one box of fixed size shows one thing at a time: the human's
  question and answer side by side, else the agent at work (its last three tool calls, newest on top,
  older ones fading), else how the run ended, else "agent · idle".
- **Only what changed moves.** Elements are patched in place by key; one that didn't change isn't
  touched, and only changed ones animate. Moving to another run fades the panels once instead.
- **Cost.** On the top line: total spent, spent per success (everything spent, divided by the
  successes), and what flawed runs cost ("failed attempts"). Each run shows its cost in the runs list and
  its cost, tokens and turns in the focus.
- **Long text stays in its box.** Titles and the newest rule are clamped to two lines, feed lines and older
  rules to one, checks and ids get an ellipsis, slugs read as words (`new-tests-fail-on-base` → "new tests
  fail on base"), and a rule check shows the rule's text. Runs and rules fade out at the bottom when they
  don't fit, so the page never scrolls.

## Presentation mode

`?present=1` paces the display, not the log: lines that arrive after the page has loaded wait in a queue
and are shown one by one, so a change stays on screen before the next one replaces it, even when the log
moves faster. The log is never touched. How long each kind of change stays (`PACE` in `app.js`):

| Change in the run in focus | Stays at least |
|---|---|
| a step changes state, the outcome | 2 s |
| a gate check passes (they come in bursts, and stay listed) | 0.7 s |
| a gate check fails | 2.2 s |
| the human's question or decision | 2.5 s |
| a new or updated rule (any run) | 2.5 s |
| a feed line, a tool call of the agent at work | 1.5 s, 0.9 s |
| moving to another run | 1.5 s, after holding the finished run 5 s |

While a run is in focus, the next runs' lines wait, so each run is seen from its first step. If the display
falls more than 30 s behind the log, it catches up at the next change of run, never mid-run: everything
older than 4 s is shown at once (runs that end inside the jump count as shown), and pacing resumes from
there. A run in focus that says nothing for 45 s while others wait lets the next one in.

`window.__shown` records when each line was shown (epoch ms, with its run, step, `ts` and `event_id`), plus
catch-ups and stalls; the recorder saves it as `display.jsonl`, which is how a moment in the footage is
matched to the log, since the display runs behind it by design.

## Pointing at things in the edit

Stable ids for the blocks a callout points at. The layout doesn't move, so the recorder's `layout.json`
(their boxes in video pixels) holds for a whole recording:

| What | Selector |
|---|---|
| counters, and one state's | `#counters`, `#count-running`, `#count-success`, `#count-flawed`, `#count-blocked`, `#count-manual`, `#count-rejected` |
| cost line, and each figure | `#money`, `#money-spent`, `#money-per-success`, `#money-failed-attempts` |
| run in focus | `#focus` (with `data-run` and `data-state`), `#run-title`, `#run-state` (the badge), `#run-cost` |
| the track, one step | `#track`, `#step-intake` … `#step-outcome` |
| the "now" box | `#now` (with `data-mode`: `ask`, `activity`, `said`, `idle`); the question `#ask-q` and the human's decision `#ask-a` are its two halves; the agent at work `#activity` |
| the feed | `#feed` |
| gate checks | `#gate`, `#gate-title`, `#checks`; one check `#checks [data-k="mutants"]` (its name), with class `ok`, `failed`, `started`, `rule` |
| runs, learned rules | `#runs-panel`, `#runs`; `#rules-panel`, `#rules`, one rule `#rules [data-k="<rule id>"]` (the newest is first) |

`--replay` truncates `runs/replay/events.jsonl`, waits `--delay` seconds (default 3, time to put the
page on screen), then appends `runs/fixtures/demo-events.jsonl` to it at the fixture's own pace
divided by `--speed`, with each `ts` rewritten to now. It never writes to `runs/events.jsonl`.
To replay into another file while something else serves it:

```bash
python3 dashboard/replay.py --to runs/events.jsonl --speed 2
```

`--log PATH` follows any other file. If the file shrinks or is replaced, the page starts over.

## What it reads

Only the run log format in [CONTRACT.md](../CONTRACT.md). Lines are grouped by `run_id` (or
`event_id` when there is none); a line with neither is ignored, except that `data.synthetic: true`
shows a "synthetic replay" tag. The contract leaves a few shapes open; this is what the page takes:

| What | Where it looks |
|---|---|
| run title | `data.title` on any line, else the first `intake` line's `detail` |
| source, kind | `data.source`, `data.kind` |
| gate checks | one per line: `data.check`, or `data.step` on a `verify` line, with `data.ok` (bool) or the line's `status`; several at once: `data.checks` or `data.steps`, as a list of `{check\|step\|name, ok\|status, detail}` or a `{name: status}` map |
| learned rules | `data.rule` as the contract's `{id, born_from, text, check, status}`, or as the id with `text` and `status` beside it in `data`; or `data.rules`, a list. A later line with the same id updates it (proposed → active). Without `born_from`, "from" is the run of the first line that mentioned it |
| final state | the `outcome` line's `data.state` and `data.reason` |
| what a human is asked | the `review` line's `data.question`, else `data.ask`, `data.question` or `data.why` on a `manual` or `blocked` outcome, else its `detail`; a short `data.need` (`real-renfe-data`) heads the box |
| what an agent is doing | lines with `actor: "agent"` and `data.tool` (one per tool call), `data.agent` naming the agent; their `detail` is shown. They don't change their step's status and stay out of the feed |
| cost and tokens | `data.usage` (`cost_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `turns`), summed per run; the outcome's `data.usage`, when there is one, is the run's total. Logs without it (older ones) count `data.cost_usd` instead, so they show cost but no tokens. Missing or non-numeric fields count as 0 |
| what the human decided | the latest `review` line: `data.decision` (approve, request-changes, yes, no, or free text), `data.reason`; `data.played_by` shows as "human (Claude)" in the track and the box; `data.run_id` marks a correction of that run |

A check named `rule …` is shown as learned. A step is drawn as running while its latest line is
`started`; a passing per-check line doesn't end it, the step's own summary line does.

Anything else is tolerated: unknown steps get their own node after `outcome`, unknown actors and
states are shown as plain text (states count as "other"), wrong types are ignored, and lines that
aren't JSON objects are skipped and counted in the footer. Text is always escaped.

## Fixture

`runs/fixtures/demo-events.jsonl` is synthetic: invented runs for the nine scenarios of the contract
table, in the order 1, 2, 3, 8, 6, 4, 5, 7, 9, so the rule learned in scenario 8 shows up in the gates
that follow. Its review lines are shaped like the engine's: Claude, playing the maintainer, approves after
the gate and answers the question before a manual or blocked outcome. Builders log one `agent` line per
tool call, and every agent's last line carries `data.usage`, summed on the outcome, as the engine is about
to. About 4 minutes at `--speed 1`.
`runs/fixtures/real-events.jsonl` (written by the engine, not here) holds runs that really happened.

## Screenshots

`screenshots/`, taken headless at 960x1080 (Chromium on Linux, so Ubuntu Sans stands in for the system font)
unless the name says otherwise; the narrow ones are the whole page at that width:

| File | What |
|---|---|
| `01-gate-running.png` | synthetic: the gate running, check by check |
| `02-claude-approves.png` | synthetic: success after Claude, as the human, approves |
| `03-manual-claude-answers.png` | synthetic: the factory asks the maintainer, Claude answers yes |
| `04-gate-fails.png` | synthetic: the gate fails two checks |
| `05-flawed-rule-learned.png` | synthetic: flawed, and the rule it produced |
| `06-learned-rule-in-gate.png` | synthetic: that rule enforced in a later gate |
| `07-manual-protected-path.png` | synthetic: a protected path; Claude says no, the maintainer decides |
| `08-rejected-injection.png` | synthetic: a prompt injection rejected at intake |
| `09-blocked-claude-says-no.png` | synthetic: blocked on real data; Claude can't provide it |
| `10-real-flawed-mutants.png` | real: the gate's mutants check fails a run |
| `11-real-human-correction.png` | real: a run retried with a human correction, the rule goes active |
| `12-real-blocked.png` | real: blocked on whether Renfe writes Porto times in Lisbon or Madrid time |
| `13-agent-at-work.png` | synthetic: the builder's latest tool calls, with cost and tokens so far |
| `14-real-rejected-injection.png` | real: the quarantine reader refuses a prompt injection |
| `narrow-520-agent-at-work.png` | synthetic, 520 px wide |
| `narrow-480-manual-claude-answers.png` | synthetic, 480 px wide |
| `narrow-560-real-flawed.png` | real, 560 px wide |
| `narrow-640-real-human-correction.png` | real, 640 px wide |
