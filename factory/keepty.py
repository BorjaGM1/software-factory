"""A command in a keepty session (https://crates.io/crates/keepty), recorded as asciinema v2 from its first byte.

keepty attaches only from a terminal and sizes a session from it, so every attach runs in a pseudo-terminal of
COLS x ROWS. A watcher gets no replay of earlier output, so the command waits behind a gate file until the
recorder is attached; `go()` opens it.
"""

import fcntl
import json
import os
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

COLS, ROWS = int(os.environ.get("KT_COLS", 100)), int(os.environ.get("KT_ROWS", 40))
DETACH = b",,,d"  # keepty's detach sequence for a writer
BAR = re.compile(r"\x1b7\x1b\[\d+;1H\x1b\[2K\x1b\[7m\[[^\]]*\] q detach \| r refresh\x1b\[0m\x1b8")  # the watcher's own bar
ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78]")


def _attach(args: list[str]):
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(args[0], args)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    return pid, fd


def _drain(fd, secs: float) -> bytes:
    out, end = b"", time.time() + secs
    while time.time() < end:
        if select.select([fd], [], [], 0.05)[0]:
            try:
                out += os.read(fd, 65536)
            except OSError:
                break
    return out


def _close(pid, fd, detach=False):
    if detach:
        try:
            os.write(fd, DETACH)
            _drain(fd, 0.3)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass
    try:
        os.close(fd)
    except OSError:
        pass


class Session:
    """`with Session(name, cmd, cwd, cast) as s:` starts the session (gated), records it, and stops it at the end."""

    def __init__(self, name: str, cmd: list[str], cwd, cast: Path, env: dict | None = None):
        self.name, self.cast = name, Path(cast)
        self.gate = self.cast.with_suffix(".go")
        self.gate.unlink(missing_ok=True)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
        self.script = (f"while [ ! -e {shlex.quote(str(self.gate))} ]; do sleep 0.1; done; "
                       f"cd {shlex.quote(str(cwd))} && exec env {exports} {shlex.join(cmd)}")
        self.text, self.last_output, self.started = "", time.time(), None
        self._stop = threading.Event()

    def __enter__(self):
        subprocess.run(["keepty", "stop", self.name], capture_output=True)
        pid, fd = _attach(["keepty", "start", self.name, "--", "sh", "-c", self.script])
        _drain(fd, 1.5)
        _close(pid, fd, detach=True)
        self._pid, self._fd = _attach(["keepty", "attach", "--watch", self.name])
        _drain(self._fd, 0.8)  # the watcher's bar
        self._thread = threading.Thread(target=self._record, daemon=True)
        self.started = time.time()
        self._thread.start()
        self.gate.touch()
        return self

    def _record(self):
        with self.cast.open("w") as f:
            f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS, "timestamp": int(self.started),
                                "start_epoch": self.started, "title": self.name,
                                "env": {"TERM": "xterm-256color", "SHELL": "/bin/sh"}}) + "\n")
            while not self._stop.is_set():
                if not select.select([self._fd], [], [], 0.1)[0]:
                    continue
                try:
                    data = os.read(self._fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                out = BAR.sub("", data.decode("utf-8", "replace"))
                if out:
                    f.write(json.dumps([round(time.time() - self.started, 4), "o", out]) + "\n")
                    f.flush()
                    self.last_output = time.time()
                    self.text = (self.text + ANSI.sub(" ", out))[-20000:]

    def send(self, data: bytes, settle: float = 0.3):
        """Type into the session as its writer, in small chunks, then detach."""
        pid, fd = _attach(["keepty", "attach", self.name])
        _drain(fd, 0.5)
        for i in range(0, len(data), 256):
            os.write(fd, data[i:i + 256])
            _drain(fd, 0.01)
        _drain(fd, settle)
        _close(pid, fd, detach=True)

    def alive(self) -> bool:
        listing = subprocess.run(["keepty", "list"], capture_output=True, text=True).stdout
        return any(line.split()[:2] == [self.name, "(running)"] for line in listing.splitlines())

    def quiet_for(self) -> float:
        return time.time() - self.last_output

    def __exit__(self, *exc):
        time.sleep(0.5)
        self._stop.set()
        self._thread.join(timeout=3)
        _close(self._pid, self._fd)
        subprocess.run(["keepty", "stop", self.name], capture_output=True)
        self.gate.unlink(missing_ok=True)
