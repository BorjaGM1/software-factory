# Monitoring: twin, canaries, intake

renfe-cli is a CLI, so monitoring here is synthetic: real CLI flows run in-process against a twin of Renfe,
with the clock pinned. Nothing reaches the network and no checkout code runs.

```bash
python -m intake.monitoring                                   # all canaries, plain twin: should all pass
python -m intake.monitoring --mode shape-drift                # scenario 1 -> event api-shape-changed
python -m intake.monitoring --mode down                       # scenario 2 -> no event, "no change" logged
python -m intake.monitoring --clock 2027-01-25T09:00:00+01:00 # scenario 3 -> event clock-canary
python -m intake.monitoring --mode shape-drift --check --target <worktree>   # judge a fix: exit 0 = all pass
```

Run from the factory's root. Without `--target` it cuts a throwaway worktree of the target (repo and base from
`targets/renfe-cli/target.toml`, else `~/projects/renfe-cli-lab` at `main`), runs the canaries with
`uv run --project <worktree> --frozen --offline`, and removes the worktree. Events go to `runs/inbox/`, log lines
to `runs/events.jsonl` (`--runs DIR` to write elsewhere).

## The parts

- [`targets/renfe-cli/twin/chaos.py`](../../targets/renfe-cli/twin/chaos.py): the maintainer's `tests/fakes.py`,
  imported (never copied or edited), answering searches on the day asked for, plus modes switchable by name:
  `plain`, `shape-drift`, `down`, `rate-limited` (429, Retry-After 30), `blocked` (403 HTML page),
  `slow[:SECONDS]` (past the request's timeout, httpx's `ReadTimeout`, without waiting for it). A chaos mode can
  hit one part of Renfe: `down@legacy`, `slow:3@map` (parts: `api`, `legacy`, `map`, `timetable`).
- [`targets/renfe-cli/canaries/run.py`](../../targets/renfe-cli/canaries/run.py): six CLI flows (`stations
  --refresh`, search 10 days ahead, search on a new-system day, search today with live delays, `delays`,
  `status`), each with the facts that must hold, plus `legacy-shop-retired`, a calendar check that reads the
  target's source. Each failure is classed `api-changed`, `renfe-unavailable` (the CLI said so as its contract
  says: exit 5 and a typed error, 403/429 never retried, Retry-After passed on, or exit 0 with a notice when only
  part of Renfe failed), `calendar` or `cli-broken`.
- `intake/monitoring/`: runs them and turns each failure worth a change into one event (failures with the same
  cause are grouped). `title` is the intake's own words, `evidence` is machine facts (argv, exit code, the CLI's
  JSON error, requests made, pauses), `untrusted` is always `null`. Canary stdout never goes into an event.

| Failure | Becomes |
|---|---|
| `api-changed` | event `api-shape-changed`, with `paths`, `needs`, and `acceptance` for the builder |
| `calendar` | event `clock-canary` (source `canary`); its `paths` include `src/renfe/checkout.py` and it names `NSV_FIRST_SALE_DATE`, so triage stops it as `manual` |
| `cli-broken` | event `bug-report` |
| `renfe-unavailable` | no event: an `intake` line and an `outcome` line (`success`, reason `no-change`) |

Samples from a real run: [`runs/fixtures/monitoring/`](../../runs/fixtures/monitoring/).

## What renfe-cli does under chaos (target `4226158`)

- **Shape drift** (search-travel moves leg times into `departure_station.departure_timestamp` /
  `arrival_station.arrival_timestamp`): exit 7, `search-travel leg: missing timestamp`, with the maintainer's
  hint. Before the cut-off, searches never reach search-travel (the legacy shop answers them), so only the
  new-system canary sees it. A hand-made fix (about 10 lines in `search.py`, read the new fields when the flat
  ones are missing) gives the same trains, times and fares as the plain twin and leaves the suite unchanged.
  It is not written into the target; its criteria are in the event.
- **Down**: 3 tries per request, 1 s and 2 s apart, all listed in `retries`; exit 5. A search before the cut-off
  tries the legacy shop 3 times, falls back to the new system, tries that 3 times: 6 s of pauses.
- **Rate-limited / blocked**: one request, never retried, exit 5; `retry after 30 s` passed on. The HTML page is
  called "bot protection?".
- **Slow**: the timeout ends it, not retried (except the legacy shop's read timeout, tried once more), exit 5.
- **Only the legacy shop down**: exit 0, the new system's list without fares, a notice saying so.
- **Only the live map down or slow**: search today exits 0 with "Delays are unknown"; `delays` and `status`
  exit 5.
- **25 Jan 2027**: every flow keeps working (search today makes no legacy-shop request), so nothing breaks on the
  day; what comes due is issue #1, the code to delete (88 lines in 8 files refer to the legacy shop or the
  cut-off date). `renfe checkout` then refuses every date. The canary does not run it.

## Things to know

- On the base, `tests/test_legacy.py::test_cli_search_falls_back_with_exit_0` fails from 21 Nov 2026: it searches
  `2026-11-20`, which is now in the past. The suite is not green before any change (`fix/pin-clock` in the
  clone, unmerged, pins the clock).
- The fake's new station reuses code `99001`, which is a real station (Santa Pola-Bus) in the shipped list. The
  stations canary uses `--refresh` so it really asks the twin.
