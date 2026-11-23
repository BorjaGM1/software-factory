"""A twin of Renfe for renfe-cli: the maintainer's fake (`tests/fakes.py` in the target, imported, never copied)
plus chaos modes, switchable by name.

    plain                 the fake as the maintainer wrote it, answering searches on the day asked for
    shape-drift           search-travel moves each leg's times into nested objects, as journey-search already
                          writes them (departure_datetime -> departure_station.departure_timestamp)
    down                  connections refused
    rate-limited          429 with Retry-After: 30
    blocked               403 with an HTML challenge page
    slow[:SECONDS]        answers take SECONDS (default 30); past the request's own timeout, httpx's ReadTimeout

A chaos mode can be scoped to one part of Renfe with `@`: `down@legacy`, `slow:8@map`. Parts: api (the new system and
its web config), legacy (the legacy shop), map (the live train map), timetable (the GTFS zip). Default: all of them.

Needs `fakes` and `renfe` importable (the target's tests/ on sys.path, the target installed).
"""

import copy
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

import fakes
from renfe import config

MADRID = ZoneInfo("Europe/Madrid")
FAKE_DAY = "2027-01-29"  # the day fakes.TRAVEL_OPTIONS runs

PARTS = {
    "api": ("api.rsv.renfe.com", "venta.rsv.renfe.com"),
    "legacy": ("venta.renfe.com",),
    "map": ("tiempo-real.largorecorrido.renfe.com",),
    "timetable": ("ssl.renfe.com",),
}
CHAOS = ("down", "rate-limited", "blocked", "slow")
MODES = ("plain", "shape-drift", *CHAOS)
CHALLENGE = "<!doctype html><html><head><title>Access denied</title></head><body>Request blocked.</body></html>"


def part_of(host: str) -> str | None:
    return next((part for part, hosts in PARTS.items() if host in hosts), None)


def _local(utc: str) -> str:
    moment = datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(MADRID)
    return moment.strftime("%Y-%m-%dT%H:%M:%S%z")


def drifted(doc: dict) -> dict:
    """search-travel's answer after the drift: each leg's four time fields become two nested objects."""
    doc = copy.deepcopy(doc)
    for option in doc["travel_options"]:
        for leg in option["travel_path"]:
            dep, arr = leg.pop("departure_datetime"), leg.pop("arrival_datetime")
            leg.pop("departure_datetime_timezone")
            leg.pop("arrival_datetime_timezone")
            leg["departure_station"] = {"departure_timestamp": _local(dep)}
            leg["arrival_station"] = {"arrival_timestamp": _local(arr)}
    return doc


class Twin:
    """A MockTransport handler: Renfe as the fake answers it, with `mode` on top. Records every request and what
    it got (an HTTP status or the exception raised)."""

    def __init__(self, mode: str = "plain"):
        self.mode = mode
        name, _, scope = mode.partition("@")
        self.name, _, arg = name.partition(":")
        if self.name not in MODES or (scope and scope not in PARTS) or (scope and self.name not in CHAOS):
            raise ValueError(f"unknown twin mode {mode!r}; modes: {', '.join(MODES)}, scopes: {', '.join(PARTS)}")
        self.scope = scope or None
        self.latency = float(arg) if arg else 30.0
        self.fake = fakes.FakeRenfe(**{config.SEARCH_TRAVEL_PATH: self._search_travel})
        self.log: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        entry = {"method": request.method, "host": request.url.host, "path": request.url.path}
        self.log.append(entry)
        try:
            response = self._answer(request)
        except Exception as exc:
            entry["got"] = type(exc).__name__
            raise
        entry["got"] = response.status_code
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        part = part_of(request.url.host)
        if part is None:
            raise AssertionError(f"the twin has no part for host {request.url.host}")
        if self.name in CHAOS and (self.scope is None or self.scope == part):
            return self._chaos(request)
        return self.fake(request)

    def _search_travel(self, request: httpx.Request) -> httpx.Response:
        """The fake's timetable, moved to the day asked for (the fake always answers FAKE_DAY)."""
        body = json.loads(request.content)
        day = datetime.fromisoformat(body["depart_after_datetime"].replace("Z", "+00:00")).astimezone(MADRID)
        doc = json.loads(json.dumps(fakes.TRAVEL_OPTIONS).replace(FAKE_DAY, day.date().isoformat()))
        return httpx.Response(200, json=drifted(doc) if self.name == "shape-drift" else doc)

    def _chaos(self, request: httpx.Request) -> httpx.Response:
        if self.name == "down":
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)
        if self.name == "rate-limited":
            return httpx.Response(429, headers={"retry-after": "30"}, json={"message": "Too Many Requests"})
        if self.name == "blocked":
            return httpx.Response(403, headers={"content-type": "text/html; charset=utf-8"}, text=CHALLENGE)
        timeout = (request.extensions.get("timeout") or {}).get("read")
        if timeout is not None and self.latency >= timeout:
            raise httpx.ReadTimeout("timed out", request=request)  # what httpx raises; nobody waits for it here
        time.sleep(self.latency)
        return self.fake(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
