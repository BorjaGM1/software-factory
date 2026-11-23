# software-factory

A small software factory. A notice about a project goes in (a failed check, a bug report, a feature request) and
the run ends in one of four ways: a change on a local branch that passed a gate the agent never sees (`success`),
a change the gate or the reviewer rejected (`flawed`), a stop that says what the factory lacks (`blocked`), or a
stop because the decision belongs to the maintainer (`manual`).

```
notice -> triage -> build (agent) -> scope check -> gate -> review -> outcome -> learn
```

It is the code of a demo on [renfe-cli](https://github.com/luisgonzaleznf/renfe-cli), and it works on other
Python projects tested with pytest. It runs on your machine, with your own Claude Code.

## What you need

- git
- Python 3.12, on PATH as `python3.12`. The factory itself uses only the standard library.
- [Claude Code](https://code.claude.com) (`claude`), signed in or with `ANTHROPIC_API_KEY` set. Checked with
  version 2.1.330.
- [uv](https://docs.astral.sh/uv/), for the demo. Your own project needs whatever its commands in `target.toml` use.

`bin/factory` tells you which of these is missing before it starts anything.

## Quick start: the demo

The demo target is a clone of renfe-cli in `~/projects/renfe-cli-lab` (to keep it elsewhere, change `repo` in
[`targets/renfe-cli/target.toml`](targets/renfe-cli/target.toml)). The example notices describe renfe-cli as it
was at commit `4226158`, so put your clone's `main` there:

```sh
git clone https://github.com/luisgonzaleznf/renfe-cli ~/projects/renfe-cli-lab
git -C ~/projects/renfe-cli-lab checkout -B main 4226158
```

See it without spending anything. This replays runs that really happened:

```sh
python3.12 dashboard/serve.py --replay --fixture runs/fixtures/real-events.jsonl    # then open http://127.0.0.1:8765
```

Then a first notice of your own run. One terminal for the dashboard, one for the factory:

```sh
python3.12 dashboard/serve.py
FACTORY_AGENT_MODEL=haiku bin/factory run factory/examples/evt-s8-from-now.json --retries 1
```

The first run takes longer: it runs the target's suite four times to learn what already fails on `main`, and
keeps that. When the run ends well, the change is one commit on the branch `factory/<run>` of your clone, and
nothing has left your machine. `bin/factory rules` shows what it learned and `bin/factory costs` what it spent.

More notices to try, in [`factory/examples/`](factory/examples/):

| Notice | What to expect |
|---|---|
| `evt-s8-from-now.json`, `evt-overdue.json`, `evt-train-stations.json` | a fix or a small feature; the gate may reject the first attempt |
| `evt-timetable-stale.json` | in the demo the gate passed it and the reviewer sent it back |
| `evt-s4-checkout.json` | stops: it touches the payment code, which belongs to the maintainer |
| `evt-s9-porto-zone.json` | stops: it needs real data nobody recorded |
| `evt-s9b-container.json` | needs a container: stops unless this machine has Docker running |

The demo also has two intakes that write notices, both made for renfe-cli:
[monitoring](intake/monitoring/README.md) (canaries against a fake Renfe) and
[feedback](intake/feedback/README.md) (a GitHub issue or a reply on X, read by a model with no tools and then
checked by code). What they queue lands in `runs/inbox/`; `bin/factory watch` runs it.

## Your own project

It has to be a git repository with Python code and a pytest suite that passes offline.

**1. Describe it** in `targets/<name>/target.toml`. This much is enough:

```toml
name = "myproject"
repo = "~/code/myproject"      # your clone; the factory cuts throwaway worktrees from it
base = "main"                  # the branch or commit changes start from
setup = ["uv sync -q --locked"]                       # run once in each worktree, before anything else
allowed_paths = ["src/*", "tests/*", "README.md"]     # what a change may touch (`*` crosses `/`)
forbidden_paths = ["pyproject.toml", "uv.lock", ".github/*"]   # what stops a run if a change touches it

[verify]
test = "uv run pytest -q -p no:cacheprovider"   # pytest, with -q: the gate adds arguments and reads its output
tests_dir = "tests"
src_dir = "src"

[triage]
kinds = { bug-report = "fix", feature-request = "add", question = "investigate" }   # notice kind -> task type
```

The rest is optional; [`targets/renfe-cli/target.toml`](targets/renfe-cli/target.toml) uses all of it:

| Key | What it adds |
|---|---|
| `verify.lock` | commands that must pass on the change (`uv lock --check`) |
| `verify.clock_shifts`, `verify.clock_module` | the suite again with the clock moved that many days ahead, for the module that does `from datetime import datetime` |
| `verify.startup`, `verify.startup_env` | commands that must still start, with no network |
| `verify.mutants_max` | how many wrong versions of the change the tests must catch (6) |
| `verify.unverifiable_claims` | checks a commit message may not claim, as regex fragments |
| `[[intent]]` | a file, a phrase in it and why: what the maintainer decided on purpose. A change there stops and asks |
| `triage.manual_kinds`, `triage.capabilities` | kinds that never reach a builder; what a notice may need, and the command that tells whether this machine has it |
| `[builder] notes` | facts about the project that go into every builder prompt |
| `commit_author` | the author of the factory's commits (default: your git identity) |
| `source` | where to clone the project from, for the message of a machine that lacks it |

(`verify.sweep_paths` and `sweep_module` there are a check made for renfe-cli's station search. Leave them out.)

**2. Try the gate alone** on a branch of yours with one commit on top of `base`. No agent, no cost. It is judged
as a fix, so it needs a new test that fails without the change:

```sh
bin/factory --target targets/myproject/target.toml gate my-branch
```

**3. Write a notice** as JSON. `evidence` is optional; what you put in `acceptance` is what the builder must make
true:

```json
{
  "id": "evt-20270101-is-even",
  "source": "human",
  "kind": "bug-report",
  "title": "is_even(2) returns False",
  "evidence": {"acceptance": ["is_even(2) is True and is_even(3) is False", "a new test covers it"]},
  "untrusted": null,
  "created_at": "2027-01-01T10:00:00+01:00"
}
```

**4. Run it.** `--target` goes before the command:

```sh
FACTORY_AGENT_MODEL=haiku bin/factory --target targets/myproject/target.toml run notice.json --retries 1
```

Two things to know. The gate runs your suite many times (about 12 in the demo: the suite, the old tests against
the new code, each mutant, each clock shift), so a 2-minute suite means about half an hour per notice. And there
are no rules to change: a clone starts with none and learns its own from your project's runs (the ones the demo
learned are examples in [`targets/renfe-cli/rules-example/`](targets/renfe-cli/rules-example/README.md), which
nothing loads). `rules/` belongs to the clone of the factory, not to a target: keep one clone per project, or
empty `rules/` when you change targets.

## Models and cost

Every agent is your own Claude Code, so it spends your plan or your API key.

| Agent | Model by default | Spending cap | Change them with |
|---|---|---|---|
| builder | your account's default, which can be its most expensive one | 2 USD per attempt | `FACTORY_AGENT_MODEL`, `FACTORY_AGENT_BUDGET` |
| reviewer | `sonnet` | 0.5 USD per decision | `FACTORY_REVIEWER_MODEL`, `FACTORY_REVIEWER_BUDGET`; `FACTORY_REVIEWER=off` skips it |
| checker (writes a rule's check) | `haiku` | 0.5 USD | `FACTORY_CHECKER_MODEL`, `FACTORY_CHECKER_BUDGET` |
| reader (feedback intake) | `haiku` | none: one answer, no tools | `FACTORY_QUARANTINE_MODEL` |

One notice is one builder attempt and one review, plus one more of each per retry (`--retries N`) and once more
when the reviewer sends a change back. In the demo, with `FACTORY_AGENT_MODEL=haiku` and the reviewer on sonnet, a
notice took from 53 to 228 seconds and cost from 2 to 9 cents. Set the builder's model: without it, the three
attempts a notice can take with `--retries 1` are up to 6 USD at the default cap.

`FACTORY_AGENT_CMD`, `FACTORY_REVIEWER_CMD` and `FACTORY_CHECKER_CMD` replace `claude -p` itself. A command that
isn't `claude` is run as given, and nothing below is added to it or checked for it.

## What the agents can touch

- **The builder** has six tools (Read, Edit, Write, Glob, Grep, Bash). Files: only inside its worktree, a
  throwaway copy of your project under `runs/<run>/wt`. Shell: your test command, `git diff`, `git status` and
  `ls`. It starts without your connectors, skills, settings files or memory. After it finishes, the factory checks
  its diff against `allowed_paths`, `forbidden_paths` and the `[[intent]]` spots, and only then runs the gate,
  which the builder never sees.
- **The reviewer, the checker and the feedback reader** have no tools at all, and nothing of your account.
- **Each run proves it.** Claude Code's own first line of a session lists its tools and servers. If an agent
  lists a tool it shouldn't have, any connector, or calls a tool that isn't its own, it is stopped and nothing it
  said is used. What each agent listed is in the run log.
- **A shell command written by a model runs only after you approve it.** A correction (`factory correct`) becomes
  a rule, and a model writes the command that checks it. `bin/factory approve RULE_ID` shows you the command and
  asks; until then it runs nowhere.
- **Text from outside** (an issue body, a reply) is read only by the reader. The builder gets a task written by
  the reader and checked by code, never the text.

This is not a sandbox. The builder runs your tests, and tests are code it can write: that code runs as you, with
your files and your network. The rules above keep an agent from wandering; they don't hold one that is trying to
get out. For notices you don't trust, run the factory in a container, a VM or as another user.

## What it does not do

- It doesn't merge, push or open a pull request. A success is a commit on a local branch for you to read.
- It doesn't listen to GitHub or X. It reads JSON files with those shapes; nothing fetches them, and the samples
  are invented.
- It doesn't schedule anything. The canaries of the demo run when you run them.
- The two intakes are written for renfe-cli. For your project you write the notice by hand, or adapt them.
- The reviewer is Claude playing the maintainer, not a person. A person comes in with
  `bin/factory correct RUN_ID "what was wrong"` and `bin/factory approve RULE_ID`.
- "Learning" is narrow. A failed gate check turns on one of seven lessons already written in
  [`factory/learn.py`](factory/learn.py). A correction keeps your sentence as the rule.
- Only Python with pytest.

## Tests

```sh
python3.12 -m unittest discover -s factory -t .      # what each agent starts with, and who approves a check; no model
python3.12 -m unittest discover -s intake/feedback   # the feedback checks; no model; skipped without the demo target
python3.12 intake/feedback/intake.py --self-test     # two haiku calls: the reader has no tools, and the proof works
```

## Where things are

| Path | What |
|---|---|
| [`factory/`](factory/README.md) | the engine: triage, builder, gate, reviewer, learning, costs |
| [`targets/renfe-cli/`](targets/renfe-cli/target.toml) | the demo target: its `target.toml`, the fake Renfe's chaos modes, the canaries, and [the rules the demo learned](targets/renfe-cli/rules-example/README.md) |
| [`intake/`](intake/feedback/README.md) | the two intakes of the demo |
| [`dashboard/`](dashboard/README.md) | a local page that follows `runs/events.jsonl` |
| [`CONTRACT.md`](CONTRACT.md) | the formats: notice (event), task, run log, outcomes |
| `runs/`, `rules/` | what the factory writes and learns. Local, git-ignored, except the sample logs in `runs/fixtures/` |
| `demo/` | the tools that recorded the demo's video (keepty, a headless browser) |

## License

[MIT](LICENSE).
