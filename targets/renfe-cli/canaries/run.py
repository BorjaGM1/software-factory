"""Synthetic monitoring for renfe-cli: real CLI flows run in-process against the twin of Renfe, with the clock pinned.

Run it inside the target's environment (intake/monitoring does this for you):

    uv run --project TARGET --frozen python targets/renfe-cli/canaries/run.py [--mode MODE] [--clock ISO] [--json]

Each canary is a CLI command and the facts that must hold. A failed canary says how it failed:

    renfe-unavailable   the twin was down, slow, blocked or rate-limited, and the CLI said so the way its contract
                        says (exit 5, a typed JSON error, a 429/403 never retried, Retry-After passed on). Nothing
                        to fix in the CLI.
    api-changed         the CLI stopped with exit 7: Renfe's answer is not the shape it reads.
    calendar            a date the maintainer scheduled work for has come (the legacy shop's last day).
    cli-broken          anything else: a crash, a wrong exit code, a wrong answer, a contract broken under chaos.
"""

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Callable

HERE = Path(__file__).resolve().parent
TWIN = HERE.parent / "twin"


def _target_root() -> Path:
    import renfe

    return Path(renfe.__file__).resolve().parents[2]


sys.path.insert(0, str(TWIN))
sys.path.insert(0, str(_target_root() / "tests"))

import httpx  # noqa: E402

import chaos  # noqa: E402
from renfe import cli, config, retry, search as search_mod  # noqa: E402
from renfe.client import RsvClient  # noqa: E402

MADRID = search_mod.MADRID
DEFAULT_CLOCK = "2026-11-21T10:00:00+01:00"
ISSUE_1 = "https://github.com/luisgonzaleznf/renfe-cli/issues/1"


@dataclass
class Canary:
    name: str
    what: str
    argv: Callable[[datetime], list[str]]
    check: Callable[[dict, datetime], list[str]]  # problems with a successful answer; [] = pass


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def _search_checks(out: dict, clock: datetime, *, fares: bool = True) -> list[str]:
    problems = []
    if not out.get("count"):
        problems.append("no trains listed")
    if fares and not out.get("fares_included"):
        problems.append("fares_included is false")
    if fares and not any(t.get("fares") for t in out.get("trains", [])):
        problems.append("no train has fares")
    if out.get("notice"):
        problems.append(f"notice: {out['notice']}")
    for train in out.get("trains", []):
        if train["departure"][:10] != out["date"]:
            problems.append(f"train {train['service']} leaves on {train['departure'][:10]}, not {out['date']}")
    return problems


def _today_checks(out: dict, clock: datetime) -> list[str]:
    if not out.get("trains") and (out.get("notice") or "").startswith("No more trains today"):
        return []  # late at night that is the right answer
    return _search_checks(out, clock) + ([] if out.get("delays_included") else ["delays_included is false"])


def _stations_checks(out: dict, clock: datetime) -> list[str]:
    names = [s["name"] for s in out.get("stations", [])]
    return [] if names == ["Apeadero Nuevo"] else [f"expected only Apeadero Nuevo from Renfe's live list, got {names}"]


def _delays_checks(out: dict, clock: datetime) -> list[str]:
    numbers = sorted(t.get("train") for t in out.get("trains", []))
    return [] if out.get("delayed") == 2 and numbers == ["00500", "03100"] else [
        f"expected 00500 and 03100 delayed, got delayed={out.get('delayed')} trains={numbers}"]


def _status_checks(out: dict, clock: datetime) -> list[str]:
    nxt = (out.get("next_station") or {}).get("code")
    return [] if out.get("on_map") and out.get("delay_minutes") == 12 and nxt == "04040" else [
        f"expected 03100 on the map, 12 min late, next 04040; got on_map={out.get('on_map')} "
        f"delay={out.get('delay_minutes')} next={nxt}"]


CANARIES = [
    Canary("stations-live", "Renfe's current station list, with a station the shipped list lacks",
           lambda c: ["stations", "Apeadero Nuevo", "--refresh"], _stations_checks),
    Canary("search-ahead", "Madrid-Barcelona 10 days ahead, with fares (the legacy shop before 25 Jan 2027)",
           lambda c: ["search", "Madrid", "Barcelona", "--date", (c.date() + timedelta(days=10)).isoformat()],
           _search_checks),
    Canary("search-new-system", "Madrid-Barcelona on a day Renfe's new system sells, with fares",
           lambda c: ["search", "Madrid", "Barcelona", "--date",
                      max(c.date() + timedelta(days=10), config.NSV_FIRST_SALE_DATE + timedelta(days=4)).isoformat()],
           _search_checks),
    Canary("search-today", "Madrid-Barcelona today, from now on, with live delays",
           lambda c: ["search", "Madrid", "Barcelona", "--date", "today"],
           lambda out, c: _today_checks(out, c)),
    Canary("delays", "the live map's delayed trains", lambda c: ["delays"], _delays_checks),
    Canary("status", "one train on the live map", lambda c: ["status", "03100"], _status_checks),
]


