#!/usr/bin/env python3
"""Run a command in a 100x40 pseudo-terminal, show it, and record it as asciinema v2 (with start_epoch).

    python3 demo/rec_cmd.py OUT.cast [--title TEXT] -- CMD...

For the parts of the demo that are a plain command (the canaries), so the video's left side shows them the same
way it shows the builder's TUI.
"""
import fcntl
import json
import os
import pty
import select
import struct
import sys
import termios
import time

COLS, ROWS = 100, 40


def main():
    a = sys.argv[1:]
    out, cmd = a[0], a[a.index("--") + 1:]
    title = a[a.index("--title") + 1] if "--title" in a else None
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm-256color", COLUMNS=str(COLS), LINES=str(ROWS), FORCE_COLOR="1")
        if title:
            print(f"\x1b[1;38;5;183m{title}\x1b[0m\r\n", flush=True)
        os.execvp(cmd[0], cmd)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    start = time.time()
    with open(out, "w") as f:
        f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS, "timestamp": int(start),
                            "env": {"TERM": "xterm-256color"}, "start_epoch": start}) + "\n")
        while True:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
                f.write(json.dumps([round(time.time() - start, 4), "o", data.decode("utf-8", "replace")]) + "\n")
            else:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    sys.exit(os.waitstatus_to_exitcode(status))
    try:
        _, status = os.waitpid(pid, 0)
        sys.exit(os.waitstatus_to_exitcode(status))
    except ChildProcessError:
        sys.exit(0)


if __name__ == "__main__":
    main()
