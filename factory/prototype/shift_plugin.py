"""pytest plugin: pretend the real clock is GATE_SHIFT_DAYS days ahead, for the CLI (renfe.cli.datetime)."""
import os
from datetime import datetime as real_datetime, timedelta

def pytest_configure(config):
    days = int(os.environ.get("GATE_SHIFT_DAYS", "0"))
    if not days:
        return
    from renfe import cli

    class Shifted(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(days=days)

    cli.datetime = Shifted