def frozen(moment: datetime):
    from datetime import datetime as real

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    return Frozen


class Sandbox:
    """What the maintainer's conftest does for tests, for one canary: the twin as the only network, no browser, no
    real pauses, a fresh cache dir, the clock pinned for the CLI."""

    def __init__(self, twin: chaos.Twin, clock: datetime):
        self.twin, self.clock, self.pauses = twin, clock, []
        self._saved = []

    def _set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def __enter__(self):
        import webbrowser

        def refuse_network(transport, request):
            raise AssertionError(f"canary tried to reach the real network ({request.url.host})")

        def refuse_browser(*a, **k):
            raise AssertionError("canary tried to open a browser")

        self._cache = tempfile.TemporaryDirectory(prefix="renfe-canary-")
        self._env = os.environ.get(config.CACHE_ENV)
        os.environ[config.CACHE_ENV] = self._cache.name
        self._set(httpx.HTTPTransport, "handle_request", refuse_network)
        self._set(webbrowser, "open", refuse_browser)
        self._set(retry, "_sleep", self.pauses.append)
        self._set(cli, "datetime", frozen(self.clock))
        self._set(cli, "make_client", lambda: RsvClient(transport=self.twin.transport(), spacing=0))
        return self

    def __exit__(self, *exc):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        if self._env is None:
            os.environ.pop(config.CACHE_ENV, None)
        else:
            os.environ[config.CACHE_ENV] = self._env
        self._cache.cleanup()


def invoke(argv: list[str], twin: chaos.Twin, clock: datetime) -> dict:
    out, err = io.StringIO(), io.StringIO()
    started = time.monotonic()
    with Sandbox(twin, clock) as box, redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(argv)
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            traceback.print_exc()
            code = None  # escaped the CLI's own error handling
    return {"argv": ["renfe", *argv], "exit_code": code, "stdout": out.getvalue(), "stderr": err.getvalue(),
            "requests": twin.log, "retry_pauses": box.pauses, "seconds": round(time.monotonic() - started, 3)}


def _unavailable_problems(run: dict, error: dict, mode: str) -> list[str]:
    """Under chaos the CLI's own contract still holds (errors.py, retry.py, client.py docstrings)."""
    problems = []
    if error.get("type") not in ("blocked", "upstream_unavailable"):
        problems.append(f"error type {error.get('type')!r}, expected blocked or upstream_unavailable")
    tries: dict[tuple, int] = {}
    for request in run["requests"]:
        key = (request["host"], request["path"])
        tries[key] = tries.get(key, 0) + 1
    refused = {k: n for k, n in tries.items() if n > 1 and any(
        r["host"] == k[0] and r["path"] == k[1] and r.get("got") in (403, 429) for r in run["requests"])}
    if refused:
        problems.append(f"a 403/429 was retried: {refused}")
    if mode.startswith("rate-limited") and "retry after 30" not in json.dumps(error):
        problems.append("Retry-After: 30 not passed on")
    if any(n > 3 for n in tries.values()):
        problems.append(f"more than 3 tries for a request: {tries}")
    if "Traceback" in run["stderr"]:
        problems.append("a traceback on stderr")
    return problems


# What an answer may lack when part of Renfe failed and the CLI said so in `notice`: degraded, not broken.
_DEGRADED = ("notice: ", "fares_included is false", "no train has fares", "delays_included is false")


def _degraded(problems: list[str], out: dict, mode: str) -> bool:
    return (mode.split(":")[0].split("@")[0] in chaos.CHAOS and bool(out.get("notice"))
            and all(p.startswith(_DEGRADED) for p in problems))


