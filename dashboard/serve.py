#!/usr/bin/env python3
"""Serve the dashboard on localhost and stream an events log to it, live.

    python3 dashboard/serve.py                  # follow runs/events.jsonl
    python3 dashboard/serve.py --replay         # replay the synthetic fixture and follow that
    python3 dashboard/serve.py --replay --speed 3 --loop
"""
import argparse
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import replay

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def follow(path, poll=0.2, ping_every=15.0):
    """Yield ("reset"|"ready"|"ping", None) and ("line", text) while path grows; reset if it shrinks or is replaced."""
    pos, ino, buf, ready, last_ping = 0, None, b"", False, time.monotonic()
    yield "reset", None
    while True:
        try:
            st = path.stat()
        except FileNotFoundError:
            st = None
        if st is None or (ino is not None and st.st_ino != ino) or st.st_size < pos:
            if pos or buf:
                yield "reset", None
            pos, buf = 0, b""
            ino = st.st_ino if st else None
        elif ino is None:
            ino = st.st_ino
        if st is not None and st.st_size > pos:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
            pos += len(chunk)
            *lines, buf = (buf + chunk).split(b"\n")
            for line in lines:
                line = line.strip()
                if line:
                    yield "line", line.decode("utf-8", "replace")
        if not ready:
            ready = True
            yield "ready", None
        if time.monotonic() - last_ping > ping_every:
            last_ping = time.monotonic()
            yield "ping", None
        time.sleep(poll)


class Handler(BaseHTTPRequestHandler):
    log_path: Path

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/events":
            return self.stream()
        if route not in STATIC:
            return self.send_error(404)
        name, ctype = STATIC[route]
        body = (HERE / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        for kind, line in follow(self.log_path):
            msg = f"data: {line}\n\n" if kind == "line" else ": ping\n\n" if kind == "ping" else f"event: {kind}\ndata:\n\n"
            try:
                self.wfile.write(msg.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    def log_message(self, *args):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", type=Path, help="events file to follow (default: runs/events.jsonl, or the replay file)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--replay", action="store_true", help="replay runs/fixtures/demo-events.jsonl into runs/replay/events.jsonl")
    p.add_argument("--fixture", type=Path, default=replay.FIXTURE, help="log to replay (default: the synthetic one; "
                   "runs/fixtures/real-events.jsonl holds the runs that really happened)")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed (2 = twice as fast)")
    p.add_argument("--delay", type=float, default=3.0, help="seconds before the replay starts")
    p.add_argument("--loop", action="store_true", help="restart the replay when it ends")
    a = p.parse_args()

    Handler.log_path = (a.log or (replay.REPLAY_LOG if a.replay else ROOT / "runs/events.jsonl")).resolve()
    if a.replay:
        threading.Thread(target=replay.replay, daemon=True, kwargs=dict(
            fixture=a.fixture, target=Handler.log_path, speed=a.speed, fresh=True, delay=a.delay, loop=a.loop)).start()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    server.daemon_threads = True
    print(f"dashboard: http://127.0.0.1:{a.port}  following {Handler.log_path}" + (" (replay)" if a.replay else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
