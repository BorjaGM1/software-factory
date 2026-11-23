#!/usr/bin/env python3
"""Drive and record a keepty session (the TUI in the demo video).

    kt.py start NAME -- CMD...     start a session running CMD (detached)
    kt.py send NAME TEXT [--enter] type TEXT into it (as its writer), optionally press Enter
    kt.py key NAME KEY             press a key: enter, esc, tab, up, down, ctrl-c
    kt.py screen NAME              print what the screen shows now (needs pyte: `uv run --with pyte`)
    kt.py record NAME OUT.cast     record the session as an asciinema v2 file until Ctrl-C / SIGTERM
    kt.py stop NAME

keepty only attaches from a terminal, so every attach runs in a pseudo-terminal of the size below.
"""
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

COLS, ROWS = int(os.environ.get("KT_COLS", 100)), int(os.environ.get("KT_ROWS", 40))
KEYS = {"enter": b"\r", "esc": b"\x1b", "tab": b"\t", "up": b"\x1b[A", "down": b"\x1b[B", "ctrl-c": b"\x03",
        "shift-tab": b"\x1b[Z"}
DETACH = b",,,d"  # keepty's detach sequence for a writer


def attach(args):
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(args[0], args)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    return pid, fd


def drain(fd, secs):
    out, end = b"", time.time() + secs
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                out += os.read(fd, 65536)
            except OSError:
                break
    return out


def close(pid, fd, detach=False):
    if detach:
        try:
            os.write(fd, DETACH)
            drain(fd, 0.3)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


def send(name, data: bytes, settle=0.4):
    pid, fd = attach(["keepty", "attach", name])
    drain(fd, 0.6)  # the attach banner and the screen repaint
    for chunk in [data[i:i + 64] for i in range(0, len(data), 64)]:  # paste-like, but not one huge write
        os.write(fd, chunk)
        drain(fd, 0.02)
    drain(fd, settle)
    close(pid, fd, detach=True)


def screen(name):
    import pyte
    pid, fd = attach(["keepty", "attach", "--watch", name])
    raw = drain(fd, 1.2)
    close(pid, fd)
    scr = pyte.Screen(COLS, ROWS)
    pyte.ByteStream(scr).feed(raw)
    lines = [l.rstrip() for l in scr.display]
    if lines and "detach" in lines[-1]:
        lines.pop()  # keepty's status bar
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def record(name, out):
    pid, fd = attach(["keepty", "attach", "--watch", name])
    start = time.time()
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    with open(out, "w") as f:
        f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS, "timestamp": int(start),
                            "env": {"TERM": "xterm-256color"}, "start_epoch": start}) + "\n")
        try:
            while not stop:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        break
                    f.write(json.dumps([round(time.time() - start, 4), "o", data.decode("utf-8", "replace")]) + "\n")
                    f.flush()
        except KeyboardInterrupt:
            pass
    close(pid, fd)


def main():
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    cmd, name = a[0], a[1] if len(a) > 1 else ""
    if cmd == "start":
        rest = a[a.index("--") + 1:]
        # keepty start sizes the session from its own terminal: run it in one of ours, then detach.
        pid, fd = attach(["keepty", "start", name, "--", *rest])
        drain(fd, 3)
        close(pid, fd, detach=True)
        time.sleep(3)
        pid, fd = attach(["keepty", "attach", "--watch", name])  # one sized attach sets the session's size
        drain(fd, 0.8)
        close(pid, fd)
    elif cmd == "send":
        send(name, a[2].encode() + (b"\r" if "--enter" in a else b""))
    elif cmd == "key":
        send(name, KEYS[a[2]], settle=0.2)
    elif cmd == "screen":
        print(screen(name))
    elif cmd == "record":
        record(name, a[2])
    elif cmd == "stop":
        subprocess.run(["keepty", "stop", name])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
