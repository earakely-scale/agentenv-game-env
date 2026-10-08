"""The overlay's side of the streamer. `Watch` follows the env through its card: its lobby, its match and the match's
spectator view. `serve()` answers the overlay page and `/state.json` (`{lobby, match, view, broadcast, env}`) from
what Watch last read, so the page reads one origin and the env needs no CORS. Stdlib only: it runs in the streamer
image's system Python."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OVERLAY = Path(__file__).with_name("overlay")
CARD = "/.well-known/agent-env.json"
LOBBY, MATCH = "urn:game:lobby/v1", "urn:game:match/v1"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css"}


def fetch(url: str, timeout: float = 10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def endpoint(card: dict, uri: str) -> str | None:
    """The `get` endpoint of the extension `uri` that `card` advertises."""
    for ext in (card.get("capabilities") or {}).get("extensions") or []:
        if ext.get("uri") == uri:
            params = ext.get("params") or {}
            return ((params.get("methods") or {}).get("get") or {}).get("endpoint") or params.get("endpoint")
    return None


class Watch:
    """The env as the overlay shows it, read on each poll: the lobby, the match, and the page of its spectator view.
    `answered` is when the env last answered (monotonic)."""

    def __init__(self, env: str, broadcast: dict):
        self.env = env.rstrip("/")
        self.state = {"lobby": None, "match": None, "view": None, "broadcast": broadcast, "env": self.env}
        self.answered: float | None = None
        self.card: dict | None = None
        self.views: dict[tuple, str | None] = {}
        self.lock = threading.Lock()

    def poll(self) -> bool:
        """Reads the env once; whether it answered."""
        try:
            self.card = self.card or fetch(self.env + CARD)
            lobby = fetch(self.env + endpoint(self.card, LOBBY))
            match = fetch(self.env + endpoint(self.card, MATCH))
        except (OSError, ValueError, TypeError):
            return False
        view = self.view(match)
        with self.lock:
            self.state = {**self.state, "lobby": lobby, "match": match, "view": view}
            self.answered = time.monotonic()
        return True

    def view(self, match: dict | None) -> str | None:
        """The address of the match's spectator view: the `http` interface of the card at its spectator_url."""
        base = (match or {}).get("spectator_url")
        if not base:
            return None
        key = (match.get("lobby_id"), base)
        if key not in self.views:
            try:
                card = fetch(self.env + base + CARD)
            except (OSError, ValueError):
                return None
            page = next((i["url"] for i in card.get("additionalInterfaces") or () if i.get("transport") == "http"),
                        None)
            self.views[key] = None if page is None else self.env + base + page
        return self.views[key]

    def run(self, every: float, stop: threading.Event) -> None:
        while not stop.is_set():
            self.poll()
            stop.wait(every)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


def serve(watch: Watch, port: int) -> ThreadingHTTPServer:
    """The overlay page and its state, on 127.0.0.1:`port`, in a thread."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/state.json":
                body, kind = json.dumps(watch.snapshot()).encode(), "application/json"
            else:
                file = OVERLAY / ("index.html" if path == "/" else path.lstrip("/"))
                if file.parent != OVERLAY or not file.is_file():
                    return self.send_error(404)
                body, kind = file.read_bytes(), TYPES.get(file.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