def judge(canary: Canary, run: dict, clock: datetime, mode: str) -> dict:
    out, err = _json(run["stdout"]), _json(run["stderr"])
    error = err.get("error") if isinstance(err, dict) else None
    result = {"canary": canary.name, "what": canary.what, **run}
    if run["exit_code"] == 0 and isinstance(out, dict):
        problems = canary.check(out, clock)
        if not problems:
            result.update(status="pass", failure=None, problems=[])
        elif _degraded(problems, out, mode):
            result.update(status="fail", failure="renfe-unavailable", degraded=True, problems=problems)
        else:
            result.update(status="fail", failure="cli-broken", problems=problems)
    elif run["exit_code"] == 7 and error and error.get("type") == "api_changed":
        result.update(status="fail", failure="api-changed", problems=[error.get("message")])
    elif run["exit_code"] == 5 and error:
        problems = _unavailable_problems(run, error, mode)
        result.update(status="fail", failure="cli-broken" if problems else "renfe-unavailable",
                      problems=problems or [error.get("message")])
    else:
        result.update(status="fail", failure="cli-broken",
                      problems=[f"exit {run['exit_code']}, stdout {'JSON' if out is not None else 'not JSON'}, "
                                f"stderr error {error!r}"])
    return result


def legacy_references() -> list[str]:
    """Where the target's source still refers to the legacy shop: file:line, for the calendar canary's evidence."""
    src = _target_root() / "src" / "renfe"
    pattern = re.compile(r"\blegacy\b|LEGACY_|NSV_FIRST_SALE_DATE", re.IGNORECASE)
    hits = []
    for path in sorted(src.glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"src/renfe/{path.name}:{n}")
    return hits


def calendar_canary(clock: datetime, today_run: dict | None) -> dict:
    """Issue #1: after 25 Jan 2027, delete the legacy-shop code. Fails once the clock reaches that day and the
    code is still there. It runs nothing new: it reads the target's source and the search-today canary's requests."""
    cutoff = config.NSV_FIRST_SALE_DATE
    refs = legacy_references()
    files = sorted({ref.split(":")[0] for ref in refs})
    result = {"canary": "legacy-shop-retired", "what": f"the legacy shop's last day ({ISSUE_1})",
              "argv": None, "exit_code": None, "requests": [], "retry_pauses": [], "seconds": 0.0,
              "stdout": "", "stderr": ""}
    days_left = (cutoff - clock.date()).days
    if days_left > 0:
        result.update(status="pass", failure=None, problems=[],
                      facts={"cutoff": cutoff.isoformat(), "days_left": days_left})
        return result
    legacy_calls = None
    if today_run is not None:
        legacy_calls = sum(1 for r in today_run["requests"] if r["host"] in chaos.PARTS["legacy"])
    result.update(status="fail" if refs else "pass", failure="calendar" if refs else None,
                  problems=[f"{clock.date()} is on or after config.NSV_FIRST_SALE_DATE ({cutoff}): issue #1 says to "
                            f"delete the legacy-shop code, and {len(refs)} lines in {len(files)} files still refer "
                            "to it"] if refs else [],
                  facts={"cutoff": cutoff.isoformat(), "days_past": -days_left, "issue": ISSUE_1,
                         "legacy_references": {f: sum(r.startswith(f + ":") for r in refs) for f in files},
                         "files": files,
                         "legacy_shop_requests_in_search_today": legacy_calls,
                         "checkout_refuses_every_date": "cli.py checkout raises UsageError for day >= "
                                                        "NSV_FIRST_SALE_DATE, and no date before it can be asked "
                                                        "for any more (not run: checkout code is never executed)"})
    return result


def run_all(mode: str, clock: datetime, names: list[str] | None = None) -> list[dict]:
    results = []
    for canary in CANARIES:
        if names and canary.name not in names:
            continue
        results.append(judge(canary, invoke(canary.argv(clock), chaos.Twin(mode), clock), clock, mode))
    if not names or "legacy-shop-retired" in names:
        today = next((r for r in results if r["canary"] == "search-today"), None)
        results.append(calendar_canary(clock, today))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", default="plain", help=f"twin mode: {', '.join(chaos.MODES)} (scope with @part)")
    parser.add_argument("--clock", default=DEFAULT_CLOCK, help="the CLI's 'now', ISO 8601 with offset")
    parser.add_argument("--only", action="append", help="run only this canary (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args()
    clock = datetime.fromisoformat(args.clock)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=MADRID)
    chaos.Twin(args.mode)  # fail early on an unknown mode
    results = run_all(args.mode, clock, args.only)
    report = {"mode": args.mode, "clock": clock.isoformat(), "target": str(_target_root()), "results": results}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"twin: {args.mode}   clock: {clock.isoformat()}   target: {report['target']}")
        for r in results:
            mark = "PASS" if r["status"] == "pass" else f"FAIL ({r['failure']})"
            print(f"  {mark:<26} {r['canary']:<20} {' '.join(r['argv'] or ['-'])[:60]}")
            for problem in r["problems"]:
                print(f"  {'':<26}   {problem}")
    return 0 if all(r["status"] == "pass" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
